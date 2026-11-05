"""付款、限频弹窗及所有网络响应均为本地模拟，不向京东下单或扣款。"""
import asyncio
import json
import unittest
from decimal import Decimal
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

from core.errors import Retryable, StopOrder
from core.payment_secret import PaymentSecret
from jd.checkout import Quote
from jd.payment import AutoPayment
from jd.order import submission_feedback
from main import run_one
from tests import test_browser as fixtures


OID = '123456789012'
CASHIER = f'''<html><body>京东收银台 订单号：{OID}<p>支付金额：￥199.00</p>
<button id="pay-now" onclick="location.href='https://pay.jd.com/paid'">确认付款</button></body></html>'''
PAID = f'<html><body>支付成功 订单号：{OID}<p>实付金额：￥199.00</p></body></html>'


class PaymentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BrowserTests.asyncSetUp
    asyncTearDown = fixtures.BrowserTests.asyncTearDown
    credentials = fixtures.BrowserTests.credentials
    checkout = fixtures.BrowserTests.checkout

    async def route(self, route):
        parsed = urlsplit(route.request.url)
        if parsed.hostname == 'pay.jd.com':
            if parsed.path == '/paid':
                self.pay_hits = getattr(self, 'pay_hits', 0) + 1
                html = getattr(self, 'paid_html', PAID)
            else:
                self.cashier_hits = getattr(self, 'cashier_hits', 0) + 1
                html = getattr(self, 'cashier_html', CASHIER)
            return await route.fulfill(content_type='text/html; charset=utf-8', body=html)
        return await fixtures.BrowserTests.route(self, route)

    def enable(self):
        self.session.task.auto_pay = True
        self.cfg.jd['payment_timeout_seconds'] = 2
        self.cfg.jd['result_timeout_seconds'] = 2
        self.session.payment_secret = PaymentSecret('654321')

    async def test_auto_payment_is_disabled_for_existing_tasks(self):
        self.assertFalse(self.session.task.auto_pay)
        await self.checkout()
        await self.session.order.submit()
        self.assertIsNone(self.store.payment(self.session.key))
        self.assertEqual(getattr(self, 'pay_hits', 0), 0)

    async def test_dry_run_with_auto_payment_never_fills_or_pays(self):
        self.enable()
        self.assertTrue(await run_one(self.session, asyncio.Lock(), dry_run=True))
        self.assertEqual(self.submit_hits, 0)
        self.assertEqual(getattr(self, 'pay_hits', 0), 0)
        self.assertIsNone(self.store.payment(self.session.key))

    async def test_fast_cart_can_submit_then_pay_exactly_once(self):
        self.enable()
        self.session.task.mode = self.session.task.source = 'cart'
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        with patch.object(self.session.checkout, 'validate', new=AsyncMock(side_effect=AssertionError('不应重复核对结算'))):
            self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(self.cashier_hits, 1)
        self.assertEqual(self.pay_hits, 1)
        self.assertEqual(self.store.payment(self.session.key)[:2], ('paid', OID))

    async def test_fast_cart_auto_payment_still_needs_actual_amount(self):
        self.enable()
        self.session.task.mode = self.session.task.source = 'cart'
        self.checkout_html = fixtures.CHECKOUT.replace('id="sumPayPriceId"', 'id="unavailable-price"')
        await self.session.cart.select()
        await self.session.cart.checkout()
        with self.assertRaisesRegex(Retryable, '应付金额仍未就绪'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))
        self.assertIsNone(self.store.payment(self.session.key))

    async def test_cashier_default_button_pays_known_order_once(self):
        self.enable()
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(self.pay_hits, 1)
        self.assertEqual(self.store.payment(self.session.key)[:2], ('paid', OID))
        with self.assertRaises(StopOrder):
            await run_one(self.session, asyncio.Lock(), now=True)
        self.assertEqual(self.pay_hits, 1)

    async def test_cashier_password_native_form_is_filled_and_submitted(self):
        self.enable()
        self.cashier_html = CASHIER.replace('<button id="pay-now"', '''<form onsubmit="event.preventDefault();location.href='https://pay.jd.com/paid'">
            <div role="dialog">请输入支付密码<input type="password" maxlength="6">
            <button type="submit" id="confirm">确定</button></div></form><button hidden id="pay-now"''')
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(self.pay_hits, 1)
        self.assertEqual(self.store.payment(self.session.key)[0], 'paid')
        self.assertEqual(self.store.snapshot()['tasks'][0]['state'], 'paid')
        self.assertNotIn('654321', json.dumps(self.store.snapshot(), ensure_ascii=False))

    async def test_six_digit_automatic_password_widget_does_not_get_second_pay_click(self):
        self.enable()
        fields = ''.join('<input type="password" maxlength="1" oninput="checkComplete()">' for _ in range(6))
        self.checkout_html = fixtures.CHECKOUT.replace("location.href='https://trade.jd.com/success'", "openPassword()") + '''
        <script>function openPassword(){ const d=document.createElement('div'); d.setAttribute('role','dialog');
        d.innerHTML='请输入支付密码FIELDS';document.body.append(d)}
        function checkComplete(){if([...document.querySelectorAll('[role=dialog] input')].every(e=>e.value.length===1))
        location.href='https://pay.jd.com/paid'}</script>'''.replace('FIELDS', fields.replace("'", "\\'"))
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(self.pay_hits, 1)
        self.assertEqual(self.store.payment(self.session.key)[0], 'paid')
        self.assertEqual(self.store.snapshot()['tasks'][0]['state'], 'paid')

    async def test_payment_amount_increase_is_blocked_without_deduction(self):
        self.enable()
        self.cashier_html = CASHIER.replace('199.00', '299.00')
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(getattr(self, 'pay_hits', 0), 0)
        self.assertEqual(self.store.get(self.session.key)[0], 'confirmed')
        self.assertEqual(self.store.payment(self.session.key)[0], 'unknown')

    async def test_cashier_wrong_order_is_not_paid(self):
        self.enable()
        self.session.submitted = True
        self.store.claim(self.session.key, self.session.task.id, Decimal('199'))
        await self.checkout()
        self.session.auto_payment.arm(Quote(Decimal('199'), None, None, ''))
        self.cashier_html = CASHIER.replace(OID, '999999999999')
        await self.page.goto('https://pay.jd.com/cashier')
        with self.assertRaisesRegex(StopOrder, '订单号'):
            await self.session.auto_payment.tick(Quote(Decimal('199'), None, None, ''), OID)
        self.assertEqual(getattr(self, 'pay_hits', 0), 0)

    async def test_sms_verification_is_not_clicked_or_filled(self):
        self.enable()
        self.cashier_html = CASHIER.replace('<button id="pay-now"', '<p>请输入短信验证码</p><input id="sms"><button id="pay-now"')
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(await self.page.locator('#sms').input_value(), '')
        self.assertEqual(getattr(self, 'pay_hits', 0), 0)
        self.assertNotEqual(self.store.payment(self.session.key)[0], 'paid')

    async def test_unknown_payment_does_not_repeat_click_after_timeout(self):
        self.enable()
        self.cashier_html = CASHIER.replace("location.href='https://pay.jd.com/paid'", "window.payClicks=(window.payClicks||0)+1")
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(await self.page.evaluate('window.payClicks'), 1)
        self.assertEqual(self.store.payment(self.session.key)[0], 'unknown')
        other = AutoPayment(self.session)
        other.arm(self.session.checkout_quote)
        with self.assertRaisesRegex(StopOrder, '已有付款记录'):
            await other.tick(self.session.checkout_quote, OID)
        self.assertEqual(await self.page.evaluate('window.payClicks'), 1)

    async def test_rate_limit_dialog_is_closed_then_current_checkout_retries_once(self):
        self.checkout_html = fixtures.CHECKOUT.replace("location.href='https://trade.jd.com/success'", "trySubmit()") + '''
        <script>function trySubmit(){window.clicks=(window.clicks||0)+1;
        if(window.clicks===1){const d=document.createElement('div');d.id='limit-dialog';
          d.innerHTML='<p>您多次提交过快，请稍后再试</p><button onclick="this.parentNode.remove()">知道了</button>';
          document.body.append(d)}else{location.href='https://trade.jd.com/success'}}</script>'''
        await self.checkout()
        with patch.object(self.session.order, 'reconcile', new=AsyncMock(side_effect=AssertionError('明确拒绝不应先等查单超时'))):
            self.assertEqual(await self.session.order.submit(), OID)
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='submit_click'").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='submission_prompt_closed'").fetchone()[0], 1)

    async def test_unknown_dialog_is_not_dismissed_and_does_not_resubmit(self):
        self.checkout_html = fixtures.CHECKOUT.replace("location.href='https://trade.jd.com/success'", "window.clicks=(window.clicks||0)+1")
        self.checkout_html += '<div role="dialog">请同意新增服务协议<button onclick="window.unknownClicked=true">知道了</button></div>'
        await self.checkout()
        with patch.object(self.session.order, 'reconcile', new=AsyncMock(return_value=None)):
            with self.assertRaises(StopOrder):
                await self.session.order.submit()
        self.assertFalse(await self.page.evaluate('!!window.unknownClicked'))
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_repeated_rate_limit_is_bounded(self):
        self.session.task.retry['max_attempts'] = 2
        self.checkout_html = fixtures.CHECKOUT.replace("location.href='https://trade.jd.com/success'", "trySubmit()") + '''
        <script>function trySubmit(){window.clicks=(window.clicks||0)+1;const d=document.createElement('div');
        d.innerHTML='<p>您多次提交过快，请稍后再试</p><button onclick="this.parentNode.remove()">知道了</button>';
        document.body.append(d)}</script>'''
        await self.checkout()
        with self.assertRaisesRegex(StopOrder, '重试次数或时间窗口耗尽'):
            await self.session.order.submit()
        self.assertEqual(await self.page.evaluate('window.clicks'), 2)
        self.assertEqual(self.store.get(self.session.key)[0], 'unknown')

    async def test_existing_rate_popup_is_closed_before_the_only_submit_click(self):
        self.checkout_html = fixtures.CHECKOUT + '''<div><p>您多次提交过快，请稍后再试</p>
        <button onclick="this.parentNode.remove()">知道了</button></div>'''
        await self.checkout()
        self.assertEqual(await self.session.order.submit(), OID)
        self.assertEqual(self.submit_hits, 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE state='submit_click'").fetchone()[0], 1)

    async def test_rate_popup_without_unique_close_control_does_not_submit(self):
        self.checkout_html = fixtures.CHECKOUT + '<div><p>您多次提交过快，请稍后再试</p></div>'
        await self.checkout()
        with self.assertRaisesRegex(StopOrder, '无法确认关闭'):
            await self.session.order.submit()
        self.assertEqual(self.submit_hits, 0)
        self.assertIsNone(self.store.get(self.session.key))

    async def test_rate_limit_recovery_then_auto_payment_keeps_one_intent(self):
        self.enable()
        self.checkout_html = fixtures.CHECKOUT.replace("location.href='https://trade.jd.com/success'", "trySubmit()") + '''
        <script>function trySubmit(){window.clicks=(window.clicks||0)+1;
        if(window.clicks===1){const d=document.createElement('div');
          d.innerHTML='<p>您多次提交过快，请稍后再试</p><button onclick="this.parentNode.remove()">知道了</button>';
          document.body.append(d)}else{location.href='https://pay.jd.com/cashier'}}</script>'''
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertEqual(self.pay_hits, 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM payments').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM submissions').fetchone()[0], 1)
        self.assertEqual(self.store.payment(self.session.key)[:2], ('paid', OID))

    async def test_paid_help_text_does_not_claim_receipt_success(self):
        self.enable()
        self.cashier_html = CASHIER.replace("location.href='https://pay.jd.com/paid'", "window.payClicks=(window.payClicks||0)+1")
        self.cashier_html += '<footer>支付成功后可以在哪里查询？已付款订单如何退款？</footer>'
        self.checkout_html = fixtures.CHECKOUT.replace('https://trade.jd.com/success', 'https://pay.jd.com/cashier')
        self.assertTrue(await run_one(self.session, asyncio.Lock(), now=True))
        self.assertNotEqual(self.store.payment(self.session.key)[0], 'paid')
        self.assertEqual(await self.page.evaluate('window.payClicks'), 1)


class FeedbackTests(unittest.TestCase):
    def test_rate_limit_message_and_server_delay(self):
        result = submission_feedback('您多次提交过快，请2秒后重试')
        self.assertEqual(result['kind'], 'rate_limit')
        self.assertEqual(result['retry_after_seconds'], 2)
