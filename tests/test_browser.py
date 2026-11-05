import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from core.config import CST, Config
from core.errors import LoginRequired, Retryable, StopOrder, VerificationRequired
from jd.auth import AuthState
from core.scheduler import Scheduler
from core.state_machine import StateStore
from jd.browser import Session
from main import purchase_attempt, run_one
from jd.cart_scan import CartScanner, extract
from jd.common import sku_ids
from core.payment_secret import PaymentSecret
from core.purchase_lock import PurchaseLanes, SubmissionGuard
from unittest.mock import patch, AsyncMock

SKU = "111111111"
PRODUCT = '''<html><body><a class="nickname">本地模拟账号</a><div class="sku-name">测试手机 黑色 256GB</div>
<div id="choose-attrs"><div class="li"><div class="dt">选择颜色</div><div class="dd"><div class="item selected">黑色</div><div class="item" onclick="this.classList.add('selected')">白色</div></div></div></div>
<input id="buy-num" value="2"><span id="store-prompt">有货</span>
<a id="btn-onkeybuy" href="https://trade.jd.com/shopping/order/getOrderInfo.action">立即购买</a>
<a id="InitCartUrl" href="https://cart.jd.com/cart_index">加入购物车</a></body></html>'''
CART = '''<html><body><div class="item-item" data-sku="111111111"><input type="checkbox"><a href="https://item.jd.com/111111111.html">手机</a><div class="p-quantity"><input class="itxt" value="1"></div></div>
<div class="item-item" data-sku="999999999"><input type="checkbox" checked><a href="https://item.jd.com/999999999.html">其他商品</a><div class="p-quantity"><input class="itxt" value="1"></div></div>
<a class="submit-btn" href="https://trade.jd.com/shopping/order/getOrderInfo.action">去结算</a></body></html>'''
CHECKOUT = '''<html><body><div class="goods-list"><div class="goods-item" data-sku="111111111"><a href="https://item.jd.com/111111111.html">手机</a><span class="p-num">x2</span></div></div>
<div class="payment-item selected" onclick="document.querySelectorAll('.payment-item').forEach(e=>e.classList.remove('selected'));this.classList.add('selected')">在线支付</div>
<div class="payment-item" onclick="document.querySelectorAll('.payment-item').forEach(e=>e.classList.remove('selected'));this.classList.add('selected')">货到付款</div>
<span id="warePriceId">￥200.00</span><span id="freightPriceId">￥9.00</span><span id="discountPriceId">-￥10.00</span><span id="sumPayPriceId">￥199.00</span><div id="sendAddr">上海市 测试街道</div>
<label><input id="useJdBean" type="checkbox">京豆</label><label><input id="useRedPacket" type="checkbox">红包</label>
<button id="order-submit" onclick="location.href='https://trade.jd.com/success'">提交订单</button></body></html>'''

# 模拟普通 Chrome 扩展附加的十个开放 Shadow Root，各有独立 body。
SHADOW_BODIES = '''<script>
for (let i=0;i<10;i++) {
    const host=document.createElement('section');
    document.body.append(host);
    const shadow=host.attachShadow({mode:'open'});
    const body=document.createElement('body');
    body.textContent='扩展辅助内容';
    shadow.append(body);
}
</script>'''
QUICK_VERIFY = '''<html><body><p>验证一下，购物无忧</p>
<button onclick="window.verifyClicks=(window.verifyClicks||0)+1">快速验证</button></body></html>'''


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = Config.parse(self.root / "config.yaml", {
            "jd": {"browser": "chrome", "headless": True, "keep_browser_open": False,
                "payment_preference": {"channel": "default"},
                "login_settle_seconds": 0.2, "login_check_timeout_seconds": 1,
                "result_timeout_seconds": 1, "action_timeout_ms": 700, "manual_timeout_seconds": 2},
            "purchase": {"id": "test", "name": "测试商品", "sku_id": SKU, "quantity": 2,
                "sku_options": {"选择颜色": "黑色"}, "max_price": 220,
                "target_time": "2026-11-11 20:00:00", "address_keyword": "测试街道"},
            "timing": {"sync_clock": False}, "notification": {"desktop": False},
            "retry": {"interval_ms": 500, "max_attempts": 3, "timeout_seconds": 10},
            "logging": {"save_screenshots": False},
        })
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel="chrome", headless=True)
        self.context = await self.browser.new_context()
        self.context.set_default_timeout(1000)
        self.checkout_html = CHECKOUT
        self.success_html = "<html><body>订单提交成功 订单号：123456789012</body></html>"
        self.product_html = PRODUCT
        self.cart_html = CART
        self.cart_release_ns = None
        self.cart_load_times = []
        self.home_html = '<html><body><a class="nickname">本地模拟账号</a></body></html>'
        self.order_html = '<table class="order-tb"><tbody><tr><td>暂无订单</td></tr></tbody></table>'
        self.submit_hits = 0
        self.post_password_html = ""
        self.fail_checkout_once = False
        self.requests = []
        await self.context.route("**/*", self.route)
        await self.credentials()
        self.page = await self.context.new_page()
        self.store = StateStore(self.root / "state.sqlite3")
        self.scheduler = Scheduler(precision=False)
        self.session = Session(self.context, self.page, self.cfg, self.cfg.tasks[0], self.store, self.scheduler)

    async def credentials(self):
        await self.context.add_cookies([{"name": name, "value": "local-test-only", "domain": ".jd.com", "path": "/"} for name in ("pt_key", "pt_pin")])

    async def route(self, route):
        self.requests.append(route.request.url)
        url = urlparse(route.request.url)
        path = url.path
        if path == "/":
            html = self.home_html
        elif path.endswith(".html"):
            html = self.product_html
        elif path == "/cart_index":
            html = self.cart_html
            now = self.scheduler.now_ns()
            self.cart_load_times.append(now)
            if self.cart_release_ns is not None and now < self.cart_release_ns:
                html = html.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        elif path == "/shopping/order/getOrderInfo.action":
            if self.fail_checkout_once:
                self.fail_checkout_once = False
                return await route.abort("failed")
            html = self.checkout_html
        elif path == "/success":
            self.submit_hits += 1
            html = self.success_html
        elif path == "/unknown":
            self.submit_hits += 1
            self.mock_submitted_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            html = "<html><body>系统繁忙</body></html>"
        elif path == "/post-password":
            self.submit_hits += 1
            self.mock_submitted_at = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            html = self.post_password_html
        elif path == "/center/list.action":
            html = self.order_html.replace("__SUBMITTED_AT__", getattr(self, "mock_submitted_at", ""))
        elif path == "/new/login.aspx":
            html = "<html><body>扫码登录</body></html>"
        else:
            return await route.abort()
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    async def asyncTearDown(self):
        self.session.cart.stop_transition_watch()
        self.session.cart.clear_selection_handoff()
        self.store.close()
        self.scheduler.close()
        await self.context.close()
        await self.browser.close()
        await self.p.stop()
        self.temp.cleanup()

    async def checkout(self):
        await self.page.goto("https://trade.jd.com/shopping/order/getOrderInfo.action")

    async def test_login_valid_expired_and_protected_redirect(self):
        await self.page.goto("https://cart.jd.com/cart_index")
        self.assertTrue(await self.session.login.valid())
        await self.context.clear_cookies()
        self.assertFalse(await self.session.login.valid())
        await self.credentials()
        await self.page.goto("https://passport.jd.com/new/login.aspx")
        self.assertFalse(await self.session.login.valid())

    async def test_authenticated_user_without_legacy_cookie_pair(self):
        await self.context.clear_cookies()
        await self.page.goto("https://www.jd.com/")
        self.assertTrue(await self.session.login.valid())

    async def test_login_reuses_session_without_reopening_qrcode(self):
        await self.session.login.login()
        self.assertTrue(await self.session.login.valid())
        self.assertFalse(any("passport.jd.com" in url for url in self.requests))
        self.assertFalse(any("cart.jd.com" in url for url in self.requests))

    async def test_visible_account_ignores_unrelated_login_text(self):
        self.home_html += '<p>购物车帮助：请先登录后添加商品</p>'
        await self.page.goto("https://www.jd.com/")
        await self.session.tools.guard()
        self.assertTrue(await self.session.login.valid())

    async def test_temporary_login_placeholder_does_not_force_rescan(self):
        self.home_html = '<html><body><a href="https://passport.jd.com/new/login.aspx">请登录</a></body></html>'
        await self.page.goto("https://www.jd.com/")
        async def hydrate():
            await asyncio.sleep(.1)
            await self.page.evaluate("document.body.innerHTML='<a class=nickname>本地模拟账号</a>'")
        job = asyncio.create_task(hydrate())
        await self.session.login.ensure()
        await job
        self.assertFalse(any("passport.jd.com" in url for url in self.requests))

    async def test_unknown_login_state_does_not_open_qrcode(self):
        from core.errors import Retryable
        self.home_html = '<html><body>正在加载账号组件</body></html>'
        with self.assertRaises(Retryable):
            await self.session.login.ensure()
        self.assertFalse(any("passport.jd.com" in url for url in self.requests))

    async def test_guard_waits_for_logged_in_header_to_replace_placeholder(self):
        self.home_html = '<html><body><a href="https://passport.jd.com/new/login.aspx">请登录</a></body></html>'
        await self.page.goto("https://www.jd.com/")
        async def hydrate():
            await asyncio.sleep(.1)
            await self.page.evaluate("document.body.innerHTML='<a class=nickname>本地模拟账号</a>'")
        job = asyncio.create_task(hydrate())
        await self.session.tools.guard()
        await job
        self.assertTrue(await self.session.login.valid())

    async def test_cart_login_placeholder_without_legacy_cookies_does_not_notify(self):
        await self.context.clear_cookies()
        self.cart_html = CART.replace('<body>', '<body><div id="account"><a href="https://passport.jd.com/new/login.aspx">请登录</a></div>')
        self.cart_html = self.cart_html.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        await self.page.goto('https://cart.jd.com/cart_index')
        async def hydrate():
            await asyncio.sleep(.1)
            await self.page.locator('#account').evaluate("el=>{el.innerHTML='<a class=nickname>本地模拟账号</a>'}")
        update = asyncio.create_task(hydrate())
        try:
            with patch('jd.login.notify', new_callable=AsyncMock) as notify:
                with self.assertRaisesRegex(Retryable, '勾选控件禁用'):
                    await self.session.cart.select(refresh=False)
                notify.assert_not_awaited()
        finally:
            await update
        status = await self.session.login.status()
        self.assertEqual(status.state, 'authenticated')
        self.assertFalse(status.credentials_present)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='manual'").fetchone()[0], 0)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertFalse(any('passport.jd.com' in url for url in self.requests))

    async def test_manual_login_recheck_precedes_notification_and_screenshot(self):
        await self.context.clear_cookies()
        await self.page.goto('https://www.jd.com/')
        with patch('jd.login.notify', new_callable=AsyncMock) as notify, \
                patch.object(self.session.login, 'snapshot', new_callable=AsyncMock) as snapshot:
            await self.session.login.wait_manual('存在可见的登录入口', login=True)
            notify.assert_not_awaited()
            snapshot.assert_not_awaited()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='manual'").fetchone()[0], 0)
        self.assertFalse(any('passport.jd.com' in url for url in self.requests))

    async def test_persistent_login_gate_without_cookies_still_requires_login(self):
        await self.context.clear_cookies()
        self.home_html = '<html><body><a href="https://passport.jd.com/new/login.aspx">请登录</a></body></html>'
        await self.page.goto('https://www.jd.com/')
        with patch.object(self.session.login, 'status', wraps=self.session.login.status) as status:
            with self.assertRaises(LoginRequired):
                await self.session.tools.guard()
            self.assertGreater(status.await_count, 1)
        self.assertFalse(await self.session.login.valid())
        self.assertEqual(self.submit_hits, 0)

    async def test_quick_verification_during_cookie_free_login_settle_still_blocks(self):
        await self.context.clear_cookies()
        self.home_html = '<html><body><a href="https://passport.jd.com/new/login.aspx">请登录</a></body></html>'
        await self.page.goto('https://www.jd.com/')
        async def challenge():
            await asyncio.sleep(.1)
            await self.page.evaluate('(html)=>{document.body.innerHTML=html}', '<p>验证一下，购物无忧</p><button>快速验证</button>')
        update = asyncio.create_task(challenge())
        try:
            with self.assertRaises(VerificationRequired):
                await self.session.tools.guard()
        finally:
            await update
        self.assertEqual(self.submit_hits, 0)

    async def test_unconfirmed_login_after_gate_is_retryable_not_authenticated(self):
        self.home_html = '<html><body><a href="https://passport.jd.com/new/login.aspx">请登录</a></body></html>'
        await self.page.goto('https://www.jd.com/')
        self.cfg.jd['action_timeout_ms'] = 100
        pending = AuthState('pending', '账号组件尚未加载', False)
        with patch.object(self.session.login, 'status', new_callable=AsyncMock, return_value=pending):
            with self.assertRaisesRegex(Retryable, '尚未确认退出登录'):
                await self.session.tools.guard()
        self.assertEqual(self.submit_hits, 0)

    async def test_login_to_challenge_change_during_dom_sampling_is_rechecked(self):
        self.home_html = QUICK_VERIFY
        await self.page.goto('https://www.jd.com/')
        with patch('jd.auth.main_body_text', new_callable=AsyncMock,
                side_effect=['请登录', '验证一下，购物无忧', '验证一下，购物无忧']):
            with self.assertRaises(VerificationRequired):
                await self.session.tools.guard()
        self.assertEqual(self.submit_hits, 0)

    async def test_real_verification_is_not_ignored_by_visible_account(self):
        self.home_html += '<p>安全验证：请完成验证</p>'
        await self.page.goto("https://www.jd.com/")
        with self.assertRaises(VerificationRequired):
            await self.session.tools.guard()

    async def test_quick_verification_stops_cart_before_waiting_for_rows(self):
        self.cfg.jd['action_timeout_ms'] = 5000
        self.cart_html = QUICK_VERIFY
        with patch.object(self.session.cart, 'find', wraps=self.session.cart.find) as find:
            with self.assertRaisesRegex(VerificationRequired, '快速验证'):
                await self.session.cart.select()
            find.assert_not_awaited()
        self.assertEqual(await self.page.evaluate('window.verifyClicks||0'), 0)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_quick_verification_arriving_while_waiting_for_rows_is_detected(self):
        self.cfg.jd['action_timeout_ms'] = 5000
        await self.page.goto('https://cart.jd.com/cart_index')
        await self.page.evaluate("document.body.innerHTML='<p>正在读取购物车</p>'")
        async def challenge():
            await asyncio.sleep(.15)
            await self.page.evaluate('(html)=>{document.body.innerHTML=html}',
                '<p>验证一下，购物无忧</p><button>快速验证</button>')
        change = asyncio.create_task(challenge())
        try:
            async with asyncio.timeout(2):
                with self.assertRaisesRegex(VerificationRequired, '快速验证'):
                    await self.session.cart.select(refresh=False)
        finally:
            await change
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_quick_verification_takes_priority_over_account_with_shadow_bodies(self):
        self.home_html = QUICK_VERIFY.replace('</body>', '<a class="nickname">模拟账号</a>' + SHADOW_BODIES + '</body>')
        await self.page.goto('https://www.jd.com/')
        self.assertEqual(await self.page.locator('body').count(), 11)
        self.assertEqual((await self.session.login.status()).state, 'verification')
        self.home_html = '<html><body><div>快速验证</div></body></html>'
        await self.page.reload()
        self.assertEqual((await self.session.login.status()).state, 'verification')
        self.home_html = '<html><body><a class="nickname">模拟账号</a><p>商品说明：快速验证质量</p></body></html>'
        await self.page.reload()
        self.assertTrue(await self.session.login.valid())

    async def test_quick_verification_can_resume_after_manual_completion(self):
        self.cart_html = QUICK_VERIFY
        await self.page.goto('https://cart.jd.com/cart_index')
        async def manual_completion():
            await asyncio.sleep(.2)
            # 本地模拟用户完成验证后的页面恢复，不点击/自动完成真实验证码。
            await self.page.evaluate('(html)=>{document.body.innerHTML=html}',
                CART.split('<body>')[1].split('</body>')[0])
        resolved = asyncio.create_task(manual_completion())
        try:
            with patch('jd.login.notify', new_callable=AsyncMock) as notify:
                await self.session.login.wait_manual('京东显示“快速验证”入口，请完成人工验证')
                notify.assert_awaited_once()
        finally:
            await resolved
        self.assertTrue(await self.session.login.valid())
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_manual_verification_resumes(self):
        await self.page.goto("https://item.jd.com/111111111.html")
        await self.page.locator("body").inner_html()
        await self.page.evaluate("document.body.innerHTML='安全验证'")
        with self.assertRaises(VerificationRequired):
            await self.session.tools.guard()
        async def resolve():
            await asyncio.sleep(.2)
            await self.page.goto("https://item.jd.com/111111111.html")
        job = asyncio.create_task(resolve())
        await self.session.login.wait_manual("本地模拟验证")
        await job

    async def test_product_read_and_spec_match(self):
        result = await self.session.product.open()
        self.assertEqual(result["sku"], SKU)
        await self.session.product.specs()
        self.assertEqual(await self.session.product.state(), "available")

    def use_g6_product(self):
        self.product_html = (Path(__file__).parent / 'fixtures' / 'jd_product_g6.html').read_text(encoding='utf-8')
        self.session.task.mode = 'direct'
        self.session.task.source = 'manual'
        self.session.task.address_keyword = ''

    async def test_g6_modern_detail_reads_specs_and_div_buy_entry(self):
        self.use_g6_product()
        self.checkout_html = self.modern_checkout_html()
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertEqual(self.session.product_selection, {'sku': SKU, 'name': '测试商品',
            'quantity': 2, 'options': ['黑色', '256GB']})
        self.assertEqual(self.submit_hits, 0)
        self.assertFalse(await self.session.page.locator('.payment-channel input').first.is_checked())
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_g6_selected_class_and_quantity_are_updated_and_verified(self):
        self.use_g6_product()
        self.session.task.sku_options = {'选择颜色': '白色', '版本': '256GB'}
        await self.session.product.open()
        await self.session.product.specs()
        selected = self.page.locator('.specification-group-0 .specification-item-sku--selected')
        self.assertEqual(await selected.inner_text(), '白色')
        self.assertEqual(await self.page.locator('#buy-num').input_value(), '2')
        self.assertEqual(await self.session.product.state(), 'available')

    async def test_g6_missing_buy_entry_is_unknown_instead_of_not_started(self):
        self.use_g6_product()
        self.product_html = self.product_html.replace('立即购买', '查看服务')
        await self.session.product.open()
        self.assertEqual(await self.session.product.state(), 'unknown')
        with self.assertRaisesRegex(Retryable, '未识别到可购买入口'):
            await self.session.product.buy()
        self.assertEqual(self.submit_hits, 0)

    async def test_g6_disabled_buy_and_selected_lack_are_not_available(self):
        self.use_g6_product()
        self.product_html = self.product_html.replace('class="" onclick=', 'class="buy-btn--disabled" onclick=')
        await self.session.product.open()
        self.assertEqual(await self.session.product.state(), 'not_started')
        self.product_html = self.product_html.replace('specification-item-sku has-image specification-item-sku--selected',
            'specification-item-sku has-image specification-item-sku--selected specification-item-sku--lack')
        await self.session.product.open()
        self.assertEqual(await self.session.product.state(), 'out_of_stock')

    async def test_g6_sku_mismatch_still_stops_before_purchase(self):
        self.use_g6_product()
        self.product_html += "<script>window.pageConfig={product:{skuid:'999999999'}}</script>"
        with self.assertRaisesRegex(StopOrder, 'SKU'):
            await self.session.product.open()
        self.assertEqual(self.submit_hits, 0)

    async def parallel_cart_detail(self, same_sku, unknown=False):
        self.use_g6_product()
        # 此夹具刻意扣住首个结算响应，等待另一入口；不要沿用普通测试的700ms动作上限。
        self.cfg.jd['action_timeout_ms'] = 5000
        self.cfg.jd['result_timeout_seconds'] = 3
        if unknown:
            self.success_html = '<html><body>系统繁忙，订单结果不明</body></html>'
        self.session.submission_guard = SubmissionGuard()
        direct = self.session
        cart_task = replace(direct.task, id='cart-parallel', mode='cart', source='cart', sku_options={})
        # 配置时间不同使两条任务持久化键不同，验证额外的跨入口防重。
        direct.task = replace(direct.task, id='detail-parallel',
            sku=SKU if same_sku else '222222222', target=direct.task.target.replace(microsecond=1000))
        direct.key = direct.task.key(self.cfg.session_dir)
        cart_page = await self.context.new_page()
        cart = Session(self.context, cart_page, self.cfg, cart_task, self.store, self.scheduler)
        cart.submission_guard = direct.submission_guard
        cart.auth_lock = direct.auth_lock
        reached = []
        both = asyncio.Event()
        async def route(request):
            if urlparse(request.request.url).path == '/shopping/order/getOrderInfo.action':
                owner = direct if request.request.frame.page == direct.page else cart
                reached.append(owner.task.id)
                if len(reached) == 2:
                    both.set()
                # 串行的旧实现无法让两页同时到达此处，测试会失败。
                await asyncio.wait_for(both.wait(), 4)
                html = CHECKOUT.replace(SKU, owner.task.sku)
                return await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
            await self.route(request)
        await self.context.unroute('**/*')
        await self.context.route('**/*', route)
        lanes = PurchaseLanes()
        results = await asyncio.gather(run_one(cart, lanes.for_task(cart.task), now=True),
            run_one(direct, lanes.for_task(direct.task), now=True), return_exceptions=True)
        self.assertEqual(set(reached), {'cart-parallel', 'detail-parallel'})
        if unknown:
            self.assertTrue(all(isinstance(result, StopOrder) for result in results), results)
            self.assertTrue(any('并行任务' in str(result) for result in results), results)
            self.assertEqual(self.submit_hits, 1)
            self.assertEqual([order['status'] for order in self.store.snapshot()['orders']], ['unknown'])
        elif same_sku:
            self.assertEqual(sum(result is True for result in results), 1, results)
            self.assertEqual(sum(isinstance(result, StopOrder) for result in results), 1, results)
            self.assertEqual(self.submit_hits, 1)
        else:
            self.assertEqual(results, [True, True])
            self.assertEqual(self.submit_hits, 2)
        self.assertFalse(lanes.for_task(cart.task).locked())
        self.assertFalse(lanes.for_task(direct.task).locked())

    async def test_cart_and_detail_reach_checkout_concurrently_for_different_goods(self):
        await self.parallel_cart_detail(same_sku=False)

    async def test_same_goods_cart_and_detail_race_submits_only_once(self):
        await self.parallel_cart_detail(same_sku=True)

    async def test_same_goods_parallel_unknown_order_blocks_the_other_route(self):
        await self.parallel_cart_detail(same_sku=True, unknown=True)

    async def test_spec_mismatch_blocks(self):
        await self.session.product.open()
        self.session.task.sku_options = {"选择颜色": "不存在的颜色"}
        with self.assertRaises(StopOrder):
            await self.session.product.specs()

    async def test_wrong_product_sku_blocks(self):
        self.product_html = PRODUCT + "<script>window.pageConfig={product:{skuid:'999999999'}}</script>"
        with self.assertRaises(StopOrder):
            await self.session.product.open()

    async def test_cart_selects_only_target_and_quantity(self):
        await self.session.cart.select()
        boxes = self.page.locator(".item-item input[type=checkbox]")
        self.assertTrue(await boxes.nth(0).is_checked())
        self.assertFalse(await boxes.nth(1).is_checked())
        self.assertEqual(await self.page.locator(".item-item .itxt").first.input_value(), "2")

    async def test_cart_batch_wait_ignores_hidden_template_and_keeps_row_indices(self):
        hidden = CART.split('<body>')[1].split('<a class="submit-btn"')[0]
        self.cart_html = CART.replace('<body>', '<body><section style="display:none">' + hidden + '</section>')
        await self.session.cart.select()
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})
        visible = self.page.locator('.item-item').filter(visible=True)
        self.assertTrue(await visible.nth(0).locator('input[type=checkbox]').is_checked())
        self.assertFalse(await visible.nth(1).locator('input[type=checkbox]').is_checked())
        for key in ('navigation', 'page_ready', 'body_ready', 'auth_ready', 'rows_ready'):
            self.assertGreaterEqual(self.session.cart_timings[key], 0)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_batch_wait_handles_delayed_rows_and_custom_selector(self):
        self.cart_html = '<html><body><a class="nickname">本地模拟账号</a><main></main></body></html>'
        self.session.selectors['cart_row'] = '[data-latency-row]'
        await self.page.goto('https://cart.jd.com/cart_index')
        rows = CART.split('<body>')[1].split('</body>')[0].replace('class="item-item"', 'data-latency-row')
        async def hydrate():
            await asyncio.sleep(.08)
            await self.page.locator('main').evaluate('(el, html)=>{el.innerHTML=html}', rows)
        update = asyncio.create_task(hydrate())
        try:
            await self.session.cart.select(refresh=False)
        finally:
            await update
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_batched_auth_checks_ignore_hidden_account_and_login_templates(self):
        await self.context.clear_cookies()
        self.home_html = '''<html><body><a class="nickname" hidden>旧账号模板</a>
            <a href="https://passport.jd.com/new/login.aspx">请登录</a></body></html>'''
        await self.page.goto('https://www.jd.com/')
        self.assertEqual((await self.session.login.status()).state, 'logged_out')
        self.home_html = '''<html><body><a hidden href="https://passport.jd.com/new/login.aspx">请登录</a>
            <a class="nickname">你好，请登录</a><a class="nickname">本地模拟账号</a></body></html>'''
        await self.page.reload()
        self.assertTrue(await self.session.login.valid())
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_checkbox_blocked_uses_short_timeout_and_recovers_without_reload(self):
        self.context.set_default_timeout(5000)
        self.cfg.jd['action_timeout_ms'] = 5000
        self.session.task.quantity = 1
        await self.session.cart.preload()
        await self.page.locator('.item-item input[type=checkbox]').nth(1).uncheck()
        await self.page.locator('body').evaluate("el=>el.insertAdjacentHTML('beforeend','<div id=cover style=\"position:fixed;inset:0;z-index:99999\"></div>')")
        started = time.monotonic()
        with self.assertRaisesRegex(Retryable, '勾选控件正在更新'):
            await self.session.cart.select(refresh=False)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertLess(self.session.cart_timings['target_checkbox'], 1000)
        self.assertTrue(self.session.cart_pending)
        await self.page.locator('#cover').evaluate('el=>el.remove()')
        await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 1})

    async def test_moving_native_checkbox_is_selected_without_waiting_for_layout_stability(self):
        self.cart_html = CART.replace('</body>', '''<style>
        @keyframes moving {from{transform:translateX(0)}to{transform:translateX(2px)}}
        .item-item input[type=checkbox]{animation:moving .08s linear infinite alternate}
        </style></body>''')
        await self.session.cart.select()
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})
        self.assertTrue(await self.page.locator('.item-item input[type=checkbox]').first.is_checked())
        self.assertFalse(await self.page.locator('.item-item input[type=checkbox]').nth(1).is_checked())

    async def test_persistently_blocked_cart_checkbox_refreshes_after_three_failures(self):
        await self.session.cart.preload()
        await self.page.locator('.item-item input[type=checkbox]').nth(1).uncheck()
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:99999\"></div>')")
        for _ in range(3):
            with self.assertRaises(Retryable):
                await self.session.cart.select(refresh=False)
        self.assertEqual(len(self.cart_load_times), 1)
        await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 2)
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_checkbox_recovery_does_not_refresh_away_verification(self):
        await self.session.cart.preload()
        await self.page.locator('.item-item input[type=checkbox]').nth(1).uncheck()
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:99999\"></div>')")
        for _ in range(3):
            with self.assertRaises(Retryable):
                await self.session.cart.select(refresh=False)
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<p>验证一下，购物无忧</p>')")
        with self.assertRaises(VerificationRequired):
            await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_ready_rows_do_not_wait_for_deferred_page_script(self):
        release = asyncio.Event()
        normal_route = self.route
        self.cart_html = CART.replace('</body>', '<script defer src="https://cart.jd.com/slow-cart-script.js"></script></body>')
        async def route(request):
            if urlparse(request.request.url).path == '/slow-cart-script.js':
                await release.wait()
                return await request.fulfill(content_type='application/javascript', body='window.slowScriptFinished=true')
            await normal_route(request)
        await self.context.unroute('**/*')
        await self.context.route('**/*', route)
        try:
            # 阻塞DOMContentLoaded，但真实商品行与控件已可操作。
            async with asyncio.timeout(2):
                await self.session.cart.select()
            self.assertFalse(await self.page.evaluate('!!window.slowScriptFinished'))
            self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})
            self.assertIn('navigation', self.session.cart_timings)
        finally:
            release.set()
            await self.page.wait_for_load_state('domcontentloaded', timeout=3000)

    async def test_cart_hidden_checkbox_blocked_uses_short_timeout(self):
        self.context.set_default_timeout(5000)
        self.cfg.jd['action_timeout_ms'] = 5000
        self.session.task.quantity = 1
        await self.session.cart.preload()
        boxes = self.page.locator('.item-item input[type=checkbox]')
        await boxes.nth(1).uncheck()
        await boxes.first.evaluate("el=>{el.id='target-box';el.style.display='none';el.insertAdjacentHTML('afterend','<label class=cart-checkbox for=target-box>选择</label>')}")
        await self.page.locator('body').evaluate("el=>el.insertAdjacentHTML('beforeend','<div id=cover style=\"position:fixed;inset:0;z-index:99999\"></div>')")
        started = time.monotonic()
        with self.assertRaisesRegex(Retryable, '勾选控件正在更新'):
            await self.session.cart.select(refresh=False)
        self.assertLess(time.monotonic() - started, 1.2)
        await self.page.locator('#cover').evaluate('el=>el.remove()')
        await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 1})

    async def test_waiting_for_sale_cart_checkbox_is_retryable(self):
        self.cart_html = CART.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        with self.assertRaisesRegex(Retryable, "尚未开售"):
            await self.session.cart.select()
        self.assertIsNone(self.session.cart_selection)
        self.assertFalse(any("trade.jd.com" in url for url in self.requests))

    async def test_preload_unreleased_cart_does_not_change_selection(self):
        self.cart_html = CART.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        await self.session.cart.preload()
        self.assertTrue(self.session.cart_preloaded)
        self.assertIsNone(self.session.cart_selection)
        boxes = self.page.locator('.item-item input[type=checkbox]')
        self.assertFalse(await boxes.nth(0).is_checked())
        self.assertTrue(await boxes.nth(1).is_checked())
        self.assertEqual(await self.page.locator('.itxt').first.input_value(), '1')
        self.assertFalse(any('trade.jd.com' in url for url in self.requests))

    async def test_cart_stock_change_during_selection_allows_next_refresh(self):
        await self.session.cart.preload()
        boxes = self.page.locator('.item-item input[type=checkbox]')
        await boxes.nth(1).evaluate("el=>el.addEventListener('change',()=>document.querySelector('.item-item input[type=checkbox]').disabled=true)")
        with self.assertRaisesRegex(Retryable, '暂不可勾选'):
            await self.session.cart.select(refresh=False)
        self.assertFalse(self.session.cart_pending)
        await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 2)
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})

    async def test_unreleased_cart_text_is_not_a_checkbox_timeout(self):
        await self.session.cart.preload()
        await self.page.locator('.item-item').first.evaluate("el=>el.insertAdjacentHTML('beforeend','<span>待发布</span><span>等待抢购</span>')")
        with self.assertRaisesRegex(Retryable, '商品状态=待发布、等待抢购'):
            await self.session.cart.select(refresh=False)
        self.assertFalse(self.session.cart_pending)
        self.assertTrue(await self.page.locator('.item-item input[type=checkbox]').nth(1).is_checked())
        await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 2)
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})

    async def test_sold_out_restocking_cart_records_stock_reason(self):
        self.cart_html = CART.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        self.cart_html = self.cart_html.replace('>手机</a>', '>手机</a><span>无货</span><div>商品补货中</div>', 1)
        with self.assertRaisesRegex(Retryable, '勾选控件禁用；商品状态=无货、商品补货中'):
            await self.session.cart.select()
        self.assertIsNone(self.session.cart_selection)
        self.assertEqual(self.submit_hits, 0)
        self.cart_html = CART.replace('>手机</a>', '>手机</a><span>支持无货订购</span>', 1)
        await self.session.cart.select()
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 2})

    async def test_scheduled_blank_cart_recovers_without_failing_task(self):
        self.session.task.mode = 'cart'
        self.session.task.source = 'cart'
        self.session.task.auto_submit = False
        self.cfg.timing['cart_preload_timeout_seconds'] = 1
        target = self.scheduler.now_ns() + 3_000_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        self.cart_release_ns = target
        normal_route = self.route
        async def route(request):
            if urlparse(request.request.url).path == '/cart_index' and self.scheduler.now_ns() < target:
                self.cart_load_times.append(self.scheduler.now_ns())
                return await request.fulfill(content_type='text/html', body='<html><body></body></html>')
            await normal_route(request)
        await self.context.unroute('**/*')
        await self.context.route('**/*', route)
        with patch.object(self.session.login, 'ensure', new=AsyncMock()):
            self.assertTrue(await run_one(self.session, asyncio.Lock()))
        self.assertLess(self.cart_load_times[0], target - 2_000_000_000)
        self.assertTrue(any(t >= target for t in self.cart_load_times))
        self.assertEqual(self.submit_hits, 0)
        self.assertEqual(self.store.snapshot()['tasks'][0]['state'], 'dry_run')

    async def test_scheduled_slow_cart_is_loaded_early_without_buying_before_sale(self):
        self.session.task.mode = 'cart'
        self.session.task.source = 'cart'
        self.session.task.auto_submit = False
        target = self.scheduler.now_ns() + 4_500_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        self.cart_release_ns = target
        normal_route = self.route
        async def route(request):
            if urlparse(request.request.url).path == '/cart_index':
                await asyncio.sleep(.6)
            await normal_route(request)
        await self.context.unroute('**/*')
        await self.context.route('**/*', route)
        with patch.object(self.session.login, 'ensure', new=AsyncMock()):
            self.assertTrue(await run_one(self.session, asyncio.Lock()))
        self.assertLess(self.cart_load_times[0], target - 3_000_000_000)
        trigger = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='trigger'").fetchone()[0])
        self.assertGreaterEqual(trigger['actual_ns'], target)
        self.assertEqual(self.submit_hits, 0)

    async def test_scheduled_cart_preloads_before_polling_and_waits_for_sale(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.auto_submit = False
        self.cfg.timing["cart_refresh_interval_ms"] = 200
        target = self.scheduler.now_ns() + 5_500_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        self.cart_release_ns = target
        with patch.object(self.session.login, "ensure", new=AsyncMock()):
            self.assertTrue(await run_one(self.session, asyncio.Lock()))
        self.assertGreaterEqual(len([t for t in self.cart_load_times if t < target]), 2)
        self.assertTrue(any(t >= target for t in self.cart_load_times))
        self.assertLess(self.cart_load_times[0], target - 4_000_000_000)
        trigger = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='trigger'").fetchone()[0])
        self.assertGreaterEqual(trigger["actual_ns"], target)
        self.assertEqual(self.submit_hits, 0)
        self.assertIn("getOrderInfo.action", self.session.page.url)

    async def test_cart_polling_stops_refreshing_once_selected_before_sale(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.auto_submit = False
        target = self.scheduler.now_ns() + 3_000_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        with patch.object(self.session.login, "ensure", new=AsyncMock()):
            self.assertTrue(await run_one(self.session, asyncio.Lock()))
        self.assertEqual(len(self.cart_load_times), 1)
        trigger = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='trigger'").fetchone()[0])
        self.assertGreaterEqual(trigger["actual_ns"], target)
        self.assertEqual(self.submit_hits, 0)

    async def test_late_cart_selection_submits_immediately_without_timed_wait(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.auto_submit = True
        self.session.task.retry["timeout_seconds"] = 10
        target = self.scheduler.now_ns() + 2_300_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        self.cart_release_ns = target + 400_000_000
        selected_at = None
        waits_after_selection = []
        select = self.session.cart.select
        wait = self.scheduler.wait

        async def track_select(refresh=True):
            nonlocal selected_at
            await select(refresh=refresh)
            if selected_at is None:
                selected_at = self.scheduler.now_ns()

        async def track_wait(epoch_ns):
            if selected_at is not None:
                waits_after_selection.append(epoch_ns)
            await wait(epoch_ns)

        with patch.object(self.session.login, "ensure", new=AsyncMock()), \
             patch.object(self.session.order, "baseline", new=AsyncMock()), \
             patch.object(self.session.cart, "select", side_effect=track_select), \
             patch.object(self.scheduler, "wait", side_effect=track_wait):
            self.assertTrue(await run_one(self.session, asyncio.Lock()))
        self.assertGreaterEqual(selected_at, self.cart_release_ns)
        self.assertEqual(waits_after_selection, [])
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.get(self.session.key)[0], "confirmed")
        trigger = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='trigger'").fetchone()[0])
        self.assertGreaterEqual(trigger["actual_ns"], selected_at)
        self.assertGreater(trigger["error_ms"], 0)

    async def test_selected_cart_submits_before_competing_refresh_can_invalidate_it(self):
        self.session.task.mode = 'cart'
        self.session.task.source = 'cart'
        self.session.task.retry['max_attempts'] = 1
        self.session.task.target = datetime.fromtimestamp(self.scheduler.now_ns() / 1e9, CST)
        self.session.key = self.session.task.key(self.cfg.session_dir)
        await self.session.cart.preload()
        selected, queued = asyncio.Event(), asyncio.Event()
        lock = asyncio.Lock()
        trace = []
        select = self.session.cart.select
        async def tracked_select(**kwargs):
            await select(**kwargs)
            trace.append('selected')
            selected.set()
            await queued.wait()
        async def competing_refresh():
            await selected.wait()
            queued.set()
            async with lock:
                trace.append('other-refresh')
                # 旧调度会在选中后先执行这里，使商品变为不可勾选；新调度已完成下单。
                if 'cart.jd.com' in self.session.page.url:
                    await self.session.page.locator('.item-item input[type=checkbox]').first.evaluate(
                        'el=>{el.disabled=true;el.checked=false}')
                self.assertEqual(self.submit_hits, 1)
        competitor = asyncio.create_task(competing_refresh())
        try:
            with patch('main.prepare_scheduled_cart', new=AsyncMock()), \
                    patch.object(self.session.cart, 'select', side_effect=tracked_select):
                self.assertTrue(await run_one(self.session, lock))
            await asyncio.wait_for(competitor, 1)
        finally:
            if not competitor.done():
                competitor.cancel()
            await asyncio.gather(competitor, return_exceptions=True)
        self.assertEqual(trace, ['selected', 'other-refresh'])
        self.assertEqual(self.submit_hits, 1)
        self.assertFalse(lock.locked())
        elapsed = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='checkout_start'").fetchone()[0])
        self.assertGreaterEqual(elapsed['since_selection_ms'], 0)

    async def test_early_cart_selection_waits_for_sale_then_submits_once(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.auto_submit = True
        target = self.scheduler.now_ns() + 3_000_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        selected_at = []
        select = self.session.cart.select

        async def track_select(refresh=True):
            await select(refresh=refresh)
            selected_at.append(self.scheduler.now_ns())

        with patch.object(self.session.login, "ensure", new=AsyncMock()), \
             patch.object(self.session.order, "baseline", new=AsyncMock()), \
             patch.object(self.session.cart, "select", side_effect=track_select):
            self.assertTrue(await run_one(self.session, asyncio.Lock()))
        self.assertLess(selected_at[0], target)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.get(self.session.key)[0], "confirmed")
        trigger = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='trigger'").fetchone()[0])
        self.assertGreaterEqual(trigger["actual_ns"], target)

    async def test_cart_polling_until_deadline_never_enters_checkout(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.auto_submit = False
        self.session.task.retry["timeout_seconds"] = 1
        self.session.task.retry["max_attempts"] = 1
        target = self.scheduler.now_ns() + 2_500_000_000
        self.session.task.target = datetime.fromtimestamp(target / 1e9, CST)
        self.cart_release_ns = target + 60_000_000_000
        with patch.object(self.session.login, "ensure", new=AsyncMock()), self.assertRaisesRegex(StopOrder, "仍未勾选"):
            await run_one(self.session, asyncio.Lock())
        self.assertGreater(len(self.cart_load_times), self.session.task.retry["max_attempts"])
        self.assertFalse(any("getOrderInfo.action" in url for url in self.requests))
        self.assertEqual(self.submit_hits, 0)
        self.assertEqual(self.store.snapshot()["orders"], [])

    async def test_cart_checkbox_timeout_during_prepare_continues_to_purchase_retry(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        original_select = self.session.cart.select
        failed_once = False

        async def select(refresh=True):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise PlaywrightTimeout("Locator.set_checked: Timeout 5000ms exceeded.")
            return await original_select(refresh=refresh)

        with patch.object(self.session.cart, "select", side_effect=select):
            self.assertTrue(await run_one(self.session, asyncio.Lock(), dry_run=True))
        self.assertTrue(failed_once)
        self.assertIn("getOrderInfo.action", self.session.page.url)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_scan_is_read_only(self):
        result = await CartScanner(self.session).scan()
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["complete"])
        self.assertEqual(result["items"][0]["quantity"], 1)
        boxes = self.session.page.locator(".item-item input[type=checkbox]")
        self.assertFalse(await boxes.nth(0).is_checked())
        self.assertTrue(await boxes.nth(1).is_checked())
        self.assertEqual(await self.session.page.locator(".itxt").first.input_value(), "1")
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_scan_with_extension_shadow_bodies_is_read_only(self):
        self.cart_html = CART.replace('</body>', SHADOW_BODIES + '</body>')
        await self.page.goto('https://cart.jd.com/cart_index')
        self.assertEqual(await self.page.locator('body').count(), 11)
        result = await CartScanner(self.session).scan()
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(result['complete'])
        self.assertTrue(await self.session.login.valid())
        boxes = self.page.locator('.item-item input[type=checkbox]')
        self.assertFalse(await boxes.nth(0).is_checked())
        self.assertTrue(await boxes.nth(1).is_checked())
        self.assertEqual(await self.page.locator('.itxt').first.input_value(), '1')
        self.assertEqual(self.submit_hits, 0)
        await self.session.tools.inspect()
        details = json.loads((self.cfg.data_dir / 'diagnostics' / 'test.json').read_text(encoding='utf-8'))
        self.assertTrue(any(item.get('sku') == SKU for item in details))

    async def test_main_document_challenge_still_blocks_with_shadow_bodies(self):
        self.home_html = '<html><body><a class="nickname">模拟账号</a><p>请完成安全验证</p>' + SHADOW_BODIES + '</body></html>'
        await self.page.goto('https://www.jd.com/')
        self.assertEqual(await self.page.locator('body').count(), 11)
        with self.assertRaises(VerificationRequired):
            await self.session.login.valid()
        with self.assertRaises(VerificationRequired):
            await self.session.tools.guard()
        self.assertEqual(self.submit_hits, 0)

    async def test_success_and_duplicate_protection_with_shadow_bodies(self):
        self.success_html = self.success_html.replace('</body>', SHADOW_BODIES + '</body>')
        await self.checkout()
        self.assertEqual(await self.session.order.submit(), '123456789012')
        self.assertEqual(await self.page.locator('body').count(), 11)
        self.assertEqual(self.store.get(self.session.key)[0], 'confirmed')
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_current_cart_layout_ignores_related_sku_links(self):
        self.cart_html = '''<html><body><a class="nickname">模拟账号</a><div data-skuuuid="row-1" class="_product-item_hash_1">
        <input type="checkbox"><a class="_goodTitle_hash_1" href="https://item.jd.com/111111111.html">模拟手机</a>
        <div class="_extra-select_hash_1"><span class="_txt_hash_1">黑色 256GB</span></div>
        <a class="_extraLink_hash_1" href="https://item.jd.com/999999999.html">相关服务商品</a>
        <span class="_price-show_hash_1">￥199.00</span><input type="text" value="2"></div></body></html>'''
        await self.page.goto("https://cart.jd.com/cart_index")
        result = await extract(self.page, self.session.selectors)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["spec"], "黑色 256GB")
        self.assertEqual(await sku_ids(self.page.locator("[data-skuuuid]")), [SKU])
        await self.session.cart.select(refresh=False)
        self.assertTrue(await self.page.locator("input[type=checkbox]").is_checked())

    async def test_cart_task_runs_from_cart_without_opening_product_page(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertEqual(self.submit_hits, 0)
        self.assertFalse(any("item.jd.com" in url for url in self.requests))

    async def test_current_cart_quantity_without_type_attribute(self):
        self.cart_html = '''<html><body><a class="nickname">模拟账号</a><div data-skuuuid="row-1">
        <input type="checkbox"><a class="_goodTitle_hash_1" href="https://item.jd.com/111111111.html">模拟手机</a>
        <div class="_good-count_hash_1"><div class="_cart-number_hash_1"><input value="1"></div></div>
        </div></body></html>'''
        await self.session.cart.select()
        quantity = self.page.locator("._cart-number_hash_1 input")
        self.assertEqual(await quantity.input_value(), "2")
        scanned = await extract(self.page, self.session.selectors)
        self.assertTrue(scanned["items"][0]["quantity_known"])
        self.assertEqual(scanned["items"][0]["quantity"], 2)

    def cart_with_row_update_on_quantity(self, action):
        # Model a server-driven cart row replacement after editing quantity.
        script = '''<script>window.updateCart = function(el) {
          if (sessionStorage.getItem('cart-updated-once')) return;
          sessionStorage.setItem('cart-updated-once','yes');
          const row=el.closest('.item-item'), copy=row.cloneNode(true);
          ACTION
          row.replaceWith(copy);
        };</script>'''.replace('ACTION', action)
        return CART.replace('class="itxt" value="1"', 'class="itxt" value="1" onblur="updateCart(this)"', 1) + script

    async def test_cart_row_refresh_before_target_selection_recovers_in_one_pass(self):
        self.cart_html = self.cart_with_row_update_on_quantity("copy.querySelector('input[type=checkbox]').checked=false;")
        await self.page.goto("https://cart.jd.com/cart_index")
        await self.session.cart.select(refresh=False)
        self.assertEqual(self.session.cart_selection, {"sku": SKU, "quantity": 2})
        self.assertEqual(self.submit_hits, 0)

    async def test_other_checkbox_sync_precedes_target_selection_without_quantity_edit(self):
        self.session.task.quantity = 1
        self.cart_html = CART.replace('type="checkbox" checked',
            '''type="checkbox" checked onchange="document.querySelector('.item-item input[type=checkbox]').checked=false"''')
        await self.page.goto("https://cart.jd.com/cart_index")
        await self.session.cart.select(refresh=False)
        self.assertEqual(self.session.cart_selection, {"sku": SKU, "quantity": 1})
        self.assertEqual(self.submit_hits, 0)

    async def test_target_checkbox_is_last_action_after_other_items_and_quantity(self):
        self.cart_html = CART.replace('type="checkbox"', '''type="checkbox" onclick="
        window.readyBeforeTarget = !document.querySelectorAll('.item-item input[type=checkbox]')[1].checked
          && document.querySelector('.itxt').value === '2';
        if (!window.readyBeforeTarget) this.checked=false;"''', 1)
        await self.session.cart.select()
        self.assertTrue(await self.page.evaluate("window.readyBeforeTarget"))
        self.assertEqual(self.session.cart_selection, {"sku": SKU, "quantity": 2})

    async def test_checkout_reuses_verified_selection_without_selecting_or_refreshing_again(self):
        await self.session.cart.select()
        with patch.object(self.session.cart, "select", new=AsyncMock(side_effect=AssertionError("重复选品"))), \
                patch.object(self.session.cart, "verify", new=AsyncMock(side_effect=AssertionError("重复核对"))):
            await self.session.cart.checkout(refresh=True)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertIn("getOrderInfo.action", self.page.url)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_correct_quantity_does_not_relocate_or_edit_quantity(self):
        self.session.task.quantity = 1
        find = self.session.cart.find
        async def forbid_quantity_read(key, **kwargs):
            if key == 'cart_quantity':
                raise AssertionError('已知正确数量仍重复定位')
            return await find(key, **kwargs)
        with patch.object(self.session.cart, 'find', side_effect=forbid_quantity_read):
            await self.session.cart.select()
        self.assertEqual(self.session.cart_selection, {'sku': SKU, 'quantity': 1})
        self.assertEqual(await self.page.locator('.itxt').first.input_value(), '1')

    async def test_cart_navigation_invalidates_selection_handoff(self):
        await self.session.cart.select()
        self.cart_html = CART.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        await self.page.goto('https://cart.jd.com/cart_index')
        with self.assertRaisesRegex(Retryable, '暂不可勾选'):
            await self.session.cart.checkout(refresh=False)
        self.assertFalse(self.session.cart_checkout_started)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_waits_for_checkout_enable_without_refresh_or_reselection(self):
        self.session.target_ns = self.scheduler.now_ns()
        self.cart_html = CART.replace(
            '<a class="submit-btn" href="https://trade.jd.com/shopping/order/getOrderInfo.action">去结算</a>',
            '''<button class="submit-btn" disabled aria-disabled="true"
            onclick="location.href='https://trade.jd.com/shopping/order/getOrderInfo.action'">去结算</button>''')
        await self.session.cart.select()
        await self.page.evaluate('''() => setTimeout(() => {
            const button = document.querySelector('.submit-btn');
            button.disabled = false; button.setAttribute('aria-disabled', 'false');
        }, 150)''')
        with patch.object(self.session.cart, 'select', new=AsyncMock(side_effect=AssertionError('重复选品'))), \
                patch.object(self.session.cart, 'verify', new=AsyncMock(side_effect=AssertionError('重复核对'))):
            await self.session.cart.checkout()
        self.assertIn('getOrderInfo.action', self.session.page.url)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='trigger'").fetchone()[0], 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_disabled_cart_checkout_preserves_selection_and_retries_current_page(self):
        self.session.target_ns = self.scheduler.now_ns()
        self.cart_html = CART.replace('class="submit-btn"', 'class="submit-btn" aria-disabled="true"')
        await self.session.cart.select()
        with self.assertRaisesRegex(Retryable, '结算按钮暂不可用'):
            await self.session.cart.checkout()
        self.assertTrue(self.session.cart_pending)
        self.assertFalse(self.session.cart_checkout_started)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='trigger'").fetchone()[0], 0)
        await self.page.locator('.submit-btn').evaluate("el=>el.setAttribute('aria-disabled','false')")
        await self.session.cart.checkout(refresh=True)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertIn('getOrderInfo.action', self.session.page.url)

    async def test_cart_handoff_keeps_final_checkout_identity_check(self):
        self.checkout_html = CHECKOUT.replace('111111111', '999999999')
        await self.session.cart.select()
        await self.session.cart.checkout()
        with self.assertRaisesRegex(StopOrder, '结算 SKU 包含其他商品'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def prepare_fast_cart_order(self):
        self.session.task.mode = 'cart'
        self.session.task.source = 'cart'
        self.session.task.max_price = None
        await self.session.cart.select()
        await purchase_attempt(self.session, warmed=True, attempt=1)

    async def test_fast_cart_submits_without_goods_address_or_discount_rechecks(self):
        with patch.object(self.session.checkout, 'discounts', new=AsyncMock(side_effect=AssertionError('默认优惠不应扫描'))), \
                patch.object(self.session.checkout, 'validate', new=AsyncMock(side_effect=AssertionError('不应复核结算信息'))), \
                patch.object(self.session.checkout, 'assert_submission_allowed', new=AsyncMock(side_effect=AssertionError('不应重复复核'))):
            await self.prepare_fast_cart_order()
            # 按用户要求依赖刚完成的购物车选品，不等商品、地址和运费组件出现。
            await self.page.locator('.goods-list,#sendAddr,#warePriceId,#freightPriceId,#discountPriceId,#sumPayPriceId').evaluate_all(
                'nodes=>nodes.forEach(el=>el.remove())')
            oid = await self.session.order.submit()
        self.assertEqual(oid, '123456789012')
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.snapshot()['orders'][0]['price'], '')
        event = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='submit_click'").fetchone()[0])
        self.assertTrue(event['fast_cart'])
        self.assertGreaterEqual(event['since_checkout_ms'], 0)
        with self.assertRaisesRegex(StopOrder, '已有提交记录'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_fast_cart_does_not_wait_for_checkout_deferred_scripts(self):
        release = asyncio.Event()
        async def slow_script(route):
            await release.wait()
            await route.fulfill(content_type='application/javascript', body='window.checkoutTailLoaded=true')
        await self.context.route('**/checkout-tail.js', slow_script)
        self.checkout_html = CHECKOUT.replace('<body>', '''<body>
            <script>window.dclSeen=false;document.addEventListener('DOMContentLoaded',()=>window.dclSeen=true)</script>
            <script defer src="https://trade.jd.com/checkout-tail.js"></script>''')
        try:
            await asyncio.wait_for(self.prepare_fast_cart_order(), 3)
            self.assertFalse(await self.page.evaluate('window.dclSeen'))
            with patch.object(self.session.checkout, 'validate', new=AsyncMock(side_effect=AssertionError('不应复核'))):
                await asyncio.wait_for(self.session.order.submit(), 3)
            self.assertEqual(self.submit_hits, 1)
            self.assertFalse(release.is_set())
        finally:
            release.set()

    async def test_fast_cart_preserves_default_payment_and_optional_price_limit(self):
        await self.prepare_fast_cart_order()
        self.session.task.max_price = Decimal('100')
        with self.assertRaisesRegex(StopOrder, '超过价格上限'):
            await self.session.order.submit()
        self.assertIsNone(self.store.get(self.session.key))
        self.session.task.max_price = None
        await self.page.locator('.payment-item').evaluate_all(
            "nodes=>nodes.forEach(el=>el.addEventListener('click',()=>sessionStorage.setItem('paymentChanged','1')))")
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)
        self.assertIsNone(await self.session.page.evaluate("sessionStorage.getItem('paymentChanged')"))
        self.assertEqual(self.store.get(self.session.key)[0], 'confirmed')
        self.assertEqual(self.session.checkout_quote.total, Decimal('199.00'))

    async def test_fast_cart_password_fill_reads_updated_total_before_click(self):
        self.checkout_html = CHECKOUT.replace('<button id="order-submit"', '''
            <div class="payment-summary-password__input"><input type="password"
            oninput="document.getElementById('sumPayPriceId').textContent='￥999.00'"></div><button id="order-submit"''')
        await self.prepare_fast_cart_order()
        self.session.task.max_price = Decimal('220')
        self.session.payment_secret = PaymentSecret('654321')
        with self.assertRaisesRegex(StopOrder, '超过价格上限'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_fast_cart_rejects_offline_payment_without_clicking(self):
        await self.prepare_fast_cart_order()
        await self.page.locator('.payment-item').nth(1).click()
        with self.assertRaisesRegex(StopOrder, '货到付款'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)

    async def test_fast_cart_pauses_for_existing_verification_before_order_claim(self):
        await self.prepare_fast_cart_order()
        await self.page.evaluate("document.body.insertAdjacentHTML('afterbegin','<p>验证一下，购物无忧</p>')")
        with self.assertRaises(VerificationRequired):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_fast_cart_waits_only_for_disabled_submit_then_continues(self):
        self.checkout_html = CHECKOUT.replace('<button id="order-submit"', '<button disabled id="order-submit"')
        await self.prepare_fast_cart_order()
        await self.page.evaluate("setTimeout(()=>document.getElementById('order-submit').disabled=false,120)")
        with patch.object(self.session.checkout, 'validate', new=AsyncMock(side_effect=AssertionError('不应复核'))):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_pending_target_sync_retries_on_current_page_without_reload(self):
        self.session.task.quantity = 1
        self.cart_html = CART.replace('type="checkbox"', '''type="checkbox" onchange="
        if(!sessionStorage.getItem('target-sync-once')) {
          sessionStorage.setItem('target-sync-once','yes');
          Promise.resolve().then(()=>this.checked=false);
        }"''', 1)
        with self.assertRaises(Retryable):
            await self.session.cart.select(refresh=True)
        self.assertTrue(self.session.cart_pending)
        await self.session.cart.select(refresh=True)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.session.cart_selection, {"sku": SKU, "quantity": 1})
        self.assertFalse(self.session.cart_pending)

    async def test_slow_checkout_navigation_is_waited_without_returning_to_cart(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.cart_html = CART.replace('href="https://trade.jd.com/shopping/order/getOrderInfo.action"',
            '''href="#" onclick="event.preventDefault(); setTimeout(()=>location.href='https://trade.jd.com/shopping/order/getOrderInfo.action', 1600);"''')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), dry_run=True))
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='trigger'").fetchone()[0], 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_delayed_cart_checkout_popup_is_followed_without_refresh(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.cart_html = CART.replace(
            '<a class="submit-btn" href="https://trade.jd.com/shopping/order/getOrderInfo.action">去结算</a>',
            '''<button class="submit-btn">去结算</button><script>
            document.querySelector('.submit-btn').addEventListener('click', () => {
              window.checkoutClicks=(window.checkoutClicks||0)+1;
              setTimeout(()=>window.open('https://trade.jd.com/shopping/order/getOrderInfo.action'),180);
            });</script>''')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), dry_run=True))
        self.assertIsNot(self.session.page, self.page)
        self.assertEqual(await self.page.evaluate("window.checkoutClicks"), 1)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertTrue(self.session.cart_checkout_reached)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_checkout_no_navigation_retries_selected_page(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.cart_html = CART.replace(
            '<a class="submit-btn" href="https://trade.jd.com/shopping/order/getOrderInfo.action">去结算</a>',
            '''<button class="submit-btn" onclick="window.checkoutClicks=(window.checkoutClicks||0)+1;
            if(window.checkoutClicks===2) location.href='https://trade.jd.com/shopping/order/getOrderInfo.action'">去结算</button>''')
        with self.assertRaisesRegex(Retryable, '仍停留购物车'):
            await self.session.cart.checkout()
        self.assertFalse(self.session.cart_checkout_started)
        self.assertEqual(await self.page.evaluate('window.checkoutClicks'), 1)
        await purchase_attempt(self.session, False, 2)
        self.assertIn('trade.jd.com', self.session.page.url)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    async def test_checkout_returned_to_cart_reenters_without_reload(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        await self.session.cart.checkout()
        self.cart_html = CART.replace('type="checkbox"', 'type="checkbox" checked', 1).replace(
            'type="checkbox" checked><a href="https://item.jd.com/999999999.html"',
            'type="checkbox"><a href="https://item.jd.com/999999999.html"').replace('value="1"', 'value="2"', 1)
        await self.page.goto('https://cart.jd.com/cart_index')
        self.assertTrue(self.session.cart_checkout_reached)
        await purchase_attempt(self.session, False, 2)
        self.assertIn('trade.jd.com', self.session.page.url)
        self.assertEqual(len(self.cart_load_times), 2)  # 首次加载和网站返回；恢复没有主动刷新。
        self.assertEqual(self.submit_hits, 0)

    async def test_returned_cart_out_of_stock_does_not_reenter_checkout(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        await self.session.cart.checkout()
        self.cart_html = CART.replace('type="checkbox"', 'type="checkbox" disabled', 1)
        await self.page.goto('https://cart.jd.com/cart_index')
        before = self.store.db.execute("SELECT count(*) FROM events WHERE state='checkout_start'").fetchone()[0]
        with self.assertRaisesRegex(Retryable, '暂不可勾选'):
            await purchase_attempt(self.session, False, 2)
        after = self.store.db.execute("SELECT count(*) FROM events WHERE state='checkout_start'").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(len(self.cart_load_times), 2)
        self.assertEqual(self.submit_hits, 0)

    async def test_missing_freight_detects_redirect_to_cart_during_wait(self):
        self.checkout_html = CHECKOUT.replace('<span id="freightPriceId">￥9.00</span>', '')
        await self.checkout()
        await self.page.evaluate("setTimeout(()=>location.href='https://cart.jd.com/cart_index',150)")
        with self.assertRaisesRegex(Retryable, '当前不是订单结算页.*cart.jd.com/cart_index'):
            await self.session.checkout.amount('freight', required=True)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_transition_does_not_adopt_existing_unrelated_checkout(self):
        self.session.concurrent_ui = True
        other = await self.context.new_page()
        await other.goto('https://trade.jd.com/shopping/order/getOrderInfo.action')
        self.cart_html = CART.replace(
            'href="https://trade.jd.com/shopping/order/getOrderInfo.action"', 'href="#" onclick="return false"')
        try:
            with self.assertRaisesRegex(Retryable, '仍停留购物车'):
                await self.session.cart.checkout()
            self.assertIs(self.session.page, self.page)
            self.assertIn('cart.jd.com', self.page.url)
            self.assertEqual(self.submit_hits, 0)
        finally:
            await other.close()

    async def test_late_checkout_popup_between_retries_is_not_clicked_again(self):
        self.session.concurrent_ui = True
        popup_delay_ms = self.cfg.jd['action_timeout_ms'] + 3000
        self.cart_html = CART.replace(
            '<a class="submit-btn" href="https://trade.jd.com/shopping/order/getOrderInfo.action">去结算</a>',
            '''<button class="submit-btn">去结算</button><script>
            document.querySelector('.submit-btn').addEventListener('click', () => {
              window.checkoutClicks=(window.checkoutClicks||0)+1;
              setTimeout(()=>window.open('https://trade.jd.com/shopping/order/getOrderInfo.action'),2600);
            });</script>''')
        self.cart_html = self.cart_html.replace('2600', str(popup_delay_ms))
        async with self.page.expect_popup(timeout=popup_delay_ms + 5000) as popup:
            with self.assertRaisesRegex(Retryable, '仍停留购物车'):
                await self.session.cart.checkout()
        child = await popup.value
        await child.wait_for_load_state('domcontentloaded')
        await self.session.cart.checkout(refresh=True)
        self.assertIs(self.session.page, child)
        self.assertEqual(await self.page.evaluate('window.checkoutClicks'), 1)
        self.assertEqual(len(self.cart_load_times), 1)
        self.assertEqual(self.submit_hits, 0)

    async def test_checkout_verification_popup_pauses_without_clicking_challenge(self):
        self.session.concurrent_ui = True
        await self.context.route('**/quick-verification', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body=QUICK_VERIFY))
        self.cart_html = CART.replace(
            '<a class="submit-btn" href="https://trade.jd.com/shopping/order/getOrderInfo.action">去结算</a>',
            '''<button class="submit-btn">去结算</button><script>
            document.querySelector('.submit-btn').addEventListener('click', () => {
              setTimeout(()=>window.open('https://cfe.m.jd.com/quick-verification'),150);
            });</script>''')
        with self.assertRaises(VerificationRequired):
            await self.session.cart.checkout()
        self.assertIn('cfe.m.jd.com', self.session.page.url)
        self.assertEqual(await self.session.page.evaluate('window.verifyClicks||0'), 0)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_explicit_network_failure_can_reload_then_finish_dry_run(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.fail_checkout_once = True
        self.assertTrue(await run_one(self.session, asyncio.Lock(), dry_run=True))
        self.assertEqual(len(self.cart_load_times), 2)
        self.assertIn("getOrderInfo.action", self.page.url)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_refresh_during_prewarm_continues_dry_run_without_submission(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.cart_html = self.cart_with_row_update_on_quantity("copy.querySelector('input[type=checkbox]').checked=false;")
        self.assertTrue(await run_one(self.session, asyncio.Lock(), dry_run=True))
        self.assertIn("getOrderInfo.action", self.page.url)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_row_refresh_changing_quantity_does_not_produce_false_ready(self):
        self.cart_html = self.cart_with_row_update_on_quantity("copy.querySelector('input[type=checkbox]').checked=true; copy.querySelector('.itxt').value='3';")
        with self.assertRaisesRegex(Retryable, "更新后数量与任务不符"):
            await self.session.cart.select()
        self.assertIsNone(self.session.cart_selection)
        self.assertEqual(self.submit_hits, 0)

    async def test_cart_other_item_reselected_during_update_still_stops(self):
        self.cart_html = self.cart_with_row_update_on_quantity("document.querySelector('[data-sku=\"999999999\"] input[type=checkbox]').checked=true;")
        with self.assertRaisesRegex(StopOrder, "最终勾选项不等于目标商品"):
            await self.session.cart.select()
        self.assertIsNone(self.session.cart_selection)
        self.assertEqual(self.submit_hits, 0)

    async def test_no_price_cap_still_records_actual_payable(self):
        self.session.task.max_price = None
        self.checkout_html = CHECKOUT.replace("￥199.00", "￥299.00")
        await self.checkout()
        quote = await self.session.checkout.validate()
        self.assertEqual(str(quote.total), "299.00")
        self.assertEqual(self.submit_hits, 0)

    async def test_zero_payable_after_discounts_is_accepted(self):
        self.session.task.max_price = None
        self.checkout_html = CHECKOUT.replace("￥199.00", "￥0.00")
        await self.checkout()
        quote = await self.session.checkout.validate()
        self.assertEqual(str(quote.total), "0.00")
        self.assertEqual(self.submit_hits, 0)

    async def test_legacy_cod_task_is_rejected_before_payment_actions(self):
        self.session.task.payment_method = "cod"
        await self.checkout()
        with self.assertRaisesRegex(StopOrder, "仅支持在线支付"):
            await self.session.checkout.select_payment()
        self.assertIn("在线支付", await self.page.locator(".payment-item.selected").inner_text())
        self.assertEqual(self.submit_hits, 0)

    async def test_mismatched_payment_prevents_submission(self):
        await self.checkout()
        await self.page.locator('.payment-item').nth(1).click()
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)

    async def test_quote_price_breakdown_and_discounts(self):
        await self.checkout()
        self.session.task.discounts["use_beans"] = True
        self.session.task.discounts["use_red_packets"] = True
        await self.session.checkout.discounts()
        self.assertTrue(await self.page.locator("#useJdBean").is_checked())
        self.assertTrue(await self.page.locator("#useRedPacket").is_checked())
        quote = await self.session.checkout.validate()
        self.assertEqual(str(quote.total), "199.00")
        self.assertEqual(str(quote.freight), "9.00")

    async def test_price_limit_blocks_without_submit(self):
        self.checkout_html = CHECKOUT.replace("￥199.00", "￥299.00")
        await self.checkout()
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_submit_button_texts_and_native_input(self):
        for text in self.cfg.jd["submit_button_texts"]:
            self.checkout_html = CHECKOUT.replace('id="order-submit"', '').replace('>提交订单</button>', f'>{text}</button>')
            await self.checkout()
            with self.subTest(text=text):
                button = await self.session.tools.find("submit")
                self.assertEqual(await button.inner_text(), text)
        self.checkout_html = CHECKOUT.replace('<button id="order-submit" onclick="location.href=\'https://trade.jd.com/success\'">提交订单</button>', '<input type="submit" value="无货订购">')
        await self.checkout()
        button = await self.session.tools.find("submit")
        self.assertEqual(await button.input_value(), "无货订购")

    async def test_backorder_button_can_submit_once_after_validation(self):
        self.checkout_html = CHECKOUT.replace('id="order-submit"', '').replace('>提交订单</button>', '>无货订购</button>')
        await self.checkout()
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_pay_now_in_order_area_does_not_follow_cashier_payment(self):
        self.checkout_html = CHECKOUT.replace('>提交订单</button>', '>立即支付</button>')
        self.success_html += '<button id="cashier-pay" onclick="window.paymentClicks=(window.paymentClicks||0)+1">确认付款</button>'
        await self.checkout()
        self.assertEqual(await (await self.session.tools.find("submit")).inner_text(), "立即支付")
        await self.session.order.submit()
        self.assertEqual(self.store.get(self.session.key)[0], "confirmed")
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(await self.session.page.evaluate('window.paymentClicks||0'), 0)

    async def test_hidden_footer_and_disabled_submit_candidates(self):
        self.checkout_html = CHECKOUT.replace('id="order-submit"', '').replace('<body>', '''<body>
            <footer><a href="#">确认下单</a></footer><div class="mod_help"><a>分期付款</a></div>
            <button style="display:none">立即支付</button><button disabled>无货订购</button>''')
        await self.checkout()
        button = await self.session.tools.find("submit")
        self.assertEqual(await button.inner_text(), "提交订单")
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_conflicting_submit_actions_are_not_guessed(self):
        self.checkout_html = CHECKOUT.replace('<body>', '<body><button>立即支付</button>')
        await self.checkout()
        with self.assertRaisesRegex(StopOrder, "多个不同的提交动作"):
            await self.session.tools.find("submit")
        self.assertEqual(self.submit_hits, 0)

    async def test_modern_nonsemantic_button_locator_survives_dom_updates(self):
        original = '<button id="order-submit" onclick="location.href=\'https://trade.jd.com/success\'">提交订单</button>'
        replacement = '<div class="payment-action-submit"><div class="btn" onclick="location.href=\'https://trade.jd.com/success\'"><span>无货</span>\n<span>订购</span></div></div>'
        self.checkout_html = CHECKOUT.replace(original, replacement)
        await self.checkout()
        button = await self.session.tools.find("submit")
        await self.page.locator("body").evaluate("el=>el.insertAdjacentHTML('afterbegin','<button>新增无关控件</button><a>更多</a>')")
        await button.click()
        self.assertEqual(self.submit_hits, 1)

    async def test_submit_text_on_product_page_is_not_checkout(self):
        self.product_html = PRODUCT + '<button id="order-submit">立即支付</button>'
        await self.page.goto(f"https://item.jd.com/{SKU}.html")
        self.assertIsNone(await self.session.tools.find("submit", required=False))

    async def test_custom_submit_text_and_selector_are_respected(self):
        self.checkout_html = CHECKOUT.replace('>提交订单</button>', '>同意并下单</button>') + '<button>立即支付</button>'
        self.cfg.jd["submit_button_texts"].append("同意并下单")
        self.cfg.selectors["submit"] = "#order-submit"
        await self.checkout()
        button = await self.session.tools.find("submit")
        self.assertEqual(await button.inner_text(), "同意并下单")

    def modern_checkout_html(self, button_text="立即支付"):
        return f'''<html><body><div class="sku-list"><div class="sku-item">
            <div class="title-hash"><div class="ellipsis-hash">测试商品</div></div><div class="sku-hash">黑色 256GB</div>
            <div class="quantity-hash">0.04kg×2</div></div></div>
            <div class="payment-summary-item"><div class="payment-summary-item__title"><em>商品总额</em></div><div class="payment-summary-item__price">￥200.00</div></div>
            <div class="payment-summary-item"><div class="payment-summary-item__title"><em>运费</em></div><div class="payment-summary-item__price">￥9.00</div></div>
            <div class="payment-channel"><div class="modePaymentLi-hash"><div>APP支付</div><div>京东App扫码付款</div>
                <label class="checkbox"><input type="checkbox" aria-checked="false" onclick="this.setAttribute('aria-checked',this.checked?'true':'false')"></label></div>
            <div class="modePaymentLi-hash"><div>货到付款</div><label class="checkbox"><input type="checkbox" disabled></label></div></div>
            <div class="payment-action"><div class="priceInfo">￥199.00</div><div class="payment-action-submit">
            <button onclick="location.href='https://trade.jd.com/success'">{button_text}</button></div></div></body></html>'''

    async def prepare_modern_checkout(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.cart_spec = "黑色 256GB"
        self.session.task.address_keyword = ""
        self.session.cart_selection = {"sku": SKU, "quantity": 2}
        self.checkout_html = self.modern_checkout_html()
        await self.checkout()

    def product_variant_html(self):
        return f'''<html><body><a class="nickname">本地模拟账号</a><h1 class="sku-name">测试商品</h1>
            <div id="choose-attrs"><div class="li"><div class="dt">选择颜色</div><div class="item selected">黑色</div></div>
            <div class="li"><div class="dt">选择容量</div><div class="item selected">256GB</div></div></div>
            <input id="buy-num" value="2"><span id="store-prompt">有货</span>
            <a id="btn-onkeybuy" href="https://trade.jd.com/shopping/order/getOrderInfo.action">立即购买</a></body></html>'''

    async def test_product_direct_modern_checkout_matches_captured_variant_and_default_payment(self):
        self.session.task.mode = 'direct'
        self.session.task.source = 'manual'
        self.session.task.name = '京东商品 ' + SKU
        self.session.task.address_keyword = ''
        self.product_html = self.product_variant_html()
        self.checkout_html = self.modern_checkout_html()
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertEqual(self.session.product_selection, {'sku': SKU, 'quantity': 2,
            'name': '测试商品', 'options': ['黑色', '256GB']})
        self.assertEqual(self.cart_load_times, [])
        self.assertEqual(self.submit_hits, 0)
        # 默认通道中的原生复选框未由脚本改动。
        self.assertFalse(await self.session.page.locator('.payment-channel input').first.is_checked())
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_product_direct_modern_checkout_rejects_missing_or_conflicting_evidence(self):
        self.session.task.mode = 'direct'
        self.session.task.source = 'manual'
        self.session.task.address_keyword = ''
        self.product_html = self.product_variant_html()
        self.checkout_html = self.modern_checkout_html()
        await self.session.product.open()
        await self.session.product.specs()
        self.assertIsNone(self.session.product_selection)
        await self.session.product.buy()
        valid = self.session.product_selection
        for invalid in (None, valid | {'sku': '999999999'}, valid | {'quantity': 1},
                        valid | {'options': []}, valid | {'options': ['白色', '128GB']},
                        valid | {'name': '其他商品'}):
            self.session.product_selection = invalid
            with self.subTest(proof=invalid), self.assertRaises(StopOrder):
                await self.session.order.submit()
            self.assertIsNone(self.store.get(self.session.key))
        self.assertEqual(self.submit_hits, 0)

    async def test_scheduled_product_detail_never_clicks_before_target(self):
        self.session.task.mode = 'direct'
        self.session.task.source = 'manual'
        self.session.task.address_keyword = ''
        self.session.task.target = datetime.fromtimestamp(self.scheduler.now_ns() / 1e9 + 1.5, CST)
        self.session.key = self.session.task.key(self.cfg.session_dir)
        self.product_html = self.product_variant_html()
        self.checkout_html = self.modern_checkout_html()
        clicks = []
        original = self.scheduler.record_trigger
        def record(task_id, target_ns):
            actual, error = original(task_id, target_ns)
            clicks.append((actual, target_ns))
            return actual, error
        with patch.object(self.scheduler, 'record_trigger', side_effect=record):
            await run_one(self.session, asyncio.Lock())
        self.assertEqual(len(clicks), 1)
        self.assertGreaterEqual(clicks[0][0], clicks[0][1])
        self.assertEqual(self.cart_load_times, [])
        self.assertEqual(self.submit_hits, 1)

    async def test_modern_checkout_manual_app_payment_and_actual_amount(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        quote = await self.session.checkout.validate()
        self.assertEqual(str(quote.total), "199.00")
        self.assertEqual(str(quote.freight), "9.00")
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def prepare_baitiao_checkout(self):
        await self.prepare_modern_checkout()
        self.checkout_html = (Path(__file__).parent / "fixtures" / "jd_checkout_baitiao.html").read_text(encoding="utf-8")
        await self.checkout()

    async def test_live_layout_baitiao_gift_and_unitemized_freight(self):
        await self.prepare_baitiao_checkout()
        channel = self.page.locator('.payment-channel')
        before = await channel.evaluate("el=>({html:el.outerHTML,checked:[...el.querySelectorAll('input')].map(n=>n.checked)})")
        await self.session.checkout.select_payment()
        quote = await self.session.checkout.validate()
        self.assertEqual(str(quote.total), "167.90")
        self.assertEqual(str(quote.subtotal), "200.00")
        self.assertEqual(quote.discount, "-￥10.00")
        self.assertIsNone(quote.freight)  # 默认通道优惠还可能改变总价，不由差额倒算运费。
        self.assertEqual(await channel.evaluate("el=>({html:el.outerHTML,checked:[...el.querySelectorAll('input')].map(n=>n.checked)})"), before)
        state = next(item for item in self.store.snapshot()['tasks'] if item['state'] == 'validated')
        self.assertIsNone(json.loads(state['detail'])['freight'])
        self.assertEqual(json.loads(state['detail'])['freight_status'], 'not_itemized')
        button = await self.session.tools.find('submit')
        self.assertEqual((await button.inner_text()).replace('\n', ''), '分期付款白条12期免息')
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_baitiao_submit_labels_and_footer_are_scoped(self):
        for label in ('分期付款 白条3期免息', '分期付款 白条12期免息', '分期付款 白条24期免息', '分期付款 信用卡12期免息'):
            self.checkout_html = self.modern_checkout_html(label) + '<footer><button>分期付款白条6期免息</button></footer>'
            await self.checkout()
            button = await self.session.tools.find('submit')
            with self.subTest(label=label):
                self.assertEqual(await button.inner_text(), label)

    async def test_baitiao_does_not_match_other_credit_actions(self):
        for label in ('分期付款白条12期有息', '白条12期免息', '开通白条', '确认借款'):
            self.checkout_html = self.modern_checkout_html(label)
            await self.checkout()
            with self.subTest(label=label):
                self.assertIsNone(await self.session.tools.find('submit', required=False))
        self.assertEqual(self.submit_hits, 0)

    async def test_baitiao_conflicting_submit_plans_stop(self):
        await self.prepare_baitiao_checkout()
        await self.page.locator('body').evaluate("el=>el.insertAdjacentHTML('beforeend','<div class=\"payment-action-submit\"><button>分期付款白条3期免息</button></div>')")
        with self.assertRaisesRegex(StopOrder, '多个不同的提交动作'):
            await self.session.tools.find('submit')
        self.assertEqual(self.submit_hits, 0)

    async def test_unitemized_freight_still_enforces_price_cap(self):
        await self.prepare_baitiao_checkout()
        self.session.task.max_price = 100
        with self.assertRaisesRegex(StopOrder, '超过价格上限'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_unitemized_freight_still_checks_main_goods_quantity(self):
        await self.prepare_baitiao_checkout()
        await self.page.locator('.quantity-b433ff').evaluate("el=>el.textContent='0.986kg×3'")
        with self.assertRaisesRegex(StopOrder, '商品数量与配置不一致'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)

    async def test_itemized_freight_is_read_in_baitiao_layout(self):
        await self.prepare_baitiao_checkout()
        await self.page.locator('.payment-summary-inner').evaluate("el=>el.insertAdjacentHTML('beforeend','<div class=\"payment-summary-item\"><div class=\"payment-summary-item__title\"><em>运费</em></div><div class=\"payment-summary-item__price\">￥9.50</div></div>')")
        await self.page.locator('.priceInfo').evaluate("el=>el.textContent='￥199.50'")
        quote = await self.session.checkout.validate()
        self.assertEqual(str(quote.freight), '9.50')
        self.assertEqual(str(quote.total), '199.50')

    async def test_freight_parse_failure_is_not_treated_as_unitemized(self):
        await self.prepare_baitiao_checkout()
        await self.page.locator('.payment-summary-inner').evaluate("el=>el.insertAdjacentHTML('beforeend','<div class=\"payment-summary-item\"><div class=\"payment-summary-item__title\"><em>运费明细</em></div><div class=\"unrecognized-price\">￥9.50</div></div>')")
        with self.assertRaisesRegex(Retryable, '未找到 freight'):
            await self.session.checkout.validate()
        self.assertEqual(self.submit_hits, 0)

    async def test_custom_required_freight_selector_is_not_skipped(self):
        await self.prepare_baitiao_checkout()
        self.cfg.selectors['freight'] = '.explicit-freight'
        self.session.selectors['freight'] = '.explicit-freight'
        with self.assertRaisesRegex(Retryable, '未找到 freight'):
            await self.session.checkout.validate()

    async def test_legacy_missing_freight_is_not_assumed_free(self):
        self.checkout_html = CHECKOUT.replace('<span id="freightPriceId">￥9.00</span>', '')
        await self.checkout()
        with self.assertRaisesRegex(Retryable, '未找到 freight'):
            await self.session.checkout.validate()
        self.assertEqual(self.submit_hits, 0)

    async def prepare_page_default_checkout(self, payment_html):
        await self.prepare_modern_checkout()
        await self.page.locator('.payment-channel').evaluate("""(el,html)=>{
            el.innerHTML=html;
            window.paymentTouches=0;
            el.addEventListener('click',()=>window.paymentTouches++);
            el.addEventListener('change',()=>window.paymentTouches++);
        }""", payment_html)

    async def test_page_default_card_without_installment_controls_submits(self):
        await self.prepare_page_default_checkout('<div class="jdPaymentList-new"><input type="checkbox" checked>账户默认银行卡</div>')
        before = await self.page.locator('.payment-channel').inner_html()
        await self.session.checkout.select_payment()
        await self.session.checkout.validate()
        self.assertEqual(await self.page.locator('.payment-channel').inner_html(), before)
        self.assertEqual(await self.page.evaluate('window.paymentTouches'), 0)
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_page_default_unknown_markup_and_multiple_plan_markers_do_not_block(self):
        await self.prepare_page_default_checkout('<section class="new-payment-component" data-selected="true">平台默认在线付款</section><div class="modePayment_main-new highlighting-new">默认方案</div><div class="modePayment_main-new highlighting-new">优惠角标</div>')
        before = await self.page.locator('.payment-channel').inner_html()
        await self.session.checkout.select_payment()
        self.assertEqual(await self.page.locator('.payment-channel').inner_html(), before)
        self.assertEqual(await self.page.evaluate('window.paymentTouches'), 0)
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_page_default_does_not_expand_hidden_payment_details(self):
        await self.prepare_page_default_checkout('<p>银行卡付款明细暂未展开</p>')
        await self.page.locator('.payment-channel').evaluate("el=>el.style.display='none'")
        await self.session.checkout.select_payment()
        self.assertFalse(await self.page.locator('.payment-channel').is_visible())
        self.assertEqual(await self.page.evaluate('window.paymentTouches'), 0)
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_page_default_does_not_force_available_app_option(self):
        await self.prepare_modern_checkout()
        app = self.page.locator('.modePaymentLi-hash input').first
        self.assertFalse(await app.is_checked())
        await self.session.checkout.select_payment()
        self.assertFalse(await app.is_checked())
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_page_default_does_not_require_payment_channel_markup(self):
        await self.prepare_modern_checkout()
        await self.page.locator('.payment-channel').evaluate('el=>el.remove()')
        await self.session.checkout.select_payment()
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_page_default_ignores_legacy_runtime_bank_preference(self):
        await self.prepare_page_default_checkout('<p>默认使用账户已有支付方式</p>')
        self.cfg.jd['payment_preference'] = {'channel': 'jd_pay', 'bank_name': '不存在的旧银行', 'card_type': '信用卡', 'installment': '不分期', 'card_last4': '9999'}
        await self.session.checkout.select_payment()
        self.assertEqual(await self.page.evaluate('window.paymentTouches'), 0)
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_page_default_selected_offline_mode_stops_without_switching(self):
        await self.prepare_modern_checkout()
        await self.page.locator('.modePaymentLi-hash input').nth(1).evaluate('el=>{el.disabled=false;el.checked=true}')
        with self.assertRaisesRegex(StopOrder, "当前页面选择了货到付款"):
            await self.session.order.submit()
        self.assertTrue(await self.page.locator('.modePaymentLi-hash input').nth(1).is_checked())
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_page_default_pay_button_outside_order_area_stops(self):
        await self.prepare_modern_checkout()
        await self.page.locator('.payment-action-submit button').evaluate('el=>document.body.append(el)')
        with self.assertRaisesRegex(StopOrder, "不在已识别的订单提交区域"):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    def post_password_prompt(self, result="订单号：123456789012 待付款"):
        # 此弹窗只在最终下单点击后的响应出现；填密码反而触发模拟扣款标记。
        return '''<html><body><main id="result"></main><div role="dialog" aria-label="支付验证">
            <h2>请输入支付密码</h2><input type="password" oninput="window.unwantedPayment=true">
            <button id="close-password" aria-label="关闭" onclick="document.getElementById('result').textContent=this.dataset.result;this.parentElement.remove()"></button>
            <button onclick="window.unwantedPayment=true">确认付款</button></div></body></html>'''.replace(
                'id="close-password"', 'id="close-password" data-result="' + result + '"')

    async def prepare_post_password_order(self, result="订单号：123456789012 待付款", pre_password=False):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        if pre_password:
            await self.add_password_inputs(6)
        self.session.payment_secret = PaymentSecret("654321")
        self.session.baseline_ids = set()
        self.post_password_html = self.post_password_prompt(result)
        await self.page.locator('.payment-action-submit button').evaluate("el=>el.onclick=()=>location.href='https://trade.jd.com/post-password'")

    async def test_post_submit_password_is_closed_without_filling(self):
        await self.prepare_post_password_order()
        oid = await self.session.order.submit()
        self.assertEqual(oid, "123456789012")
        self.assertFalse(self.session.payment_secret_attempted)
        self.assertFalse(await self.session.page.evaluate('!!window.unwantedPayment'))
        self.assertEqual(await self.session.page.get_by_role('dialog').count(), 0)
        self.assertEqual(self.submit_hits, 1)
        self.assertIn("待付款", self.store.get(self.session.key)[2])
        with self.assertRaises(StopOrder):
            await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 1)

    async def test_pre_submit_password_filled_then_post_submit_password_closed(self):
        await self.prepare_post_password_order(pre_password=True)
        await self.session.order.submit()
        self.assertTrue(self.session.payment_secret_attempted)
        self.assertFalse(await self.session.page.evaluate('!!window.unwantedPayment'))
        self.assertEqual(self.submit_hits, 1)
        self.assertIn("待付款", self.store.get(self.session.key)[2])

    async def test_post_submit_password_without_runtime_secret_can_close(self):
        await self.prepare_post_password_order()
        self.session.payment_secret.clear()
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)
        self.assertIn("待付款", self.store.get(self.session.key)[2])

    async def test_closed_password_dialog_alone_does_not_confirm_order(self):
        await self.prepare_post_password_order(result="返回商品页面")
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.store.get(self.session.key)[0], "unknown")
        self.assertEqual(self.submit_hits, 1)
        self.assertFalse(self.session.payment_secret_attempted)

    async def test_post_submit_password_close_does_not_cancel_order(self):
        await self.prepare_post_password_order()
        self.post_password_html = self.post_password_html.replace('aria-label="关闭"', 'aria-label="取消订单"')
        self.session.selectors["payment_password_dialog_close"] = "#close-password"
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.store.get(self.session.key)[0], "unknown")
        self.assertEqual(self.submit_hits, 1)
        self.assertFalse(self.session.payment_secret_attempted)

    async def test_password_dialog_cannot_be_closed_before_order_submission(self):
        self.checkout_html = self.post_password_prompt()
        await self.checkout()
        self.assertFalse(await self.session.order.dismiss_post_submit_password(self.page))
        self.assertEqual(await self.page.get_by_role('dialog').count(), 1)

    async def test_password_cannot_be_filled_after_submission(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        await self.add_password_inputs(6)
        self.session.payment_secret = PaymentSecret("654321")
        self.session.submitted = True
        self.assertFalse(await self.session.checkout.fill_payment_password())
        self.assertFalse(self.session.payment_secret_attempted)
        self.assertEqual(await self.page.locator('.payment-summary-password__input input').evaluate_all('nodes=>nodes.map(e=>e.value).join("")'), "")

    async def test_post_submit_password_popup_is_closed_and_parent_order_checked(self):
        await self.prepare_post_password_order()
        self.post_password_html = '<html><body><h2>请输入支付密码</h2><input type="password" oninput="window.unwantedPayment=true"></body></html>'
        await self.page.locator('.payment-action-submit button').evaluate("""el=>el.setAttribute('onclick',
            "document.body.innerHTML='订单号：123456789012 待付款';window.open('https://pay.jd.com/post-password')")""")
        origin = self.page
        await self.session.order.submit()
        self.assertEqual(self.session.page, origin)
        self.assertFalse(origin.is_closed())
        self.assertEqual(len(self.context.pages), 1)
        self.assertEqual(self.submit_hits, 1)
        self.assertFalse(self.session.payment_secret_attempted)
        self.assertIn("待付款", self.store.get(self.session.key)[2])

    async def test_post_submit_password_close_reconciles_pending_order(self):
        await self.prepare_post_password_order(result="正在返回订单")
        self.order_html = f'''<table class="order-tb"><tbody data-sku="{SKU}"><tr><td>订单号：123456789012 待付款
            <span class="dealtime" title="__SUBMITTED_AT__">__SUBMITTED_AT__</span><a href="https://item.jd.com/{SKU}.html">手机</a>
            <div class="amount"><span class="ftx-13">￥199.00</span></div><span class="goods-number">x2</span></td></tr></tbody></table>'''
        await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)
        self.assertTrue(any('order.jd.com/center/list.action' in url for url in self.requests))
        self.assertIn("待付款", self.store.get(self.session.key)[2])

    async def test_modern_identity_needs_cart_proof_and_matching_spec(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        for proof, spec in ((None, "黑色 256GB"), ({"sku": "999999999", "quantity": 2}, "黑色 256GB"),
                            ({"sku": SKU, "quantity": 2}, "白色 128GB")):
            self.session.cart_selection = proof
            self.session.task.cart_spec = spec
            with self.subTest(proof=proof, spec=spec), self.assertRaises(StopOrder):
                await self.session.order.submit()
            self.assertIsNone(self.store.get(self.session.key))
        self.assertEqual(self.submit_hits, 0)

    async def test_modern_payment_password_stops_before_final_click(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        await self.page.locator("body").evaluate("el=>el.insertAdjacentHTML('beforeend','<div class=\"payment-summary-password__input\"><input type=\"password\"></div>')")
        with self.assertRaisesRegex(StopOrder, "要求支付密码"):
            await self.session.order.submit()
        self.assertIsNone(self.store.get(self.session.key))
        self.assertEqual(self.submit_hits, 0)

    async def add_password_inputs(self, count):
        controls = '<input type="password" maxlength="6">' if count == 1 else '<input class="otp-input" maxlength="1">' * count
        await self.page.locator("body").evaluate("(el,html)=>el.insertAdjacentHTML('beforeend',html)",
            '<div class="payment-summary-password__input">' + controls + '</div>')

    async def test_payment_password_fills_six_boxes_and_submits_once(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        await self.add_password_inputs(6)
        self.session.payment_secret = PaymentSecret("654321")
        await self.session.order.submit()
        self.assertTrue(self.session.payment_secret_attempted)
        self.assertEqual(self.submit_hits, 1)
        events = self.store.snapshot()
        self.assertNotIn("654321", str(events))
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)

    async def test_payment_password_single_field_and_amount_recheck(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        await self.add_password_inputs(1)
        self.session.payment_secret = PaymentSecret("654321")
        await self.page.locator("input[type=password]").evaluate("el=>el.addEventListener('input',()=>document.querySelector('.priceInfo').textContent='￥999.00')")
        with self.assertRaisesRegex(StopOrder, "超过价格上限"):
            await self.session.order.submit()
        self.assertEqual(await self.page.locator("input[type=password]").input_value(), "654321")
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_password_fill_error_is_redacted(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        await self.add_password_inputs(6)
        self.session.payment_secret = PaymentSecret("654321")
        from playwright.async_api import Locator
        with patch.object(Locator, "fill", new=AsyncMock(side_effect=RuntimeError('fill("654321") failed'))):
            with self.assertRaises(StopOrder) as caught:
                await self.session.checkout.fill_payment_password()
        self.assertNotIn("654321", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.submit_hits, 0)

    async def test_password_error_prevents_filling_and_retry(self):
        await self.prepare_modern_checkout()
        await self.session.checkout.select_payment()
        await self.add_password_inputs(6)
        self.session.payment_secret = PaymentSecret("654321")
        await self.page.locator("body").evaluate("el=>el.insertAdjacentHTML('beforeend','<p>支付密码错误</p>')")
        with self.assertRaisesRegex(StopOrder, "校验未通过"):
            await self.session.order.submit()
        self.assertFalse(self.session.payment_secret_attempted)
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_dry_run_never_fills_payment_password(self):
        self.session.task.mode = "cart"
        self.session.task.source = "cart"
        self.session.task.cart_spec = "黑色 256GB"
        self.session.task.address_keyword = ""
        self.session.payment_secret = PaymentSecret("654321")
        self.checkout_html = self.modern_checkout_html().replace('</body>', '<div class="payment-summary-password__input"><input type="password"></div></body>')
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertEqual(await self.session.page.locator("input[type=password]").input_value(), "")
        self.assertFalse(self.session.payment_secret_attempted)
        self.assertEqual(self.submit_hits, 0)

    async def test_disabled_backorder_button_never_claims_or_clicks(self):
        self.checkout_html = CHECKOUT.replace('<button id="order-submit"', '<button disabled id="order-submit"').replace('>提交订单</button>', '>无货订购</button>')
        await self.checkout()
        from core.errors import Retryable
        with self.assertRaises(Retryable):
            await self.session.order.submit()
        self.assertIsNone(self.store.get(self.session.key))
        self.assertEqual(self.submit_hits, 0)

    async def test_wrong_checkout_sku_blocks(self):
        self.checkout_html = CHECKOUT.replace(SKU, "999999999")
        await self.checkout()
        with self.assertRaises(StopOrder):
            await self.session.checkout.validate()

    async def test_wrong_quantity_blocks(self):
        self.checkout_html = CHECKOUT.replace("x2", "x1")
        await self.checkout()
        with self.assertRaises(StopOrder):
            await self.session.checkout.validate()

    async def test_dry_run_full_flow_never_submits(self):
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))
        self.assertEqual(self.store.snapshot()["tasks"][0]["state"], "dry_run")

    async def test_product_link_task_saved_without_cart_scan_runs_direct_and_deduplicates(self):
        from datetime import timedelta
        from core.config_store import upsert_task
        self.cfg.path.write_text(json.dumps({'jd': self.cfg.jd, 'tasks': [],
            'timing': {'sync_clock': False}, 'notification': {'desktop': False},
            'logging': {'save_screenshots': False}}), encoding='utf-8')
        upsert_task(self.cfg.path, {'source': 'manual', 'product_url': f'https://item.jd.com/{SKU}.html',
            'quantity': 2, 'target_time': (datetime.now(CST) + timedelta(days=1)).isoformat()})
        cfg = Config.load(self.cfg.path)
        self.session = Session(self.context, self.page, cfg, cfg.tasks[0], self.store, self.scheduler)
        self.assertEqual(self.session.task.mode, 'direct')
        await run_one(self.session, asyncio.Lock(), dry_run=True)
        self.assertEqual(self.submit_hits, 0)
        # 下一次从管理页运行会取得新的任务页，而非重复使用这个没有账号栏的模拟结算页。
        self.session.page = await self.context.new_page()
        await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.cart_load_times, [])
        self.assertTrue(any(url == f'https://item.jd.com/{SKU}.html' for url in self.requests))
        with self.assertRaises(StopOrder):
            await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 1)

    async def test_product_ready_does_not_wait_for_unrelated_deferred_script(self):
        release = asyncio.Event()
        normal_route = self.route
        self.product_html = PRODUCT.replace('</body>', '<script defer src="https://item.jd.com/slow-product.js"></script></body>')
        async def route(request):
            if urlparse(request.request.url).path == '/slow-product.js':
                await release.wait()
                return await request.fulfill(content_type='application/javascript', body='window.extraScriptFinished=true')
            await normal_route(request)
        await self.context.unroute('**/*')
        await self.context.route('**/*', route)
        try:
            async with asyncio.timeout(2):
                info = await self.session.product.open()
            self.assertEqual(info['sku'], SKU)
            self.assertFalse(await self.page.evaluate('!!window.extraScriptFinished'))
            self.assertEqual(self.submit_hits, 0)
        finally:
            release.set()
            await self.page.wait_for_load_state('domcontentloaded', timeout=3000)

    async def test_auto_submit_false_stops_at_checkout(self):
        self.session.task.auto_submit = False
        await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 0)

    async def test_live_mock_submit_and_duplicate_protection(self):
        await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.get(self.session.key)[0], "confirmed")
        self.assertEqual(self.store.snapshot()["tasks"][0]["state"], "success")
        with self.assertRaises(StopOrder):
            await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 1)

    async def test_network_failure_recovers_with_bounded_retry(self):
        self.fail_checkout_once = True
        await run_one(self.session, asyncio.Lock(), now=True)
        attempts = self.store.db.execute("SELECT COUNT(*) FROM events WHERE state='attempt'").fetchone()[0]
        self.assertEqual(attempts, 2)
        self.assertEqual(self.submit_hits, 1)

    async def test_unknown_result_queries_order_center_without_resubmit(self):
        self.checkout_html = CHECKOUT.replace("/success", "/unknown")
        await self.checkout()
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.get(self.session.key)[0], "unknown")
        self.assertTrue(any("order.jd.com/center/list.action" in url for url in self.requests))
        with self.assertRaises(StopOrder):
            await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.submit_hits, 1)

    async def test_transient_stock_feedback_is_saved_during_click_before_order_query(self):
        self.checkout_html = CHECKOUT.replace("location.href='https://trade.jd.com/success'", """
            window.submitClicks=(window.submitClicks||0)+1;
            const feedback=document.createElement('div'); feedback.id='stock-feedback';
            feedback.textContent='抱歉，您本单购买的以下商品或赠品在所选择的地址下暂时无货';
            document.body.append(feedback);""")
        await self.checkout()
        captured = asyncio.Event()
        trace = []
        original = self.session.order.click_transition
        async def slow_transition(button):
            await original(button)
            await asyncio.wait_for(captured.wait(), 2)
            await self.page.locator('#stock-feedback').evaluate('el=>el.remove()')
            trace.append('click-returned')
        async def snapshot(reason):
            if reason == 'submit-feedback':
                row = self.store.db.execute("SELECT detail FROM events WHERE state='submit_feedback'").fetchone()
                self.assertIn('在所选择的地址下暂时无货', json.loads(row[0])['message'])
                self.assertEqual(await self.page.locator('#stock-feedback').count(), 1)
                trace.append('first-feedback-snapshot')
                captured.set()
        async def query(*args):
            self.assertTrue(captured.is_set())
            self.assertEqual(await self.page.locator('#stock-feedback').count(), 0)
            trace.append('query-order')
            return None
        with patch.object(self.session.order, 'click_transition', side_effect=slow_transition), \
                patch.object(self.session.order, 'snapshot', side_effect=snapshot), \
                patch.object(self.session.order, 'reconcile', side_effect=query):
            with self.assertRaisesRegex(StopOrder, '提交阶段页面提示.*暂时无货'):
                await self.session.order.submit()
        self.assertEqual(trace, ['first-feedback-snapshot', 'click-returned', 'query-order'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='submit_feedback'").fetchone()[0], 1)
        click = json.loads(self.store.db.execute("SELECT detail FROM events WHERE state='submit_click'").fetchone()[0])
        self.assertTrue(click['action_completed'])
        self.assertLessEqual(click['started'], click['ended'])
        self.assertEqual(self.store.get(self.session.key)[0], 'unknown')
        self.assertIn('暂时无货', self.store.get(self.session.key)[2])
        with self.assertRaisesRegex(StopOrder, '已有提交记录'):
            await self.session.order.submit()
        self.assertEqual(await self.page.evaluate('window.submitClicks'), 1)

    async def test_submit_feedback_screenshot_timeout_does_not_block_order_query(self):
        self.checkout_html = CHECKOUT.replace("location.href='https://trade.jd.com/success'",
            "document.body.insertAdjacentHTML('beforeend','<div>库存不足</div>')")
        await self.checkout()
        cancelled = asyncio.Event()
        async def snapshot(reason):
            if reason == 'submit-feedback':
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        query = AsyncMock(return_value=None)
        with patch.object(self.session.order, 'snapshot', side_effect=snapshot), \
                patch.object(self.session.order, 'reconcile', new=query):
            async with asyncio.timeout(5):
                with self.assertRaisesRegex(StopOrder, '库存不足'):
                    await self.session.order.submit()
        self.assertTrue(cancelled.is_set())
        query.assert_awaited_once()
        self.assertEqual(self.store.get(self.session.key)[0], 'unknown')

    async def test_cancelled_submit_cleans_up_feedback_watcher_and_keeps_unknown(self):
        await self.checkout()
        entered = asyncio.Event()
        async def blocked_click(button):
            entered.set()
            await asyncio.Event().wait()
        query = AsyncMock()
        with patch.object(self.session.order, 'click_transition', side_effect=blocked_click), \
                patch.object(self.session.order, 'reconcile', new=query):
            job = asyncio.create_task(self.session.order.submit())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
            finally:
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        self.assertFalse(any(task.get_name() == 'jd-submit-feedback:' + self.session.task.id
                             for task in asyncio.all_tasks() if not task.done()))
        self.assertEqual(self.store.get(self.session.key)[0], 'unknown')
        query.assert_not_awaited()

    async def test_unknown_result_reconciles_matching_order(self):
        await self.reconcile_matching_order("199.00")

    async def test_zero_payable_unknown_result_reconciles_matching_order(self):
        self.session.task.max_price = None
        await self.reconcile_matching_order("0.00")

    async def reconcile_matching_order(self, amount):
        self.session.baseline_ids = set()
        self.checkout_html = CHECKOUT.replace("/success", "/unknown").replace("￥199.00", "￥" + amount)
        stamp = "__SUBMITTED_AT__"
        self.order_html = f'''<table class="order-tb"><tbody data-sku="{SKU}"><tr><td>订单号：123456789012 待付款
            <span class="dealtime" title="{stamp}">{stamp}</span><a href="https://item.jd.com/{SKU}.html">手机</a>
            <div class="amount"><span class="ftx-13">￥{amount}</span></div><span class="goods-number">x2</span></td></tr></tbody></table>'''
        await self.checkout()
        found = await self.session.order.submit()
        self.assertEqual(found, "123456789012")
        self.assertEqual(self.store.get(self.session.key)[0], "confirmed")
        self.assertEqual(self.submit_hits, 1)

    async def test_existing_order_is_not_misidentified_as_new_order(self):
        self.session.baseline_ids = {"123456789012"}
        self.checkout_html = CHECKOUT.replace("/success", "/unknown")
        stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        self.order_html = f'''<table class="order-tb"><tbody data-sku="{SKU}"><tr><td>订单号：123456789012 待付款
            <span class="dealtime" title="{stamp}">{stamp}</span><a href="https://item.jd.com/{SKU}.html">手机</a>
            <div class="amount"><span class="ftx-13">￥199.00</span></div><span class="goods-number">x2</span></td></tr></tbody></table>'''
        await self.checkout()
        with self.assertRaises(StopOrder):
            await self.session.order.submit()
        self.assertEqual(self.store.get(self.session.key)[0], "unknown")
        self.assertEqual(self.submit_hits, 1)

    async def test_buy_button_popup_is_followed(self):
        self.product_html = PRODUCT.replace('id="btn-onkeybuy"', 'id="btn-onkeybuy" target="_blank"')
        await self.session.product.open()
        await self.session.product.buy()
        self.assertIn("trade.jd.com", self.session.page.url)

    async def test_login_credentials_survive_persistent_profile_restart(self):
        folder = str(self.root / "persistent-test")
        context = await self.p.chromium.launch_persistent_context(folder, channel="chrome", headless=True)
        try:
            await context.add_cookies([{"name": "pt_key", "value": "local", "domain": ".jd.com", "path": "/", "expires": time.time() + 3600}])
        finally:
            await context.close()
        context = await self.p.chromium.launch_persistent_context(folder, channel="chrome", headless=True)
        try:
            self.assertTrue(any(c["name"] == "pt_key" for c in await context.cookies("https://www.jd.com")))
        finally:
            await context.close()
