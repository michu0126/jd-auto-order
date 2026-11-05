import asyncio
import logging
from urllib.parse import urlsplit

from jd.cart import Cart
from jd.checkout import Checkout
from jd.common import PageTools, SELECTORS
from jd.login import Login
from jd.order import Order
from jd.product import Product
from core.errors import StopOrder
from core.payment_secret import PaymentSecret
from core.browser_settings import resolve_endpoint

LOG = logging.getLogger(__name__)


class BrowserConnection:
    """区分脚本拥有的浏览器与借用的普通 Chrome；借用时绝不关闭上下文。"""
    def __init__(self, context, browser=None):
        self.context, self.browser = context, browser
        self.attached = browser is not None
        self.claimed_pages = set()

    def __getattr__(self, name):
        return getattr(self.context, name)

    async def acquire_page(self, reuse_cart=False):
        if self.attached and reuse_cart:
            candidates = [page for page in self.context.pages if not page.is_closed()
                          and page not in self.claimed_pages and urlsplit(page.url).hostname == "cart.jd.com"]
            if len(candidates) > 1:
                raise StopOrder("当前浏览器有多个购物车标签页，请只保留希望用于任务的一个购物车标签")
            if candidates:
                self.claimed_pages.add(candidates[0])
                LOG.info("复用当前 Chrome 的现有购物车标签页")
                return candidates[0]
        page = await self.context.new_page()
        self.claimed_pages.add(page)
        return page

    def release_page(self, page):
        self.claimed_pages.discard(page)

    async def close(self):
        if self.attached:
            # connect_over_cdp 返回的 Browser.close 仅断开传输，不发送远端 Browser.close。
            await self.browser.close()
        else:
            await self.context.close()


async def launch(playwright, cfg):
    if cfg.jd["browser_connection"] == "existing":
        endpoint = resolve_endpoint(cfg.jd)
        try:
            browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=cfg.jd["cdp_timeout_seconds"] * 1000)
        except Exception:
            raise StopOrder("未连接到现有 Chrome。请确认 Chrome 的远程调试已开启，并在 Chrome 弹出的连接请求中点击允许；不会另开独立浏览器") from None
        if not browser.contexts:
            await browser.close()
            raise StopOrder("现有 Chrome 没有可用的普通浏览上下文，请先打开管理页和京东购物车")
        candidates = [ctx for ctx in browser.contexts if any(
            urlsplit(page.url).hostname in ("127.0.0.1", "localhost", "::1")
            and urlsplit(page.url).port == cfg.jd["management_port"] and urlsplit(page.url).path == "/"
            for page in ctx.pages if not page.is_closed())]
        if len(candidates) == 1:
            context = candidates[0]
        elif not candidates and len(browser.contexts) == 1:
            context = browser.contexts[0]
        else:
            await browser.close()
            raise StopOrder("无法唯一确定管理页面所在的 Chrome 用户资料，请在希望接管的资料中打开管理页面后重试")
        context.set_default_timeout(cfg.jd["action_timeout_ms"])
        LOG.info("已连接现有 Chrome，复用登录会话；任务结束仅断开连接")
        return BrowserConnection(context, browser)
    kwargs = {} if cfg.jd["browser"] == "chromium" else {"channel": cfg.jd["browser"]}
    context = await playwright.chromium.launch_persistent_context(str(cfg.session_dir),
        headless=cfg.jd["headless"], locale="zh-CN", timezone_id="Asia/Shanghai",
        viewport={"width": 1365, "height": 900}, **kwargs)
    context.set_default_timeout(cfg.jd["action_timeout_ms"])
    if cfg.jd["block_media"]:
        async def route_handler(route):
            if route.request.resource_type == "media":
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", route_handler)
    return BrowserConnection(context)


class Session:
    def __init__(self, context, page, cfg, task, store, scheduler):
        self.context, self.page, self.cfg, self.task = context, page, cfg, task
        self.store, self.scheduler = store, scheduler
        unknown = cfg.selectors.keys() - SELECTORS.keys()
        if unknown:
            raise StopOrder(f"未知选择器名称：{sorted(unknown)}")
        self.selectors = SELECTORS | cfg.selectors
        self.base_selectors = self.selectors.copy()
        self.key = task.key(cfg.session_dir)
        self.submitted = False
        self.target_ns = None
        self.baseline_ids = None
        self.cart_selection = None
        self.cart_selected_ns = None
        self.product_selection = None
        self.product_prepared = False
        self.submission_guard = None
        self.concurrent_ui = False
        self.parallel_detail_count = 1
        self.cart_pending = False
        self.cart_preloaded = False
        self.cart_timings = {}
        self.cart_checkout_started = False
        self.cart_checkout_reached = False
        self.cart_checkout_page = None
        self.cart_checkout_reached_ns = None
        self.payment_secret = PaymentSecret()
        self.payment_secret_attempted = False
        self.auth_lock = asyncio.Lock()
        self.tools = PageTools(self)
        self.login = Login(self)
        self.product = Product(self)
        self.cart = Cart(self)
        self.checkout = Checkout(self)
        self.order = Order(self)
        from jd.payment import AutoPayment
        self.auto_payment = AutoPayment(self)
