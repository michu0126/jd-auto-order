import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright.async_api import TimeoutError as PlaywrightTimeout

from core.errors import Cancelled, Retryable, StopOrder, VerificationRequired
from main import preload_cart, prepare_scheduled_cart, warm_cart


class Clock:
    def __init__(self, now):
        self.now = now
        self.stopped = False

    def now_ns(self):
        return self.now

    def check_stop(self):
        if self.stopped:
            raise Cancelled("用户已停止任务")

    async def wait(self, target):
        self.check_stop()
        self.now = max(self.now, target)


class CartPrewarmTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, now):
        events = []
        session = SimpleNamespace(
            cfg=SimpleNamespace(timing={"cart_refresh_interval_ms": 200, "cart_refresh_start_before_seconds": 2,
                "cart_preload_timeout_seconds": 15, "prewarm_seconds": 60}),
            task=SimpleNamespace(id="test", sku="111", quantity=1, auto_submit=True,
                retry={"timeout_seconds": 2, "max_attempts": 1}), scheduler=Clock(now),
            cart=SimpleNamespace(select=AsyncMock(), preload=AsyncMock()), cart_selection=None, cart_preloaded=False,
            login=SimpleNamespace(ensure=AsyncMock()), order=SimpleNamespace(baseline=AsyncMock()),
            auth_lock=asyncio.Lock(), page=SimpleNamespace(is_closed=lambda: False),
            store=SimpleNamespace(event=lambda *args, **kwargs: events.append(args)))
        return session, events

    async def test_cart_prepares_immediately_then_at_sixty_and_thirty_seconds(self):
        session, events = self.make_session(0)
        loads = []
        async def preload(refresh):
            loads.append((session.scheduler.now_ns(), refresh))
            session.cart_preloaded = True
        session.cart.preload.side_effect = preload
        await prepare_scheduled_cart(session, asyncio.Lock(), 120_000_000_000)
        self.assertEqual(loads, [(0, True), (60_000_000_000, False), (90_000_000_000, True)])
        session.login.ensure.assert_awaited_once_with(manual=False)
        session.order.baseline.assert_awaited_once()
        session.cart.select.assert_not_awaited()
        self.assertFalse(any(event[1] in ('failed', 'attempt', 'trigger') for event in events))

    async def test_preload_timeouts_keep_task_waiting(self):
        session, events = self.make_session(0)
        session.cart.preload.side_effect = PlaywrightTimeout('页面仍在加载')
        self.assertFalse(await preload_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertEqual(session.cart.preload.await_count, 3)
        self.assertFalse(session.cart_preloaded)
        session.cart.select.assert_not_awaited()
        self.assertEqual({event[1] for event in events}, {'preloading'})

    async def test_preload_navigation_is_cancelled_before_polling_window(self):
        session, events = self.make_session(27_950_000_000)
        cancelled = asyncio.Event()
        async def load(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        session.cart.preload.side_effect = load
        lock = asyncio.Lock()
        self.assertFalse(await preload_cart(session, lock, 28_000_000_000, attempts=1))
        self.assertTrue(cancelled.is_set())
        self.assertFalse(lock.locked())

    async def test_order_baseline_cannot_consume_preload_window(self):
        session, _ = self.make_session(0)
        session.cart.preload.side_effect = lambda **kwargs: setattr(session, 'cart_preloaded', True)
        async def baseline():
            # 只剩很短的基线读取预算，用真实短超时验证取消。
            await asyncio.Event().wait()
        session.order.baseline.side_effect = baseline
        async def ensure(**kwargs):
            session.scheduler.now = 89_950_000_000
        session.login.ensure.side_effect = ensure
        await prepare_scheduled_cart(session, asyncio.Lock(), 120_000_000_000)
        self.assertEqual(session.cart.preload.await_count, 3)
        self.assertEqual(session.scheduler.now_ns(), 90_000_000_000)

    async def test_preloaded_page_is_reused_for_first_poll(self):
        session, _ = self.make_session(0)
        session.cart_preloaded = True
        async def select(refresh):
            self.assertFalse(refresh)
            session.cart_selection = {'sku': '111', 'quantity': 1}
        session.cart.select.side_effect = select
        self.assertTrue(await warm_cart(session, asyncio.Lock(), 30_000_000_000))

    async def test_slow_presale_request_does_not_block_sale_time_poll(self):
        session, _ = self.make_session(29_950_000_000)
        calls = []
        async def select(**kwargs):
            calls.append(session.scheduler.now_ns())
            if len(calls) == 1:
                await asyncio.Event().wait()
            session.cart_selection = {'sku': '111', 'quantity': 1}
        session.cart.select.side_effect = select
        self.assertTrue(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertEqual(calls, [29_950_000_000, 30_000_000_000])

    async def test_unavailable_cart_is_reloaded_until_ready_without_checkout(self):
        session, events = self.make_session(22_000_000_000)
        calls = []

        async def select(refresh):
            self.assertTrue(refresh)
            calls.append(session.scheduler.now_ns())
            if len(calls) < 3:
                raise Retryable("等待抢购")
            session.cart_selection = {"sku": "111", "quantity": 1}

        session.cart.select.side_effect = select
        self.assertTrue(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertEqual(calls, [28_000_000_000, 28_200_000_000, 28_400_000_000])
        self.assertEqual(session.scheduler.now_ns(), 28_400_000_000)
        self.assertEqual([entry[2]["ready"] for entry in events], [False, False, True])

    async def test_playwright_timeout_does_not_abort_prewarm_or_leave_stale_proof(self):
        session, events = self.make_session(24_000_000_000)
        session.cart_selection = {"sku": "111", "quantity": 1}
        session.cart.select.side_effect = PlaywrightTimeout("Locator.set_checked: Timeout 5000ms exceeded.")
        self.assertFalse(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertIsNone(session.cart_selection)
        self.assertEqual(len(events), 20)
        self.assertEqual(session.scheduler.now_ns(), 32_000_000_000)

    async def test_slow_refresh_is_cancelled_at_task_deadline(self):
        session, events = self.make_session(31_950_000_000)
        cancelled = asyncio.Event()

        async def never_finishes(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session.cart.select.side_effect = never_finishes
        lock = asyncio.Lock()
        self.assertFalse(await warm_cart(session, lock, 30_000_000_000))
        self.assertTrue(cancelled.is_set())
        self.assertFalse(lock.locked())
        self.assertEqual(len(events), 1)

    async def test_busy_account_lock_is_bounded_by_task_deadline(self):
        session, events = self.make_session(31_950_000_000)
        lock = asyncio.Lock()
        await lock.acquire()
        self.assertFalse(await warm_cart(session, lock, 30_000_000_000))
        session.cart.select.assert_not_awaited()
        self.assertTrue(lock.locked())  # 不释放其他任务持有的锁。
        lock.release()

    async def test_stop_and_closed_browser_are_not_silently_retried(self):
        session, _ = self.make_session(22_000_000_000)
        session.scheduler.stopped = True
        with self.assertRaises(Cancelled):
            await warm_cart(session, asyncio.Lock(), 30_000_000_000)
        session.cart.select.assert_not_awaited()
        session.scheduler.stopped = False
        session.page.is_closed = lambda: True
        session.cart.select.side_effect = PlaywrightTimeout("closed")
        with self.assertRaisesRegex(StopOrder, "浏览器页面已关闭"):
            await warm_cart(session, asyncio.Lock(), 30_000_000_000)

    async def test_late_start_after_deadline_does_not_refresh(self):
        session, _ = self.make_session(33_000_000_000)
        self.assertFalse(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        session.cart.select.assert_not_awaited()

    async def test_verification_pauses_polling_and_releases_cart_lock(self):
        session, events = self.make_session(30_000_000_000)
        entered, released = asyncio.Event(), asyncio.Event()
        lock = asyncio.Lock()
        async def select(**kwargs):
            if session.cart.select.await_count == 1:
                raise VerificationRequired('京东显示快速验证')
            session.cart_selection = {'sku': '111', 'quantity': 1}
        async def wait_manual(reason, login):
            self.assertFalse(lock.locked())
            self.assertFalse(login)
            entered.set()
            await released.wait()
        session.cart.select.side_effect = select
        session.login.wait_manual = AsyncMock(side_effect=wait_manual)
        job = asyncio.create_task(warm_cart(session, lock, 30_000_000_000))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(.05)
            self.assertEqual(session.cart.select.await_count, 1)
            self.assertFalse(job.done())
        finally:
            released.set()
        self.assertTrue(await job)
        self.assertEqual(session.cart.select.await_count, 2)
        self.assertIn('快速验证', events[0][2]['result'])

    async def test_verification_completion_after_cutoff_does_not_extend_purchase_window(self):
        session, events = self.make_session(30_000_000_000)
        session.cart.select.side_effect = VerificationRequired('京东显示快速验证')
        async def wait_manual(*args, **kwargs):
            session.scheduler.now = 33_000_000_000
        session.login.wait_manual = AsyncMock(side_effect=wait_manual)
        self.assertFalse(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertEqual(session.cart.select.await_count, 1)
        self.assertIsNone(session.cart_selection)

    async def test_keeps_polling_after_sale_beyond_checkout_attempt_limit(self):
        session, events = self.make_session(22_000_000_000)
        calls = []
        async def select(**kwargs):
            calls.append(session.scheduler.now_ns())
            if session.scheduler.now_ns() < 30_600_000_000:
                raise Retryable("尚未放货")
            session.cart_selection = {"sku": "111", "quantity": 1}
        session.cart.select.side_effect = select
        self.assertTrue(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertEqual(len(calls), 14)
        self.assertGreater(len(calls), session.task.retry["max_attempts"])
        self.assertEqual(calls[-1], 30_600_000_000)
        self.assertTrue(events[-1][2]["ready"])

    async def test_selection_with_wrong_quantity_is_not_success(self):
        session, events = self.make_session(28_000_000_000)
        async def select(**kwargs):
            session.cart_selection = {"sku": "111", "quantity": 2 if not events else 1}
        session.cart.select.side_effect = select
        self.assertTrue(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertEqual([event[2]["ready"] for event in events], [False, True])

    async def test_selection_completing_after_deadline_is_not_used(self):
        session, events = self.make_session(31_900_000_000)
        async def select(**kwargs):
            session.scheduler.now = 32_000_000_000
            session.cart_selection = {"sku": "111", "quantity": 1}
        session.cart.select.side_effect = select
        self.assertFalse(await warm_cart(session, asyncio.Lock(), 30_000_000_000))
        self.assertIsNone(session.cart_selection)
