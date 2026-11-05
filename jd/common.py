import asyncio
import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from core.config import CST
from core.checkout_labels import SUBMIT_BUTTON_TEXTS
from core.errors import LoginRequired, Retryable, StopOrder, VerificationRequired
from jd.auth import AUTH_USER, observe
from jd.submit_button import checkout_url, find_submit

LOG = logging.getLogger(__name__)
SELECTORS = {
    "auth_user": AUTH_USER,
    "product_name": ".sku-name, #name h1, #page-right-information .sku-title-name",
    "spec_group": "#choose-attrs .li, #choose-attrs > div, .choose-attrs .li, .specifications-panel-content .specification-group, .specification-series-layout",
    "spec_label": ".dt, .label, .specification-group-label, .layout-label",
    "spec_item": ".item, [data-value], .specification-item-sku, .specification-series-item",
    "spec_selected": ".item.selected, [aria-checked=true], .specification-item-sku--selected, .specification-series-item--selected",
    "add_cart": "#InitCartUrl, .bottom-btns-root > #add-to-cart",
    "buy_now": "#btn-onkeybuy, #btn-buy, a:has-text('立即抢购'), button:has-text('立即抢购'), a:has-text('立即购买'), button:has-text('立即购买'), .bottom-btns-root > div:has(.first-row > div:text-is('立即购买')), .bottom-btns-root > div:has(.first-row > div:text-is('立即抢购')), .bottom-btns-root > div:has(.first-row > div:text-is('抢购'))",
    "product_quantity": "#buy-num",
    "stock": "#store-prompt, #store-selector .text, .store-prompt, .logistics-delivery-time",
    "cart_row": ".item-item, [data-skuuuid]",
    "cart_checkbox": "input[type=checkbox]",
    "cart_checkbox_click": ".cart-checkbox, label:has(input[type=checkbox])",
    "cart_quantity": ".p-quantity input.itxt, [class*='_good-count_'] input:not([type=checkbox]), [class*='_cart-number_'] input:not([type=checkbox]), input[type=number], input[type=text]",
    "checkout": ".submit-btn, .common-submit-btn, [class*='_submitBtn_']",
    "checkout_goods": ".goods-list .goods-item, #goods-list .goods-item, .sku-list > .sku-item",
    "checkout_quantity": ".p-num, .goods-number, .quantity, [class^='quantity-']",
    "total": "#sumPayPriceId, #sumPayPrice, #payPriceId, .payment-action .priceInfo",
    "subtotal": "#warePriceId, #warePrice, .payment-summary-item:has(.payment-summary-item__title em:text-is('商品总额')) .payment-summary-item__price",
    "freight": "#freightPriceId, #freightPrice, .payment-summary-item:has(.payment-summary-item__title em:text-is('运费')) .payment-summary-item__price",
    "discount": "#discountPriceId, #couponPriceId, #coudan_price, .payment-summary-item:has(.payment-summary-item__title em:text-is('共减')) .payment-summary-item__price",
    "address": "#sendAddr, .send-addr, .consignee-cont .item-selected, .addr-detail",
    "submit": "#order-submit, #submitOrder, .payment-action-submit button, .payment-action-submit > .btn",
    "beans": "#useJdBean, #useBean, input[name=jdBean]",
    "red_packets": "#useRedPacket, input[name=redPacket]",
    "coupon": ".coupon-item, .coupon-list .item",
    "order_row": ".order-tb tbody, #order-list .tbody",
    "order_time": ".dealtime",
    "order_amount": ".amount .ftx-13, .amount .amount-price",
    "order_quantity": ".goods-number",
    "payment_options": ".payment-item, .payment-list [data-value], [role=radio]",
    "payment_password_dialog": "[role=dialog], dialog[open], [aria-modal=true]",
    "payment_password_dialog_close": "[aria-label='关闭'], [aria-label='Close'], [title='关闭'], [title='Close']",
}
ROLES = {"checkout": ("去结算",), "submit": SUBMIT_BUTTON_TEXTS,
         "buy_now": ("立即购买", "立即抢购", "抢购"), "add_cart": ("加入购物车",)}
CHECKOUT_FIELDS = {"submit", "checkout_goods", "checkout_quantity", "total", "subtotal",
                   "freight", "discount", "address", "beans", "red_packets", "coupon"}


async def first_visible(scope, selector):
    nodes = scope.locator(selector)
    for index in range(await nodes.count()):
        item = nodes.nth(index)
        if await item.is_visible():
            return item
    return None


SKU_IDS_JS = """el => {
        const ids = [el.getAttribute('data-sku'), el.getAttribute('skuId'), el.getAttribute('data-sku-id')];
        const main = el.querySelectorAll('a[class*="_goodTitle_"], .p-name a[href], a[data-cart-title]');
        for (const a of (main.length ? main : el.querySelectorAll('a[href]'))) {
            const m = a.href.match(/item\\.jd\\.com\\/(\\d+)\\.html/);
            if (m) ids.push(m[1]);
        }
        return [...new Set(ids.filter(Boolean))];
    }"""


async def sku_ids(node):
    return await node.evaluate(SKU_IDS_JS)


def quantity_text(text):
    match = re.fullmatch(r"(?:\d+(?:\.\d+)?\s*(?:kg|g)\s*)?(?:[xX×*]\s*)?(\d+)\s*(?:件)?", text.strip(), re.I)
    if not match:
        raise StopOrder("无法识别结算数量，请更新 checkout_quantity 选择器")
    return int(match[1])


class PageTools:
    def __init__(self, session):
        self.s = session

    @property
    def page(self):
        return self.s.page

    def page_location(self):
        # 日志只记录站点和路径，不携带账号、地址或临时查询参数。
        parsed = urlparse(self.page.url)
        return (parsed.hostname or parsed.scheme) + parsed.path

    async def require_checkout(self):
        if checkout_url(self.page.url):
            return
        await self.guard()  # 登录/人工验证按原流程暂停，不能当成普通导航失败。
        if not checkout_url(self.page.url):
            raise Retryable(f"当前不是订单结算页，尚未进入或已返回购物车；页面={self.page_location()}")

    async def find(self, key, required=True, scope=None, timeout=None, check_page=False):
        scope = scope if scope is not None else self.page
        deadline = time.monotonic() + (timeout if timeout is not None else self.s.cfg.jd["action_timeout_ms"]) / 1000
        while True:
            if key in CHECKOUT_FIELDS and (required or key != "submit"):
                await self.require_checkout()
            item = (await find_submit(self.page, self.s.selectors[key], self.s.cfg.jd["submit_button_texts"], "submit" in self.s.cfg.selectors)
                    if key == "submit" else await first_visible(scope, self.s.selectors[key]))
            if item is not None:
                return item
            if key != "submit" and key not in self.s.cfg.selectors:
                for label in ROLES.get(key, ()):
                    for role in ("button", "link"):
                        nodes = scope.get_by_role(role, name=label, exact=True)
                        if await nodes.count() == 1 and await nodes.is_visible():
                            return nodes
            if not required:
                return None
            self.s.scheduler.check_stop()
            if check_page:
                # 等待期间也可能跳到验证页，不能等满控件超时再刷新掉验证。
                await self.guard()
            if time.monotonic() >= deadline:
                raise Retryable(f"未找到 {key}，可能尚未开售或需要更新选择器；页面={self.page_location()}")
            await asyncio.sleep(0.05)

    async def guard(self):
        await self.wait_body()
        await self.check_auth()

    def check_domain(self):
        if self.page.url.startswith("chrome-error://"):
            raise Retryable("浏览器显示网络错误页")
        host = urlparse(self.page.url).hostname or ""
        if host != "jd.com" and not host.endswith(".jd.com"):
            raise StopOrder("页面跳到京东域名以外，停止操作")

    async def wait_body(self):
        self.check_domain()
        # 使用可取消的短读检查；在开售边界取消等待时，不遗留浏览器内的长等待任务。
        deadline = time.monotonic() + self.s.cfg.jd["action_timeout_ms"] / 1000
        while not await self.page.evaluate("() => !!(document.body && document.body.innerText.trim().length > 0)"):
            self.s.scheduler.check_stop()
            if time.monotonic() >= deadline:
                raise Retryable("页面内容仍在加载，等待响应后重试")
            await asyncio.sleep(.05)

    async def check_auth(self):
        self.check_domain()
        host = urlparse(self.page.url).hostname or ""
        auth = await observe(self.page, self.s.context, self.s.selectors)
        if auth.state == "logged_out" and host != "passport.jd.com":
            # 普通 Chrome 已登录时也可能没有旧式 Cookie 对，不能靠它决定是否等待渲染。
            # 登录入口持续出现才进入人工流程；明确账号标识恢复后立即继续。
            auth = await self.s.login.settled(min(self.s.cfg.jd["login_check_timeout_seconds"],
                self.s.cfg.jd["action_timeout_ms"] / 1000), require_stable_account=False)
            if auth.state == "pending":
                raise Retryable("登录组件仍在加载，保留现有会话等待，尚未确认退出登录")
        if auth.state == "verification":
            raise VerificationRequired(auth.reason)
        if auth.state == "logged_out":
            raise LoginRequired(auth.reason)

    async def goto(self, url):
        await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await self.guard()

    async def background_activation(self, node):
        """后台页不等渲染帧稳定；仍核对可见、可用及真正命中的控件。"""
        if not getattr(self.s, 'concurrent_ui', False) and not await self.page.evaluate(
                "() => document.visibilityState !== 'visible' || !document.hasFocus()"):
            return False
        self.s.scheduler.check_stop()
        if not await node.is_visible() or not await node.is_enabled():
            raise Retryable('后台页面控件暂不可操作')
        hit = await node.evaluate("""el => {
            el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
            const r=el.getBoundingClientRect(), style=getComputedStyle(el);
            if (!r.width || !r.height || style.visibility==='hidden' || style.display==='none' ||
                style.pointerEvents==='none' || el.getAttribute('aria-disabled')==='true') return false;
            const target=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            return target===el || !!target && el.contains(target);
        }""")
        if hit and hasattr(self.page, 'parent_hit'):
            hit = await self.page.parent_hit(node)
        if not hit:
            await self.check_auth()
            raise Retryable('后台页面控件被遮挡，保留页面等待，不强制穿过弹层')
        return True

    def record_activation(self, trigger):
        if trigger and self.s.target_ns is not None:
            actual, error = self.s.scheduler.record_trigger(self.s.task.id, self.s.target_ns)
            self.s.store.event(self.s.task.id, 'trigger', {'target_ns': self.s.target_ns,
                'actual_ns': actual, 'error_ms': error})

    async def activate(self, node, timeout=None, trigger=False):
        timeout = timeout if timeout is not None else self.s.cfg.jd['action_timeout_ms']
        if await self.background_activation(node):
            tag = await node.evaluate('el => el.tagName')
            self.record_activation(trigger)
            if tag in ('A', 'BUTTON'):
                await node.press('Enter', timeout=timeout)
            else:
                # DIV 入口没有浏览器内置键盘行为；命中检查通过后仍发送原生鼠标点击。
                await node.click(force=True, timeout=timeout)
        else:
            self.record_activation(trigger)
            await node.click(timeout=timeout)

    async def set_checkbox(self, node, expected, timeout=None):
        timeout = timeout if timeout is not None else self.s.cfg.jd['action_timeout_ms']
        if await node.is_checked(timeout=timeout) == expected:
            return
        if await self.background_activation(node):
            await node.press('Space', timeout=timeout)
            if await node.is_checked(timeout=timeout) != expected:
                raise Retryable('后台复选框尚未按配置生效，保留当前页面核对')
        else:
            await node.set_checked(expected, timeout=timeout)

    async def click_transition(self, button, trigger=False, *, wait_for_load=True):
        new_pages = []
        origin = self.page
        def on_popup(page):
            new_pages.append(page)
        origin.on("popup", on_popup)
        try:
            opens_popup = await button.get_attribute("target") == "_blank" or "window.open" in (await button.get_attribute("onclick") or "")
            if opens_popup:
                async with origin.expect_popup(timeout=self.s.cfg.jd["action_timeout_ms"]) as popup:
                    await self.activate(button, trigger=trigger)
                self.s.page = await popup.value
            else:
                await self.activate(button, trigger=trigger)
            # click 会等待普通导航，popup 事件在新页初始化时更新目标。
            if new_pages:
                self.s.page = new_pages[-1]
                if not wait_for_load:
                    return
                await self.page.wait_for_url(lambda url: str(url) not in ("", "about:blank"),
                    wait_until="domcontentloaded", timeout=15000)
            if wait_for_load:
                await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        finally:
            origin.remove_listener("popup", on_popup)

    async def snapshot(self, reason):
        if not self.s.cfg.logging["save_screenshots"]:
            return
        folder = self.s.cfg.data_dir / "screenshots" / self.s.task.id
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S-%f")
        location = self.page_location()
        LOG.info("[%s] 调试快照 %s：页面=%s", self.s.task.id, reason, location)
        try:
            (folder / f"{stamp}-{reason}.json").write_text(json.dumps({"page": location,
                "reason": reason, "cart_checkout_started": getattr(self.s, "cart_checkout_started", False),
                "cart_checkout_reached": getattr(self.s, "cart_checkout_reached", False)}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            LOG.warning("[%s] 保存调试页面信息失败", self.s.task.id)
        try:
            await self.page.screenshot(path=str(folder / f"{stamp}-{reason}.png"), full_page=True, timeout=10000,
                mask=[self.page.locator(".payment-summary-password__input, input[type=password], input[inputmode=numeric][maxlength='1']")])
        except Exception:
            LOG.warning("[%s] 保存截图失败", self.s.task.id)

    async def inspect(self):
        """只保存可更新定位所需的 DOM 摘要，不导出 Cookie 或整页源码。"""
        folder = self.s.cfg.data_dir / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        import json
        data = await self.page.evaluate("""() => [...(document.body?.querySelectorAll('button,a,input,[data-sku],.sku-name,.sku-title-name,.specification-group,.specification-series-layout,.bottom-btns-root > div') || [])]
            .filter(e => e.getClientRects().length).slice(0, 300).map(e => ({
                tag:e.tagName, id:e.id, class:e.className, text:(e.innerText||'').trim().slice(0,100),
                type:e.getAttribute('type'), sku:e.getAttribute('data-sku')}))""")
        (folder / f"{self.s.task.id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        await self.snapshot("inspect")
