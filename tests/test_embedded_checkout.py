"""轻量结算形状来自只读页面；所有商品、金额和请求响应均为本地虚构。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from core.errors import StopOrder
from jd.checkout import Quote
from jd.embedded_checkout import HOST, EmbeddedOrder, FrameSurface, OrderOnlyGuard, bind_embedded
from main import run_one
from tests import test_browser as fixtures


LITE = '''<html><body><div id="settlementLite"><div class="consignee_card_addressDetail-local">测试街道</div>
<div class="sku-list"><div class="sku-item"><div class="goods-local"><div class="title-local">
<span><div class="text-ellipsis text-local">测试手机 黑色 256GB</div></span></div>
<div class="sku-local">黑色</div><input class="number-display" type="number" value="2"></div></div></div>
<input class="number-display" type="number" value="0">
<div class="bill-item"><div class="bill-item__title"><em>商品总额</em></div><div class="bill-item__price">￥200.00</div></div>
<div class="bill-item"><div class="bill-item__title"><em>共减</em></div><div class="bill-item__price">-￥1.00</div></div>
<div class="submitOrder-local"><div class="priceInfo-local"><span class="price-local">199.00</span></div>
<div class="submit-local" onclick="submitNative()">立即支付</div></div></div>
<script>async function submitNative(){
 const r=await fetch('https://api.m.jd.com/client.action?functionId=balance_submitOrder_pc',{
 method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'body=native-fixture-only'});
 const result=await r.json();
 if(result.body.resultFlag) await fetch('https://api.m.jd.com/client.action?functionId=platJDPayAcc',{
 method:'POST',body:'fixture-payment-must-not-leave-browser'});
}</script></body></html>'''


class EmbeddedCheckoutTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BrowserTests.asyncSetUp
    asyncTearDown = fixtures.BrowserTests.asyncTearDown
    credentials = fixtures.BrowserTests.credentials

    async def configure(self):
        self.cfg.jd.update(action_timeout_ms=5000, result_timeout_seconds=2)
        self.context.set_default_timeout(5000)
        self.session.task = replace(self.session.task, retry=dict(self.session.task.retry, timeout_seconds=30))
        self.lite_html = LITE
        self.payment_requests, self.native_requests = [], []
        self.frame_url = 'https://' + HOST + '/?topUrl=https%3A%2F%2Fitem.jd.com%2F' + fixtures.SKU + '.html'
        self.product_html = fixtures.PRODUCT.replace('href="https://trade.jd.com/shopping/order/getOrderInfo.action"',
            'href="#" onclick="document.querySelector(\'iframe\').src=\'' + self.frame_url + '\';return false"')
        self.product_html += '<iframe style="width:1000px;height:800px" src="about:blank"></iframe>'
        await self.context.unroute('**/*')
        await self.context.route('**/*', self.route)

    async def route(self, route):
        if urlsplit(route.request.url).hostname == HOST:
            return await route.fulfill(content_type='text/html; charset=utf-8', body=self.lite_html)
        if urlsplit(route.request.url).hostname == 'api.m.jd.com':
            self.payment_requests.append(route.request.url)
            return await route.fulfill(body='{}', headers={'Access-Control-Allow-Origin': '*'})
        await fixtures.BrowserTests.route(self, route)

    async def test_preview_reads_iframe_exact_goods_quantity_amount_without_submission(self):
        await self.configure()
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertIsInstance(self.session.page, FrameSurface)
        self.assertEqual(self.session.checkout_quote.total, Decimal('199'))
        self.assertEqual(self.session.checkout_quote.subtotal, Decimal('200'))
        self.assertIsNone(self.session.checkout_quote.freight)
        self.assertEqual(self.payment_requests, [])
        self.assertFalse(self.session.submitted)

    async def test_changed_variant_and_recommendation_are_rejected(self):
        await self.configure()
        self.lite_html = LITE.replace('<div class="sku-local">黑色</div>', '<div class="sku-local">白色</div>')
        with self.assertRaisesRegex(StopOrder, '名称/规格'):
            await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.lite_html = LITE.replace('type="number" value="0"', 'type="number" value="1"')
        await self.session.product.open()
        with self.assertRaisesRegex(StopOrder, '推荐加购商品'):
            await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertFalse(self.payment_requests)

    async def test_frame_top_product_must_match_original_task(self):
        await self.configure()
        await self.page.goto(self.frame_url.replace(fixtures.SKU, '999999999'))
        # 顶层并非受支持的商品入口，不可冒充某个任务的内嵌结算。
        self.assertFalse(await bind_embedded(self.session))
        await self.page.goto('https://item.jd.com/' + fixtures.SKU + '.html')
        await self.page.locator('iframe').evaluate('(e,url)=>e.src=url', self.frame_url.replace(fixtures.SKU, '999999999'))
        await self.page.frame_locator('iframe').locator('#settlementLite').wait_for()
        with self.assertRaisesRegex(StopOrder, '来源与任务商品不一致'):
            await bind_embedded(self.session)

    async def test_parent_overlay_cannot_be_clicked_through_from_iframe(self):
        await self.configure()
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.session.concurrent_ui = True
        await self.page.evaluate("""() => {const e=document.createElement('div');
            e.style='position:fixed;inset:0;background:white;z-index:99999';document.body.append(e)}""")
        from core.errors import Retryable
        with self.assertRaisesRegex(Retryable, '被遮挡'):
            await self.session.tools.activate(self.session.page.locator('[class^="submit-"]'))
        self.assertFalse(self.payment_requests)

    async def test_multiple_embedded_detail_tasks_stop_before_order_intent(self):
        await self.configure()
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.session.parallel_detail_count = 2
        with self.assertRaisesRegex(StopOrder, '已观察到串商品'):
            await self.session.order.submit()
        self.assertFalse(self.payment_requests)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_native_order_response_is_persisted_and_payment_is_blocked(self):
        await self.configure()
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        payload = json.dumps({'code': '0', 'body': {'resultFlag': True,
            'order': {'orderIdStr': '987654321012', 'orderPrice': '199.00'}}}).encode()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        async def forward(guard, route):
            self.native_requests.append(route.request.url)
            return await self.context.request.get('http://127.0.0.1:' + str(server.server_port))
        try:
            with patch.object(OrderOnlyGuard, 'forward', forward):
                result = await self.session.order.submit()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result, '987654321012')
        self.assertEqual(len(self.native_requests), 1)
        self.assertEqual(self.payment_requests, [])
        self.assertEqual(self.store.get(self.session.key)[:2], ('confirmed', '987654321012'))
        self.assertTrue(self.page.is_closed())
        self.assertGreater(self.session.order.payment_guard.blocked, 0)


class OrderOnlyGuardTests(unittest.IsolatedAsyncioTestCase):
    def guard(self):
        owner = SimpleNamespace()
        frame = SimpleNamespace(page=owner)
        session = SimpleNamespace(page=SimpleNamespace(owner=owner, frame=frame),
            checkout_quote=Quote(Decimal('199'), None, None, ''),
            store=SimpleNamespace(event=lambda *a, **kw: None), task=SimpleNamespace(id='test'))
        guard = OrderOnlyGuard(session)
        guard.owns = AsyncMock(return_value=True)
        return guard, frame

    async def test_native_forwarding_disables_redirects_and_transport_retries(self):
        guard, _ = self.guard()
        route = SimpleNamespace(fetch=AsyncMock(return_value='response'))
        self.assertEqual(await guard.forward(route), 'response')
        route.fetch.assert_awaited_once_with(max_retries=0, max_redirects=0, timeout=20000)

    async def test_unmatched_amount_never_confirms_and_duplicate_post_is_blocked(self):
        guard, frame = self.guard()
        req = SimpleNamespace(url='https://api.m.jd.com/client.action?functionId=balance_submitOrder_pc',
            method='POST', post_data='body=fixture', frame=frame)
        response = SimpleNamespace(json=AsyncMock(return_value={'code':'0','body':{'resultFlag':True,
            'order':{'orderIdStr':'123456789012','orderPrice':'200.00'}}}))
        guard.forward = AsyncMock(return_value=response)
        route = SimpleNamespace(request=req, fulfill=AsyncMock(), abort=AsyncMock(), fallback=AsyncMock())
        await guard.handle(route)
        self.assertIsNone(guard.accepted)
        await guard.handle(route)
        self.assertEqual(guard.forward.await_count, 1)
        route.abort.assert_awaited_once()

    async def test_payment_and_post_order_get_are_blocked_but_other_tabs_are_untouched(self):
        guard, frame = self.guard()
        route = SimpleNamespace(request=SimpleNamespace(url='https://api.m.jd.com/?functionId=platJDPayAcc',
            method='POST', post_data='', frame=frame), abort=AsyncMock(), fallback=AsyncMock())
        await guard.handle(route)
        self.assertEqual(route.abort.await_count, 1)
        guard.order_response_received = True
        route.request.method = 'GET'
        await guard.handle(route)
        self.assertEqual(route.abort.await_count, 2)
        guard.owns = AsyncMock(return_value=False)
        await guard.handle(route)
        route.fallback.assert_awaited_once()
