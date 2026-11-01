import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from core.browser_settings import resolve_endpoint, validate_endpoint
from core.config import Config
from core.errors import StopOrder
from jd.browser import launch


class BrowserSettingsTests(unittest.TestCase):
    def test_loopback_only_and_no_embedded_credentials(self):
        for value in ('auto', 'http://127.0.0.1:9222', 'ws://localhost:9222/devtools/browser',
                      'ws://[::1]:9222/devtools/browser/test-id'):
            self.assertEqual(validate_endpoint(value), value)
        for value in ('https://example.com:9222', 'http://127.0.0.1:9222@evil.test',
                      'ws://user:pass@localhost:9222/devtools/browser', 'ws://localhost:9222/unknown',
                      'http://localhost:9222/?token=secret', None, True):
            with self.subTest(value=value), self.assertRaises(StopOrder):
                validate_endpoint(value)

    def test_auto_discovers_only_the_debug_port_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = {'cdp_endpoint': 'auto', 'cdp_user_data_dir': folder}
            marker = Path(folder) / 'DevToolsActivePort'
            for data, expected in (('9222\n', 'ws://127.0.0.1:9222/devtools/browser'),
                                   ('9223\n/devtools/browser/local-test\n', 'ws://127.0.0.1:9223/devtools/browser/local-test')):
                marker.write_text(data)
                self.assertEqual(resolve_endpoint(settings), expected)
            for data in ('0', '65536', '9222\n/bad', 'not-a-port'):
                marker.write_text(data)
                with self.assertRaises(StopOrder):
                    resolve_endpoint(settings)
            marker.unlink()
            with self.assertRaisesRegex(StopOrder, 'chrome://inspect'):
                resolve_endpoint(settings)

    def test_platform_profiles_keep_their_existing_isolation(self):
        cfg = Config.parse(Path('config.yaml'), {'jd': {'browser_connection': 'existing'}, 'tasks': []})
        self.assertEqual(cfg.for_platform('jd').jd['browser_connection'], 'existing')
        self.assertEqual(cfg.for_platform('taobao').jd['browser_connection'], 'persistent')
        for values in ({'browser_connection': 'unknown'}, {'cdp_timeout_seconds': 0},
                       {'browser_connection': 'existing', 'browser': 'msedge'}):
            with self.assertRaises(StopOrder):
                Config.parse(Path('config.yaml'), {'jd': values, 'tasks': []})


class BrowserConnectionMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_mode_still_owns_and_closes_its_context(self):
        cfg = Config.parse(Path('config.yaml'), {'tasks': []})
        page = object()
        context = SimpleNamespace(set_default_timeout=lambda _: None, route=AsyncMock(),
                                  close=AsyncMock(), new_page=AsyncMock(return_value=page))
        chromium = SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context), connect_over_cdp=AsyncMock())
        connection = await launch(SimpleNamespace(chromium=chromium), cfg)
        self.assertFalse(connection.attached)
        self.assertIs(await connection.acquire_page(reuse_cart=True), page)
        await connection.close()
        context.close.assert_awaited_once()
        context.route.assert_awaited_once()
        chromium.connect_over_cdp.assert_not_awaited()

    async def test_connection_failure_never_launches_another_browser(self):
        cfg = Config.parse(Path('config.yaml'), {'jd': {'browser_connection': 'existing',
            'cdp_endpoint': 'http://127.0.0.1:9222'}, 'tasks': []})
        chromium = SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=OSError()), launch_persistent_context=AsyncMock())
        with self.assertRaisesRegex(StopOrder, '不会另开'):
            await launch(SimpleNamespace(chromium=chromium), cfg)
        chromium.launch_persistent_context.assert_not_awaited()

    async def test_uses_management_context_and_disconnects_without_closing_it(self):
        cfg = Config.parse(Path('config.yaml'), {'jd': {'browser_connection': 'existing',
            'cdp_endpoint': 'http://127.0.0.1:9222'}, 'tasks': []})
        unrelated = SimpleNamespace(pages=[])
        manager = SimpleNamespace(url='http://127.0.0.1:8765/', is_closed=lambda: False)
        context = SimpleNamespace(pages=[manager], set_default_timeout=lambda _: None, close=AsyncMock(), route=AsyncMock())
        browser = SimpleNamespace(contexts=[unrelated, context], close=AsyncMock())
        connection = await launch(SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser))), cfg)
        self.assertIs(connection.context, context)
        await connection.close()
        browser.close.assert_awaited_once()
        context.close.assert_not_awaited()
        context.route.assert_not_awaited()


class AttachedBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.p = await async_playwright().start()
        self.owner = await self.p.chromium.launch_persistent_context(self.temp.name,
            channel='chrome', headless=True, args=['--remote-debugging-port=0'])
        await self.owner.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<html><body>local fixture</body></html>'))
        self.manager = await self.owner.new_page()
        await self.manager.goto('http://127.0.0.1:8765/')
        self.cart = await self.owner.new_page()
        await self.cart.goto('https://cart.jd.com/cart_index')
        await self.cart.evaluate("window.fixtureMarker='original-cart'")
        await self.owner.add_cookies([{'name': 'pt_key', 'value': 'local-test-only', 'domain': '.jd.com', 'path': '/'}])
        endpoint = resolve_endpoint({'cdp_endpoint': 'auto', 'cdp_user_data_dir': self.temp.name})
        self.cfg = Config.parse(Path(self.temp.name) / 'config.yaml', {'jd': {
            'browser_connection': 'existing', 'cdp_endpoint': endpoint}, 'tasks': []})
        self.connection = None

    async def asyncTearDown(self):
        if self.connection is not None and self.connection.browser.is_connected():
            await self.connection.close()
        await self.owner.close()
        await self.p.stop()
        self.temp.cleanup()

    async def test_reuses_cart_and_session_then_leaves_all_user_tabs_open(self):
        self.connection = await launch(self.p, self.cfg)
        before = len(self.owner.pages)
        page = await self.connection.acquire_page(reuse_cart=True)
        self.assertEqual(await page.evaluate('window.fixtureMarker'), 'original-cart')
        self.assertEqual(len(self.owner.pages), before)
        self.assertTrue(any(c['value'] == 'local-test-only' for c in await self.connection.cookies('https://cart.jd.com')))
        self.connection.release_page(page)
        self.assertIs(await self.connection.acquire_page(reuse_cart=True), page)
        await self.connection.close()
        self.assertTrue(self.owner.browser.is_connected())
        self.assertFalse(self.cart.is_closed())
        self.assertFalse(self.manager.is_closed())
        self.assertEqual(await self.cart.evaluate('window.fixtureMarker'), 'original-cart')

    async def test_ambiguous_cart_tabs_are_not_guessed(self):
        second = await self.owner.new_page()
        await second.goto('https://cart.jd.com/cart_index?second=1')
        self.connection = await launch(self.p, self.cfg)
        with self.assertRaisesRegex(StopOrder, '多个购物车'):
            await self.connection.acquire_page(reuse_cart=True)
        self.assertFalse(self.cart.is_closed())

    async def test_execute_dry_run_keeps_the_borrowed_browser_and_management_tab(self):
        from datetime import datetime, timedelta
        from core.config import CST
        from main import execute
        from tests.test_browser import CART, CHECKOUT
        await self.owner.unroute('**/*')
        async def route(request):
            path = urlsplit(request.request.url).path
            if path == '/cart_index':
                html = CART
            elif path == '/shopping/order/getOrderInfo.action':
                html = CHECKOUT
            elif path == '/':
                html = '<html><body><a class="nickname">本地模拟账号</a></body></html>'
            else:
                return await request.abort()
            await request.fulfill(content_type='text/html; charset=utf-8', body=html)
        await self.owner.route('**/*', route)
        await self.owner.add_cookies([{'name': 'pt_pin', 'value': 'local-test-only', 'domain': '.jd.com', 'path': '/'}])
        await self.cart.reload()
        cfg = Config.parse(Path(self.temp.name) / 'config.yaml', {'jd': self.cfg.jd | {
            'login_settle_seconds': .2, 'login_check_timeout_seconds': 1, 'payment_preference': {'channel': 'default'}},
            'timing': {'sync_clock': False}, 'notification': {'desktop': False}, 'logging': {'save_screenshots': False},
            'retry': {'max_attempts': 1, 'timeout_seconds': 7},
            'tasks': [{'id': 'borrowed-cart', 'sku_id': '111111111', 'name': '手机', 'quantity': 2,
                'source': 'cart', 'purchase_mode': 'cart', 'max_price': 220, 'address_keyword': '测试街道',
                'target_time': (datetime.now(CST) + timedelta(minutes=5)).isoformat()}]})
        args = SimpleNamespace(command='run', task=None, platform='jd', now=False, dry_run=True, noninteractive=True)
        result = await execute(cfg, args)
        self.assertEqual(result, 0, f"fixture page: {self.cart.url} / {(await self.cart.locator('body').inner_text())[:400]}")
        self.assertTrue(self.owner.browser.is_connected())
        self.assertFalse(self.manager.is_closed())
        self.assertEqual(self.manager.url, 'http://127.0.0.1:8765/')
        self.assertIn('/shopping/order/getOrderInfo.action', self.cart.url)
