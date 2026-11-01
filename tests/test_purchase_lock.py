"""复现另一个刷新已排队时，勾选到结算不能被抢占。"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.config import CST, Config
from core.errors import ParallelSubmission, Retryable, StopOrder, VerificationRequired
from core.purchase_lock import PurchaseLanes, PurchaseLease, SubmissionGuard
from core.scheduler import Scheduler
from core.state_machine import StateStore
from main import execute, run_one


class ParallelPurchaseTests(unittest.IsolatedAsyncioTestCase):
    def task(self, taskid='detail', mode='direct', sku='111', target=100, platform='jd'):
        return SimpleNamespace(id=taskid, mode=mode, sku=sku, platform=platform,
            target=datetime.fromtimestamp(target, CST), retry={'timeout_seconds': 30})

    async def test_cart_and_detail_can_hold_their_lanes_concurrently(self):
        lanes = PurchaseLanes()
        cart = lanes.for_task(self.task('cart-1', 'cart'))
        direct = lanes.for_task(self.task())
        async with cart:
            async with asyncio.timeout(.1):
                async with direct:
                    self.assertTrue(cart.locked())
        self.assertIs(cart, lanes.for_task(self.task('cart-2', 'cart')))
        self.assertIsNot(direct, lanes.for_task(self.task('detail-2')))
        self.assertIsNot(cart, lanes.for_task(self.task('cart', 'direct')))

    async def test_other_platforms_retain_shared_checkout_lane(self):
        lanes = PurchaseLanes()
        self.assertIs(lanes.for_task(self.task('tb1', platform='taobao')),
            lanes.for_task(self.task('tb2', platform='taobao')))

    async def test_same_sku_overlapping_routes_allow_only_one_submission_intent(self):
        guard = SubmissionGuard()
        persisted = []
        guard.claim(self.task('cart', 'cart'), None, lambda: persisted.append('cart'))
        with self.assertRaisesRegex(ParallelSubmission, '并行任务 cart'):
            guard.claim(self.task(target=101), None, lambda: persisted.append('detail'))
        self.assertEqual(persisted, ['cart'])

    async def test_failed_persistence_does_not_block_other_route(self):
        guard = SubmissionGuard()
        def fail():
            raise StopOrder('模拟落盘失败')
        with self.assertRaises(StopOrder):
            guard.claim(self.task(), None, fail)
        guard.check(self.task())

    async def test_other_products_and_nonoverlapping_windows_are_independent(self):
        guard = SubmissionGuard()
        guard.claim(self.task(), None, lambda: None)
        guard.check(self.task(sku='222'))
        guard.check(self.task(target=131))
        guard.check(self.task(platform='taobao'))

    async def test_now_mode_uses_effective_trigger_window(self):
        guard = SubmissionGuard()
        guard.claim(self.task(target=1000), 100_000_000_000, lambda: None)
        with self.assertRaises(StopOrder):
            guard.check(self.task(target=2000), 101_000_000_000)

    async def test_start_all_wires_independent_lanes_and_reports_loser_as_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            target = datetime.now(CST) + timedelta(minutes=5)
            cfg = Config.parse(Path(folder) / 'config.yaml', {'tasks': [
                {'id': 'cart', 'sku_id': '111', 'target_time': target.isoformat(), 'source': 'cart', 'purchase_mode': 'cart'},
                {'id': 'detail', 'sku_id': '111', 'target_time': target.isoformat(), 'purchase_mode': 'direct'}],
                'timing': {'sync_clock': False}, 'notification': {'desktop': False}})
            context = SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(is_closed=lambda: False)), close=AsyncMock())
            class Playwright:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
            entered, locks, guards = set(), [], []
            both, claimed = asyncio.Event(), asyncio.Event()
            async def operation(session, lock, *args):
                async with lock:
                    entered.add(session.task.id)
                    locks.append(lock)
                    guards.append(session.submission_guard)
                    if len(entered) == 2:
                        both.set()
                    await asyncio.wait_for(both.wait(), 1)
                    if session.task.id == 'cart':
                        session.submission_guard.claim(session.task, None,
                            lambda: session.store.claim(session.key, session.task.id, 10))
                        session.submitted = True
                        session.store.finish(session.key, 'confirmed', '123456789012')
                        session.store.event(session.task.id, 'success')
                        claimed.set()
                        return True
                    await asyncio.wait_for(claimed.wait(), 1)
                    session.submission_guard.check(session.task)
            args = SimpleNamespace(command='run', task=None, platform='jd', now=True, dry_run=False, noninteractive=True)
            with patch('main.async_playwright', return_value=Playwright()), \
                    patch('main.launch', new=AsyncMock(return_value=context)), patch('main.run_one', side_effect=operation):
                self.assertEqual(await execute(cfg, args), 0)
            self.assertEqual(len({id(lock) for lock in locks}), 2)
            self.assertIs(guards[0], guards[1])
            store = StateStore(Path(folder) / 'data/state.sqlite3')
            try:
                result = store.snapshot()
                self.assertEqual({item['state'] for item in result['tasks']}, {'success', 'duplicate_stopped'})
                self.assertEqual(len(result['orders']), 1)
            finally:
                store.close()


class PurchaseLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_acquire_does_not_queue_behind_a_waiter(self):
        lock = asyncio.Lock()
        lease = PurchaseLease(lock)
        await lease.acquire()
        entered = asyncio.Event()
        async def other():
            async with lock:
                entered.set()
        job = asyncio.create_task(other())
        try:
            await asyncio.sleep(0)
            async with lease:
                await asyncio.sleep(0)
                self.assertFalse(entered.is_set())
            await asyncio.wait_for(job, 1)
            self.assertTrue(entered.is_set())
            self.assertFalse(lock.locked())
        finally:
            lease.release()
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_cancelled_waiter_never_releases_another_owners_lock(self):
        lock = asyncio.Lock()
        await lock.acquire()
        lease = PurchaseLease(lock)
        async def waiter():
            try:
                await lease.acquire()
            finally:
                lease.release()
        job = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertTrue(lock.locked())
        lock.release()

    async def test_lease_cannot_be_used_or_released_by_another_task(self):
        lock = asyncio.Lock()
        lease = PurchaseLease(lock)
        await lease.acquire()
        async def misuse():
            with self.assertRaises(RuntimeError):
                await lease.acquire()
            with self.assertRaises(RuntimeError):
                lease.release()
        await asyncio.create_task(misuse())
        self.assertTrue(lock.locked())
        lease.release()
        self.assertFalse(lock.locked())


class CartCheckoutHandoffTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, *, before_target=0):
        scheduler = Scheduler(precision=False)
        self.addCleanup(scheduler.close)
        events = []
        task = SimpleNamespace(id='selected-task', sku='111111111', quantity=1,
            target=datetime.fromtimestamp(scheduler.now_ns() / 1e9 + before_target, CST),
            mode='cart', source='cart', auto_submit=True,
            retry={'timeout_seconds': 2, 'max_attempts': 2, 'enabled': True, 'interval_ms': 20})
        session = SimpleNamespace(task=task, scheduler=scheduler, key='local-test', submitted=False,
            cfg=SimpleNamespace(timing={'cart_refresh_enabled': True, 'cart_refresh_start_before_seconds': 2,
                'cart_refresh_interval_ms': 200}),
            cart_selection=None, cart_selected_ns=None, cart_preloaded=True, cart_timings={},
            cart=SimpleNamespace(select=AsyncMock()), order=SimpleNamespace(submit=AsyncMock()),
            checkout=SimpleNamespace(validate=AsyncMock()), tools=SimpleNamespace(snapshot=AsyncMock()),
            page=SimpleNamespace(is_closed=lambda: False), auth_lock=asyncio.Lock(),
            store=SimpleNamespace(get=lambda _: None, event=lambda *args, **kwargs: events.append(args)))
        return session, events

    async def race(self, *, before_target=0, purchase_error=None, submit_error=None, cancel=False):
        session, events = self.make_session(before_target=before_target)
        lock = asyncio.Lock()
        selected, queued, other_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        trace = []

        async def select(**kwargs):
            session.cart_selection = {'sku': session.task.sku, 'quantity': 1}
            trace.append('selected')
            selected.set()
            # 确保另一个任务已经在底层 asyncio.Lock 的等待队列中。
            await queued.wait()

        async def other():
            await selected.wait()
            queued.set()
            async with lock:
                trace.append('other-refresh')
                other_entered.set()

        async def purchase(*args):
            trace.append('checkout')
            self.assertTrue(lock.locked())
            if purchase_error and trace.count('checkout') == 1:
                raise purchase_error
            # 模拟浏览器跨进程等待，其他任务仍不能插入刷新。
            await asyncio.sleep(.01)
            self.assertGreaterEqual(session.scheduler.now_ns(), round(session.task.target.timestamp() * 1e9))

        async def submit():
            trace.append('submit')
            if submit_error:
                raise submit_error
            session.submitted = True

        async def recover(*args):
            self.assertFalse(lock.locked())
            await asyncio.wait_for(other_entered.wait(), 1)
            trace.append('manual-resumed')

        async def snapshot(*args):
            self.assertFalse(lock.locked())
            await asyncio.wait_for(other_entered.wait(), 1)

        session.cart.select.side_effect = select
        session.order.submit.side_effect = submit
        session.tools.snapshot.side_effect = snapshot
        competitor = asyncio.create_task(other())
        task = None
        try:
            with patch('main.prepare_scheduled_cart', new=AsyncMock()), \
                    patch('main.purchase_attempt', side_effect=purchase), patch('main.recover', side_effect=recover):
                task = asyncio.create_task(run_one(session, lock))
                if cancel:
                    await asyncio.wait_for(selected.wait(), 1)
                    # armed 事件发生时已跨过 warm_cart，正在持锁等待目标时间。
                    while not any(event[1] == 'armed' for event in events):
                        await asyncio.sleep(0)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                elif submit_error:
                    with self.assertRaises(type(submit_error)):
                        await asyncio.wait_for(task, 2)
                else:
                    self.assertTrue(await asyncio.wait_for(task, 2))
            await asyncio.wait_for(competitor, 1)
        finally:
            for job in (task, competitor):
                if job is not None and not job.done():
                    job.cancel()
            await asyncio.gather(*(job for job in (task, competitor) if job is not None), return_exceptions=True)
        self.assertFalse(lock.locked())
        return trace, events

    async def test_late_selection_checks_out_before_already_queued_refresh(self):
        trace, events = await self.race()
        self.assertEqual(trace, ['selected', 'checkout', 'submit', 'other-refresh'])
        ready = next(event[2] for event in events if event[1] == 'cart_refresh')
        self.assertTrue(ready['selection_lock_retained'])

    async def test_early_selection_keeps_cart_reserved_until_target_and_submission(self):
        trace, _ = await self.race(before_target=.05)
        self.assertEqual(trace, ['selected', 'checkout', 'submit', 'other-refresh'])

    async def test_stop_while_waiting_for_target_releases_reservation(self):
        trace, _ = await self.race(before_target=.5, cancel=True)
        self.assertEqual(trace, ['selected', 'other-refresh'])

    async def test_terminal_submission_error_releases_reservation(self):
        trace, _ = await self.race(submit_error=StopOrder('模拟终止'))
        self.assertEqual(trace, ['selected', 'checkout', 'submit', 'other-refresh'])

    async def test_retryable_checkout_releases_lock_before_snapshot_and_retry(self):
        trace, _ = await self.race(purchase_error=Retryable('模拟暂时无货'))
        self.assertLess(trace.index('other-refresh'), trace.index('submit'))
        self.assertEqual(trace.count('checkout'), 2)

    async def test_manual_verification_releases_lock_before_waiting(self):
        trace, _ = await self.race(purchase_error=VerificationRequired('模拟人工验证'))
        self.assertLess(trace.index('other-refresh'), trace.index('manual-resumed'))
        self.assertEqual(trace.count('submit'), 1)

    async def test_selection_timeout_releases_lock_and_never_submits(self):
        session, _ = self.make_session()
        session.task.retry['timeout_seconds'] = .02
        lock = asyncio.Lock()
        async def slow_select(**kwargs):
            await asyncio.Event().wait()
        session.cart.select.side_effect = slow_select
        with patch('main.prepare_scheduled_cart', new=AsyncMock()), \
                self.assertRaisesRegex(StopOrder, '仍未勾选'):
            await asyncio.wait_for(run_one(session, lock), 1)
        self.assertFalse(lock.locked())
        session.order.submit.assert_not_awaited()

    async def test_cancel_during_submission_releases_lock(self):
        session, _ = self.make_session()
        lock = asyncio.Lock()
        submitting = asyncio.Event()
        async def select(**kwargs):
            session.cart_selection = {'sku': session.task.sku, 'quantity': 1}
        async def submit():
            submitting.set()
            await asyncio.Event().wait()
        session.cart.select.side_effect = select
        session.order.submit.side_effect = submit
        with patch('main.prepare_scheduled_cart', new=AsyncMock()), patch('main.purchase_attempt', new=AsyncMock()):
            job = asyncio.create_task(run_one(session, lock))
            try:
                await asyncio.wait_for(submitting.wait(), 1)
                self.assertTrue(lock.locked())
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
            finally:
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        self.assertFalse(lock.locked())
