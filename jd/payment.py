"""明确启用后的付款 UI 流程。付款意图先落盘，结果不明时不重复扣款。"""
import asyncio
import logging
import re
import time
from urllib.parse import urlparse

from core.config import money
from core.errors import StopOrder
from jd.common import PageTools
from jd.dom import main_body_text
from jd.submit_button import checkout_url
from utils.notifier import notify
from playwright.async_api import Error as PlaywrightError


LOG = logging.getLogger(__name__)
PAY_NAMES = re.compile(r'^(?:立即支付|确认支付|确认付款|立即付款|确认并支付|支付|付款)(?:\s*[¥￥]\s*\d+(?:\.\d{1,2})?)?$')
PASSWORD_NAMES = re.compile(r'支付密码|交易密码')
CHALLENGE = re.compile(r'短信验证码|输入验证码|指纹验证|人脸识别|刷脸验证|验证一下[，,]?\s*购物无忧|请完成安全验证|请扫码|扫码支付')
ERROR = re.compile(r'支付密码(?:错误|不正确|已锁定)|密码输入错误|密码不正确|密码已锁定|支付失败|付款失败|银行卡余额不足|交易被拒绝')
PAID = re.compile(r'(?:^|[\r\n。！!])\s*(?:支付成功|付款成功|订单(?:支付)?状态\s*[:：]?\s*已支付)(?:[！!。\s]|$)')
IDS = re.compile(r'(?:订单号|订单编号)\s*[:：]?\s*(\d{6,30})(?!\d)')
AMOUNTS = re.compile(r'(?:应付金额|实付金额|支付金额|付款金额|订单金额|需支付|应付)\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?')


def payment_domain(url):
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and (host == 'jd.com' or host.endswith('.jd.com') or
                                         host == 'jdpay.com' or host.endswith('.jdpay.com'))


class AutoPayment(PageTools):
    def __init__(self, session):
        super().__init__(session)
        self.origin = None
        self.before = set()
        self.quote = None
        self.order_id = ''
        self.reserved = False
        self.triggered = False
        self.password_filled = False
        self.status = 'idle'
        self.notified = set()

    def arm(self, quote):
        if not self.s.task.auto_pay:
            return
        if self.origin is None:
            self.origin = getattr(self.page, 'owner', self.page)
            self.before = set(self.s.context.pages)
        self.quote = quote

    def reserve(self, amount, order_id=''):
        if not self.reserved:
            self.s.store.claim_payment(self.s.key, self.s.task.id, amount, order_id)
            self.reserved = True
        if order_id:
            self.order_id = order_id

    async def manual(self, reason):
        self.status = 'manual'
        if reason not in self.notified:
            self.notified.add(reason)
            self.s.store.event(self.s.task.id, 'payment_manual', reason)
            await notify(self.s.cfg.notification['desktop'], '京东付款需要人工操作', self.s.task.name + '：' + reason)
        return self.status

    async def adopt_popup(self):
        candidates = []
        for page in self.s.context.pages:
            if page in self.before or page.is_closed() or not payment_domain(page.url):
                continue
            if await page.opener() is self.origin:
                candidates.append(page)
        if len(candidates) > 1:
            raise StopOrder('出现多个付款窗口，请人工核实订单，未猜选付款窗口')
        if candidates:
            self.s.page = candidates[0]

    async def confirm_paid(self, oid, amount):
        self.status = 'paid'
        self.order_id = oid
        if not self.reserved:
            self.reserve(amount, oid)
        self.s.store.finish_payment(self.s.key, 'paid', oid, '已核对支付成功页面、订单号及金额', amount=amount)
        self.s.store.event(self.s.task.id, 'paid', {'order_id': oid, 'amount': str(amount)})
        LOG.info('[%s] 已核对付款成功，订单号=%s 金额=%s', self.s.task.id, oid, amount)
        await notify(self.s.cfg.notification['desktop'], '京东付款成功', f'{self.s.task.name}，¥{amount}，订单号 {oid}')
        return self.status

    async def tick(self, quote, order_id=''):
        if not self.s.task.auto_pay or self.status in ('paid', 'failed', 'unknown'):
            return self.status
        submission = self.s.store.get(self.s.key)
        if not self.s.submitted or not submission or submission[0] not in ('submitting', 'confirmed'):
            raise StopOrder('缺少本任务的已提交订单证据，未发起付款')
        self.s.scheduler.check_stop()
        self.arm(quote)
        if order_id:
            if self.order_id and self.order_id != order_id:
                raise StopOrder('付款订单号与本任务已绑定订单不一致')
            self.order_id = order_id
        await self.adopt_popup()
        if not payment_domain(self.page.url):
            return await self.manual('付款页面不在已支持的京东域名，请在浏览器完成')
        text = await main_body_text(self.page)
        ids = set(IDS.findall(text))
        if len(ids) > 1 or (self.order_id and ids and ids != {self.order_id}):
            raise StopOrder('付款页面订单号不唯一或与本任务不一致，未付款')
        oid = self.order_id or (next(iter(ids)) if len(ids) == 1 else '')
        amounts = {money(value, allow_zero=True) for value in AMOUNTS.findall(text)}
        amount = next(iter(amounts)) if len(amounts) == 1 else None
        on_checkout = checkout_url(self.page.url)
        if on_checkout:
            # 原结算文档没有订单号时，仍由本轮已核对的商品、数量、金额绑定付款。
            if getattr(self.page, 'owner', self.page) is not self.origin:
                raise StopOrder('付款验证与本轮结算页面来源不一致')
            current = await self.s.checkout.amount('total', required=True)
            if current != quote.total:
                raise StopOrder('付款前结算应付已变化，未继续付款')
            amount = current
        if amount is not None and amount > quote.total:
            raise StopOrder('付款金额超过本轮已核对订单金额，未付款')
        if PAID.search(text) and oid and amount is not None and ids == {oid}:
            return await self.confirm_paid(oid, amount)
        error = ERROR.search(text)
        if error:
            self.status = 'failed'
            if self.reserved:
                self.s.store.finish_payment(self.s.key, 'unknown', oid, '页面提示：' + error[0] + '；不重复扣款')
            self.s.store.event(self.s.task.id, 'payment_failed', error[0])
            return self.status
        if CHALLENGE.search(text):
            return await self.manual('页面要求短信、指纹、人脸或扫码验证，请在浏览器完成，完成后继续核实')
        if amount is None or (not on_checkout and (not oid or ids != {oid})):
            return 'waiting'
        dialogs = self.page.locator('[role=dialog], [aria-modal=true], dialog[open]').filter(has_text=PASSWORD_NAMES).filter(visible=True)
        fields = None
        scope = self.page
        if await dialogs.count() > 1:
            raise StopOrder('出现多个付款密码框，未继续付款')
        if await dialogs.count() == 1:
            scope = dialogs.first
            fields = scope.locator('input[type=password]:visible, input[inputmode=numeric][maxlength="1"]:visible')
        elif not on_checkout and PASSWORD_NAMES.search(text):
            fields = scope.locator('input[type=password]:visible')
        if not on_checkout and not self.order_id:
            return 'waiting'  # 收银台动作先由订单结果流程绑定订单号。
        if fields is not None and await fields.count():
            if self.password_filled:
                return 'waiting'
            if not self.s.payment_secret:
                return await self.manual('请输入本次运行支付密码，或直接在京东付款框完成')
            count = await fields.count()
            if count not in (1, 6):
                return await self.manual('付款密码控件结构未识别，请在京东完成')
            if not self.password_filled:
                # 部分密码组件在第六位输入时即发起扣款，填写前就保存付款意图。
                self.reserve(amount, oid)
                self.password_filled = True
                self.triggered = True
                try:
                    value = self.s.payment_secret.value()
                    if count == 1:
                        await fields.fill(value)
                    else:
                        for index, digit in enumerate(value):
                            await fields.nth(index).fill(digit)
                except Exception:
                    raise StopOrder('付款密码填写未完成，请核实支付结果，不重复填写') from None
                self.s.store.event(self.s.task.id, 'payment_password_filled', '已填写本次付款密码，不记录密码内容', update_state=False)
            controls = scope.get_by_role('button', name=re.compile(r'^(?:确认支付|确认付款|立即支付|确定|确认)$')).or_(
                scope.get_by_role('link', name=re.compile(r'^(?:确认支付|确认付款|立即支付|确定|确认)$'))).filter(visible=True)
            # 原生表单有明确的提交按钮时才补按一次；六格/自动提交组件仅填入并等待结果。
            if (count == 1 and await controls.count() == 1 and await fields.count() and await controls.is_enabled()
                    and await controls.evaluate("el=>el.tagName==='BUTTON'&&el.type==='submit'&&!!el.form")):
                await self.activate(controls, timeout=self.s.cfg.jd['action_timeout_ms'])
            self.status = 'waiting'
            return self.status
        if on_checkout:
            return 'waiting'  # 不能把原订单提交按钮当成第二次付款按钮。
        # 已按过收银台付款入口时仍可处理新出现的密码框，但不会再次按同一个付款入口。
        if self.triggered:
            return 'waiting'
        controls = self.page.get_by_role('button', name=PAY_NAMES).or_(self.page.get_by_role('link', name=PAY_NAMES)).filter(visible=True)
        if await controls.count() == 1 and await controls.is_enabled():
            self.reserve(amount, oid)
            self.triggered = True
            await self.activate(controls, timeout=self.s.cfg.jd['action_timeout_ms'])
            self.status = 'waiting'
        return self.status

    async def finish(self, quote, order_id):
        if self.status == 'paid':
            self.s.store.event(self.s.task.id, 'paid', {'order_id': self.order_id})
            return self.status
        deadline = time.monotonic() + self.s.cfg.jd['payment_timeout_seconds']
        try:
            while time.monotonic() < deadline:
                status = await self.advance(quote, order_id)
                if status in ('paid', 'failed', 'unknown'):
                    if status == 'failed':
                        self.s.store.event(self.s.task.id, 'payment_failed', '订单已生成，付款未完成，请核实支付结果')
                    return status
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            if self.reserved:
                self.s.store.finish_payment(self.s.key, 'unknown', self.order_id, '付款阶段被取消，请核实支付结果')
            raise
        except Exception as exc:
            LOG.warning('[%s] 自动付款未确认：%s', self.s.task.id, type(exc).__name__)
            await self.manual(str(exc) if isinstance(exc, StopOrder) else '付款页面暂不可操作，请人工核实')
        self.status = 'unknown' if self.reserved else 'manual'
        if self.reserved:
            self.s.store.finish_payment(self.s.key, 'unknown', self.order_id, '付款结果未确认，不重复扣款')
        self.s.store.event(self.s.task.id, 'payment_' + self.status, '订单已生成，付款仍需核实')
        return self.status

    async def advance(self, quote, order_id=''):
        try:
            return await self.tick(quote, order_id)
        except PlaywrightError as exc:
            if not self.page.is_closed() and 'Execution context was destroyed' in str(exc):
                return 'waiting'
            raise
