"""一个购物车、一个详情页独立下单；重叠的同路径任务启动前拒绝。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from core.config import CST, Config
from core.errors import StopOrder
from core.purchase_lock import validate_task_concurrency
from core.state_machine import StateStore
from jd.embedded_checkout import HOST, OrderOnlyGuard
from main import execute
from tests import test_browser as fixtures
from tests.test_embedded_checkout import LITE


def config(folder):
    return Config.parse(Path(folder) / 'config.yaml', {'tasks': [
        {'id': name, 'sku_id': sku, 'target_time': '2026-11-01 10:00:00',
         'source': source, 'purchase_mode': mode, 'quantity': qty, 'sku_options': options}
        for name, sku, source, mode, qty, options in (
            ('cart-task', fixtures.SKU, 'cart', 'cart', 2, {}),
            ('detail-task', '222222222', 'manual', 'direct', 1, {'选择颜色': '黑色'}))],
        'jd': {'headless': True, 'action_timeout_ms': 5000, 'result_timeout_seconds': 3,
            'login_settle_seconds': .2, 'keep_browser_open': False},
        'timing': {'sync_clock': False}, 'retry': {'max_attempts': 1, 'timeout_seconds': 30},
        'notification': {'desktop': False}, 'logging': {'save_screenshots': False}})


class ConcurrencyPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_cart_and_one_detail_are_preserved_as_two_individual_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            validate_task_concurrency(cfg.tasks)
            self.assertEqual([t.id for t in cfg.tasks], ['cart-task', 'detail-task'])
            self.assertEqual([t.quantity for t in cfg.tasks], [2, 1])

    async def test_same_path_conflicts_stop_before_browser_launch(self):
        for mode in ('cart', 'direct'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                cfg = config(folder)
                cfg.tasks.append(replace(cfg.tasks[0 if mode == 'cart' else 1], id='conflict', sku='333333333'))
                args = SimpleNamespace(command='run', now=True, dry_run=False, task=None, platform='jd', noninteractive=True)
                with patch('main.launch', new_callable=AsyncMock) as launch:
                    with self.assertRaisesRegex(StopOrder, '不会合并商品或自动排队'):
                        await execute(cfg, args)
                    launch.assert_not_awaited()

    async def test_prewarm_overlap_is_rejected_and_separated_times_are_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            first = config(folder).tasks[0]
            second = replace(first, id='later', target=first.target + timedelta(seconds=45))
            with self.assertRaisesRegex(StopOrder, '预热或购买窗口重叠'):
                validate_task_concurrency([first, second])
            second.target += timedelta(minutes=2)
            validate_task_concurrency([first, second])
            with self.assertRaises(StopOrder):
                validate_task_concurrency([first, second], immediate=True)

    async def test_daily_conflict_on_later_date_and_midnight_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            first = replace(config(folder).tasks[0], recurrence='daily')
            later = replace(first, id='once-later', recurrence='once', target=first.target + timedelta(days=4))
            with self.assertRaises(StopOrder):
                validate_task_concurrency([first, later])
            first.target = datetime(2026, 11, 1, 23, 59, 50, tzinfo=CST)
            later = replace(first, id='midnight', target=datetime(2026, 11, 2, 0, 0, 5, tzinfo=CST))
            with self.assertRaises(StopOrder):
                validate_task_concurrency([first, later])


class TwoLaneBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BrowserTests.asyncSetUp
    asyncTearDown = fixtures.BrowserTests.asyncTearDown
    credentials = fixtures.BrowserTests.credentials
    route = fixtures.BrowserTests.route

    async def run_two(self, embedded=False):
        cfg = config(self.root)
        self.context.set_default_timeout(5000)
        self.cfg = cfg
        arrived, ready, products = set(), asyncio.Event(), {}
        payment_leaks, forwarded = [], []
        detail_frame_url = 'https://' + HOST + '/?topUrl=https%3A%2F%2Fitem.jd.com%2F222222222.html'
        async def rendezvous(label):
            arrived.add(label)
            if len(arrived) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
        async def route(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname == 'item.jd.com':
                products[route.request.frame.page] = '222222222'
                html = fixtures.PRODUCT.replace(fixtures.SKU, '222222222')
                if embedded:
                    html = html.replace('href="https://trade.jd.com/shopping/order/getOrderInfo.action"',
                        'href="#" onclick="document.querySelector(\'iframe\').src=\'' + detail_frame_url + '\';return false"')
                    html += '<iframe style="width:1000px;height:800px" src="about:blank"></iframe>'
                return await route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if parsed.hostname == HOST:
                await rendezvous('detail-task')
                return await route.fulfill(content_type='text/html; charset=utf-8', body=LITE.replace('value="2"','value="1"'))
            if parsed.path == '/shopping/order/getOrderInfo.action':
                sku = products.get(route.request.frame.page)
                await rendezvous('detail-task' if sku else 'cart-task')
                html = fixtures.CHECKOUT.replace(fixtures.SKU, sku).replace('x2','x1') if sku else fixtures.CHECKOUT
                return await route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if parsed.hostname == 'api.m.jd.com':
                payment_leaks.append(route.request.url)
                return await route.fulfill(body='{}')
            await fixtures.BrowserTests.route(self, route)
        await self.context.unroute('**/*')
        await self.context.route('**/*', route)
        payload = json.dumps({'code':'0','body':{'resultFlag':True,
            'order':{'orderIdStr':'987654321012','orderPrice':'199.00'}}}).encode()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Access-Control-Allow-Origin','*')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self,*args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        async def forward(guard,route):
            forwarded.append(route.request.url)
            return await self.context.request.get('http://127.0.0.1:'+str(server.server_port))
        args = SimpleNamespace(command='run', now=True, dry_run=False, task=None, platform='jd', noninteractive=True)
        try:
            with patch('main.launch', new=AsyncMock(return_value=self.context)), patch.object(OrderOnlyGuard,'forward',forward):
                result = await execute(cfg,args)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result,0)
        self.assertEqual(arrived,{'cart-task','detail-task'})
        self.assertEqual(payment_leaks,[])
        self.assertEqual(self.submit_hits,1 if embedded else 2)
        self.assertEqual(len(forwarded),1 if embedded else 0)
        store = StateStore(self.root/'data/state.sqlite3')
        try:
            orders = store.snapshot()['orders']
            self.assertEqual({o['task_id'] for o in orders},{'cart-task','detail-task'})
            self.assertTrue(all(o['status']=='confirmed' and o['price']=='199.00' for o in orders))
            self.assertFalse(any('batch' in o['task_id'] for o in orders))
        finally:
            store.close()

    async def test_one_cart_and_full_page_detail_reach_checkout_before_either_finishes(self):
        await self.run_two()

    async def test_one_cart_and_embedded_detail_keep_distinct_orders_and_block_payment(self):
        await self.run_two(embedded=True)
