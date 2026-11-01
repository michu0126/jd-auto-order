import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.errors import Retryable
from main import preload_product, prepare_scheduled_product, purchase_attempt
from tests.test_cart_prewarm import Clock


class ProductPrewarmTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, now=0):
        session = SimpleNamespace(
            task=SimpleNamespace(id='detail', auto_submit=True, mode='direct', retry={'interval_ms': 500}),
            cfg=SimpleNamespace(timing={'prewarm_seconds': 60}), scheduler=Clock(now),
            product=SimpleNamespace(open=AsyncMock(), read=AsyncMock(), specs=AsyncMock(),
                state=AsyncMock(), buy=AsyncMock()), product_prepared=False,
            tools=SimpleNamespace(guard=AsyncMock(), find=AsyncMock()),
            page=SimpleNamespace(url='https://item.jd.com/111.html', is_closed=lambda: False),
            auth_lock=asyncio.Lock(), login=SimpleNamespace(ensure=AsyncMock()),
            store=SimpleNamespace(event=lambda *args: None), order=SimpleNamespace(baseline=AsyncMock()),
            checkout=SimpleNamespace(select_payment=AsyncMock(), discounts=AsyncMock()))
        async def complete_purchase_navigation():
            session.page.url = 'https://trade.jd.com/shopping/order/getOrderInfo.action'
        session.product.buy.side_effect = complete_purchase_navigation
        session.cfg.jd = {'action_timeout_ms': 500}
        return session

    async def test_slow_title_wait_is_cancelled_at_preparation_deadline(self):
        session = self.make_session(29_950_000_000)
        cancelled = asyncio.Event()
        async def slow():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        session.product.open.side_effect = slow
        self.assertFalse(await preload_product(session, 30_000_000_000, refresh=True, attempts=1))
        self.assertTrue(cancelled.is_set())
        self.assertFalse(session.product_prepared)
        session.product.buy.assert_not_awaited()

    async def test_product_loads_immediately_then_checks_before_sale(self):
        session = self.make_session()
        await prepare_scheduled_product(session, 120_000_000_000)
        session.product.open.assert_awaited_once()
        self.assertEqual(session.product.specs.await_count, 4)
        session.login.ensure.assert_awaited_once_with(manual=False)
        session.order.baseline.assert_awaited_once()
        session.product.buy.assert_not_awaited()
        self.assertEqual(session.scheduler.now_ns(), 110_000_000_000)

    async def test_late_start_skips_expired_prewarm_stages(self):
        session = self.make_session(119_000_000_000)
        await prepare_scheduled_product(session, 120_000_000_000)
        session.product.open.assert_not_awaited()
        session.product.specs.assert_not_awaited()
        session.order.baseline.assert_not_awaited()

    async def test_failed_read_retries_are_bounded_and_do_not_buy(self):
        session = self.make_session()
        session.product.read.side_effect = Retryable('未找到 product_name')
        session.product.open.side_effect = Retryable('未找到 product_name')
        self.assertFalse(await preload_product(session, 1_000_000_000, attempts=2))
        self.assertEqual(session.product.read.await_count, 1)
        self.assertEqual(session.product.open.await_count, 1)
        session.product.buy.assert_not_awaited()

    async def test_first_attempt_reprepares_when_prewarm_failed_on_item_url(self):
        session = self.make_session()
        await purchase_attempt(session, False, 1)
        session.product.open.assert_awaited_once()
        session.product.specs.assert_awaited_once()
        session.product.buy.assert_awaited_once()

    async def test_first_attempt_reuses_verified_product_page(self):
        session = self.make_session()
        session.product_prepared = True
        await purchase_attempt(session, False, 1)
        session.product.open.assert_not_awaited()
        session.product.buy.assert_awaited_once()
