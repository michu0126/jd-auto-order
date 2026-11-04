"""商品页内的京东轻量结算；付款请求在订单生成后被隔离。"""
import re
from urllib.parse import parse_qs, urlsplit

from core.config import money
from core.errors import StopOrder
from jd.checkout import Checkout
from jd.order import Order


HOST = 'pc-settlement-lite-pro.pf.jd.com'
FRAME_SELECTOR = 'iframe[src^="https://' + HOST + '"]'
LITE_SELECTORS = {
    'checkout_goods': '.sku-list > .sku-item:has([class^="goods-"])',
    'checkout_quantity': 'input.number-display',
    'submit': '[class^="submitOrder-"] > [class^="submit-"]',
    'total': '[class^="submitOrder-"] > [class^="priceInfo-"] > [class^="price-"]',
    'subtotal': '.bill-item:has(.bill-item__title em:text-is("商品总额")) > .bill-item__price',
    'freight': '.bill-item:has(.bill-item__title em:text-matches("^(运费|配送费|邮费)$")) > .bill-item__price',
    'discount': '.bill-item:has(.bill-item__title em:text-is("共减")) > .bill-item__price',
    'address': '[class^="consignee_card_addressDetail-"]',
}


class FrameSurface:
    """DOM 操作落在 Frame，窗口、截图和弹窗事件仍由真实 Page 负责。"""
    owner_methods = {'screenshot', 'on', 'remove_listener', 'expect_popup', 'bring_to_front', 'close', 'goto'}

    def __init__(self, owner, frame):
        self.owner, self.frame = owner, frame

    @property
    def root(self):
        return self.owner if self.frame.is_detached() else self.frame

    @property
    def url(self):
        return self.root.url

    def is_closed(self):
        return self.owner.is_closed()

    async def parent_hit(self, node):
        point = await node.evaluate('el => {const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}}')
        frame_element = await self.frame.frame_element()
        return await frame_element.evaluate("""(el,p) => {
            const r=el.getBoundingClientRect();
            return document.elementFromPoint(r.x+el.clientLeft+p.x,r.y+el.clientTop+p.y)===el;
        }""", point)

    def __getattr__(self, name):
        return getattr(self.owner if name in self.owner_methods or not hasattr(self.root, name) else self.root, name)


async def bind_embedded(session):
    if isinstance(session.page, FrameSurface):
        return True
    owner = session.page
    source = urlsplit(owner.url)
    if source.hostname != 'item.jd.com' or source.path != '/' + session.task.sku + '.html':
        return False
    candidates = []
    for frame in owner.frames:
        if frame == owner.main_frame:
            continue
        parsed = urlsplit(frame.url)
        if parsed.scheme != 'https' or parsed.hostname != HOST or parsed.path != '/':
            continue
        element = await frame.frame_element()
        if not await element.is_visible():
            continue
        top = parse_qs(parsed.query).get('topUrl', [])
        if len(top) != 1 or urlsplit(top[0]).hostname != 'item.jd.com' or urlsplit(top[0]).path != '/' + session.task.sku + '.html':
            raise StopOrder('嵌入式结算来源与任务商品不一致，停止操作')
        candidates.append(frame)
    if not candidates:
        return False
    if len(candidates) != 1 or session.task.mode != 'direct':
        raise StopOrder('无法唯一确定本任务的嵌入式结算窗口')
    session.page = FrameSurface(owner, candidates[0])
    session.selectors = session.base_selectors | LITE_SELECTORS | session.cfg.selectors
    session.checkout = EmbeddedCheckout(session)
    session.order = EmbeddedOrder(session)
    session.store.event(session.task.id, 'embedded_checkout', '已识别商品页内嵌京东结算，保留默认支付')
    return True


class EmbeddedCheckout(Checkout):
    async def validate_goods(self, goods):
        quantities = []
        for card in await goods.all():
            if not await card.is_visible():
                continue
            await self.verify_goods_identity(card)
            quantity = await self.find('checkout_quantity', scope=card)
            value = await quantity.input_value()
            if not re.fullmatch(r'[1-9]\d*', value):
                raise StopOrder('嵌入式结算商品数量不可识别')
            quantities.append(int(value))
        # 推荐加购区域不属于主商品卡；不能悄悄购买已选中的推荐品。
        extra = await self.page.locator('input.number-display').evaluate_all("""inputs => inputs.some(e =>
            e.getClientRects().length && !e.closest('.sku-item')?.querySelector('[class^="goods-"]') && Number(e.value)!==0)""")
        if extra:
            raise StopOrder('嵌入式结算含未授权的推荐加购商品，停止提交')
        return quantities

    async def freight_not_itemized(self):
        if 'freight' in self.s.cfg.selectors:
            return False
        rows = self.page.locator('.bill-item').filter(visible=True)
        labels = await rows.locator('.bill-item__title em').all_inner_texts()
        return (labels.count('商品总额') == 1 and
            not any(re.search('运费|配送费|邮费', label) for label in labels) and
            await self.page.locator(LITE_SELECTORS['total']).filter(visible=True).count() == 1)

    async def validate(self):
        quote = await super().validate()
        self.s.checkout_quote = quote
        return quote


class OrderOnlyGuard:
    """只转发浏览器原生生成的一次下单请求，不构造私有请求或支付参数。

    已观察到的轻量结算会在下单成功后直接请求收单和支付 SDK。
    下单响应返回前先封住此页后续网络，结束时关闭脚本创建的商品页，
    再移除拦截，避免延迟付款请求在断开脚本后继续执行。
    """
    def __init__(self, session):
        self.s = session
        self.surface = session.page
        self.owner = self.surface.owner
        self.handler = self.handle
        self.forwarded = False
        self.accepted = None
        self.order_response_received = False
        self.blocked = 0

    async def install(self):
        if await self.surface.evaluate('() => !!navigator.serviceWorker?.controller'):
            raise StopOrder('该结算页由 Service Worker 控制，不能保证隔离自动支付，停止最终提交')
        await self.s.context.route('**/*', self.handler)

    async def owns(self, request):
        try:
            page = request.frame.page
            for _ in range(4):
                if page == self.owner:
                    return True
                page = await page.opener()
                if page is None:
                    break
        except Exception:
            return False
        return False

    @staticmethod
    def function_id(request):
        params = parse_qs(urlsplit(request.url).query)
        if request.post_data:
            params = params | parse_qs(request.post_data)
        values = params.get('functionId', [])
        return values[0] if len(values) == 1 else ''

    async def handle(self, route):
        request = route.request
        if not await self.owns(request):
            return await route.fallback()
        parsed = urlsplit(request.url)
        order_request = (request.method == 'POST' and parsed.scheme == 'https' and
            (parsed.hostname == 'jd.com' or (parsed.hostname or '').endswith('.jd.com')) and
            request.frame == self.surface.frame and self.function_id(request) == 'balance_submitOrder_pc')
        if order_request and not self.forwarded:
            self.forwarded = True
            response = await self.forward(route)
            # 无论成功或失败，收到响应后都不再放行本页后续支付或重复下单。
            self.order_response_received = True
            try:
                data = await response.json()
                body = data.get('body', {}) if data.get('code') in ('0', 0) else {}
                order = body.get('order', {})
                ids = str(order.get('orderIdStr', ''))
                quote = getattr(self.s, 'checkout_quote', None)
                if (body.get('resultFlag') is True and re.fullmatch(r'\d{6,30}', ids) and quote is not None
                        and money(str(order.get('orderPrice', '')), allow_zero=True) == quote.total):
                    self.accepted = ids
                    self.s.store.event(self.s.task.id, 'order_response',
                        {'order_id': ids, 'total': str(quote.total), 'payment_blocked': True}, update_state=False)
            except (ValueError, AttributeError, StopOrder, TypeError):
                pass
            return await route.fulfill(response=response)
        if self.order_response_received or request.method not in ('GET', 'HEAD', 'OPTIONS') or order_request:
            self.blocked += 1
            return await route.abort('blockedbyclient')
        return await route.fallback()

    async def forward(self, route):
        return await route.fetch(max_retries=0, max_redirects=0, timeout=20000)

    async def close(self):
        self.order_response_received = True
        try:
            # 只有详情页任务可绑定此类窗口；它们由脚本新建，不复用用户购物车。
            owned = []
            for page in list(self.s.context.pages):
                current = page
                for _ in range(4):
                    if current == self.owner:
                        owned.append(page)
                        break
                    current = await current.opener()
                    if current is None:
                        break
            for page in reversed(owned):
                if not page.is_closed():
                    await page.close()
        finally:
            await self.s.context.unroute('**/*', self.handler)


class EmbeddedOrder(Order):
    def success(self, text):
        return self.payment_guard.accepted

    async def submit(self):
        if self.s.task.auto_pay:
            raise StopOrder('商品页内嵌结算的自动付款尚需真实收银台适配；请使用购物车或普通结算页，或关闭本任务自动付款')
        if getattr(self.s, 'parallel_detail_count', 1) > 1:
            raise StopOrder('同账号多个详情页的轻量结算已观察到串商品；停止本路最终提交，不能承诺独立并行')
        self.payment_guard = OrderOnlyGuard(self.s)
        await self.payment_guard.install()
        try:
            return await super().submit()
        finally:
            await self.payment_guard.close()
