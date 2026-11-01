import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from core.config import Config
from core.platforms import DOMAINS
from core.scheduler import Scheduler
from core.state_machine import StateStore
from core.errors import Retryable, StopOrder
from marketplaces.web import WebPurchase
from marketplaces.taobao_product import inspect_tmall_product


class MarketplaceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.domains = patch.dict(DOMAINS, {"taobao": ("marketplace.test",)})
        self.domains.start()
        self.cfg = Config.parse(Path(self.temp.name) / "config.yaml", {
            "jd": {"action_timeout_ms": 500, "result_timeout_seconds": 1},
            "notification": {"desktop": False}, "logging": {"save_screenshots": False},
            "tasks": [{"id": "web", "platform": "taobao", "name": "模拟眼镜", "quantity": 1,
                       "product_url": "https://marketplace.test/product?id=123456789", "target_time": "2026-11-11 10:00:00",
                       "sku_options": {"尺码": "M"}, "auto_submit": True}],
            "platforms": {"taobao": {"login_url": "https://marketplace.test/login", "checkout_paths": ["/checkout"],
                "result_paths": ["/result"], "selectors": {key: "." + key for key in
                    ("auth_user", "product_name", "buy_now", "checkout_root", "checkout_item", "checkout_item_id",
                     "checkout_quantity", "total", "address", "submit", "order_id", "order_status", "spec_尺码",
                     "selected_尺码", "checkout_spec_尺码")}}}})
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel="chrome", headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.hits = 0
        self.ambiguous = False
        self.wrong_item = False
        self.variant = "6313432066497"
        await self.context.route("**/*", self.route)
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.scheduler = Scheduler(precision=False)
        self.flow = WebPurchase(self.context, self.page, self.cfg.for_platform("taobao"), self.cfg.tasks[0], self.store, self.scheduler)

    async def route(self, route):
        parsed = urlparse(route.request.url)
        if parsed.hostname != "marketplace.test":
            return await route.abort()
        html = '<div class="auth_user">模拟用户</div>'
        if parsed.path == "/product":
            html += '<h1 class="product_name">模拟眼镜</h1><button class="spec_尺码" onclick="this.classList.add(\'selected_尺码\')">M</button><a class="buy_now" href="/checkout">立即购买</a>'
        elif parsed.path == "/checkout":
            identifier = "987654321" if self.wrong_item else "123456789"
            html += '<main class="checkout_root"><article class="checkout_item"><span class="checkout_item_id">' + identifier + '</span><span class="checkout_variant_id">' + self.variant + '</span><span class="checkout_quantity">×1</span><span class="checkout_spec_尺码">M</span></article><span class="total">￥199.00</span><div class="address">模拟收货地址</div><a class="submit" href="/result">提交订单</a></main>'
        elif parsed.path == "/result":
            self.hits += 1
            html += '<p>响应暂未确认</p>' if self.ambiguous else '<p class="order_status">待付款</p><span class="order_id">123456789012345</span>'
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    async def asyncTearDown(self):
        self.store.close()
        self.scheduler.close()
        await self.context.close()
        await self.browser.close()
        await self.p.stop()
        self.domains.stop()
        self.temp.cleanup()

    async def test_configured_flow_submits_once_without_payment(self):
        self.assertTrue(await self.flow.run(asyncio.Lock(), now=True))
        self.assertEqual(self.hits, 1)
        self.assertEqual(self.store.get(self.flow.key)[0], "confirmed")
        with self.assertRaises(StopOrder):
            await self.flow.run(asyncio.Lock(), now=True)
        self.assertEqual(self.hits, 1)

    async def test_dry_run_never_submits(self):
        self.assertTrue(await self.flow.run(asyncio.Lock(), dry_run=True))
        self.assertEqual(self.hits, 0)
        self.assertIsNone(self.store.get(self.flow.key))

    async def test_wrong_item_blocks_submission(self):
        self.wrong_item = True
        with self.assertRaisesRegex(StopOrder, "真实商品 ID"):
            await self.flow.run(asyncio.Lock(), now=True)
        self.assertEqual(self.hits, 0)

    async def test_unknown_result_is_durable_and_not_retried(self):
        self.ambiguous = True
        with self.assertRaises(StopOrder):
            await self.flow.run(asyncio.Lock(), now=True)
        self.assertEqual(self.hits, 1)
        self.assertEqual(self.store.get(self.flow.key)[0], "unknown")

    async def test_variant_without_size_requires_exact_checkout_sku(self):
        self.flow.task = replace(self.flow.task, sku_options={}, variant_id=self.variant)
        self.flow.profile["selectors"]["checkout_variant_id"] = ".checkout_variant_id"
        self.variant = "6313432066498"
        with self.assertRaisesRegex(StopOrder, "SKU ID 与任务不一致"):
            await self.flow.run(asyncio.Lock(), now=True)
        self.assertEqual(self.hits, 0)
        self.variant = self.flow.task.variant_id
        self.assertTrue(await self.flow.run(asyncio.Lock(), dry_run=True))
        self.assertEqual(self.hits, 0)

    async def test_observed_tmall_fixture_has_no_size_or_verified_variant(self):
        # Sanitized structure observed on the allowed product page; never contacts Tmall.
        html = '''<title>测试眼镜-tmall.com天猫</title><div class="site-nav-login-info-nick">模拟用户</div>
        <section id="SkuPanel_tbpcDetail_ssr2025"><div id="skuOptionsArea">
        <div class="skuItem--sample"><label class="ItemLabel--sample">商品规格</label>
        <div data-vid="-3" data-disabled="false" class="valueItem--sample isSelected--sample">限定版
        <span data-vid="-3" class="valueItemImgWrap--sample"></span></div></div></div>
        <input class="countValue--sample" value="1"><footer id="tbpcDetail_SkuPanelFoot">
        <button>加入购物车</button><button disabled>即将开始，10.26 10:00开售</button></footer></section>'''
        await self.page.route("https://detail.tmall.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        await self.page.goto("https://detail.tmall.com/item.htm?id=1094601672942&skuId=6313432066497")
        report = await inspect_tmall_product(self.page)
        self.assertEqual(report["product_id"], "1094601672942")
        self.assertEqual(report["requested_variant_id"], "6313432066497")
        self.assertFalse(report["variant_verified"])
        self.assertFalse(report["size_available"])
        self.assertFalse(report["automatic_checkout_verified"])
        self.assertTrue(report["authenticated"])
        self.assertEqual(report["quantity"], "1")
        self.assertEqual(report["purchase_state"], "not_started")
        self.assertEqual(len(report["options"]), 1)
        self.assertTrue(report["options"][0]["selected"])
        self.flow.task = replace(self.flow.task, sku="1094601672942", name="测试眼镜",
                                 product_link=self.page.url, sku_options={})
        with patch.dict(DOMAINS, {"taobao": ("tmall.com",)}):
            with self.assertRaisesRegex(Retryable, "尚未开售"):
                await self.flow.prepare()
        self.assertEqual(self.hits, 0)
