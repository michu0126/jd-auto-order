import asyncio
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError

from core.errors import Retryable, StopOrder
from jd.common import PageTools, SKU_IDS_JS
from jd.submit_button import checkout_url

LOG = logging.getLogger(__name__)


class Cart(PageTools):
    def __init__(self, session):
        super().__init__(session)
        self._transition_origin = None
        self._transition_before = set()
        self._transition_pages = []
        self._transition_listener = None

    def stop_transition_watch(self):
        if self._transition_listener is not None:
            self._transition_origin.remove_listener("popup", self._transition_listener)
            self._transition_listener = None

    def start_transition_watch(self):
        self.stop_transition_watch()
        self._transition_origin = self.page
        self._transition_before = set(self.s.context.pages)
        self._transition_pages = []
        self._transition_listener = lambda page: self._transition_pages.append(page)
        self._transition_origin.on("popup", self._transition_listener)

    async def transition_pages(self):
        # 只跟随本次点击产生且由本任务购物车打开的新窗口，不接管其他任务或手工页。
        for page in self.s.context.pages:
            if page not in self._transition_before and page not in self._transition_pages:
                if await page.opener() is self._transition_origin:
                    self._transition_pages.append(page)
        return [page for page in dict.fromkeys([self.page, *self._transition_pages]) if not page.is_closed()]

    async def wait_checkout(self):
        # 允许购物车脚本稍后导航/打开新窗口；正常导航一旦完成就立即返回。
        deadline = time.monotonic() + max(2, self.s.cfg.jd["action_timeout_ms"] / 1000)
        while True:
            self.s.scheduler.check_stop()
            pages = await self.transition_pages()
            ready = [page for page in pages if checkout_url(page.url)]
            if len(ready) > 1:
                raise StopOrder("本次购物车点击打开多个结算窗口，停止操作，避免串单")
            if ready:
                self.s.page = ready[0]
                self.s.cart_checkout_reached = True
                self.stop_transition_watch()
                self.s.store.event(self.s.task.id, "checkout_page", {"page": self.page_location(),
                    "new_window": self.page is not self._transition_origin}, update_state=False)
                LOG.info("[%s] 已确认订单结算页：%s", self.s.task.id, self.page_location())
                return
            opened = [page for page in pages if page is not self._transition_origin]
            if len(opened) == 1:
                self.s.page = opened[0]
            if not self.page.is_closed() and self.page.url not in ("", "about:blank"):
                await self.guard()
            if time.monotonic() >= deadline:
                if not self.page.is_closed() and urlparse(self.page.url).hostname == "cart.jd.com" and not opened:
                    self.s.cart_checkout_started = False
                    self.stop_transition_watch()
                    raise Retryable(f"点击去结算后仍停留购物车，保留勾选并重新核对后重试；页面={self.page_location()}")
                raise Retryable(f"本次结算窗口仍在加载，保留页面等待，不重复点击；页面={self.page_location()}")
            await asyncio.sleep(.05)

    @property
    def expected_selection(self):
        return {"sku": self.s.task.sku, "quantity": self.s.task.quantity}

    @property
    def action_timeout(self):
        return min(self.s.cfg.jd["action_timeout_ms"], self.s.cfg.timing["cart_action_timeout_ms"])

    async def guard(self):
        async with self.step("body_ready"):
            await self.wait_body()
        async with self.step("auth_ready"):
            await self.check_auth()

    @asynccontextmanager
    async def step(self, name):
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed = (time.perf_counter_ns() - started) / 1e6
            self.s.cart_timings[name] = round(self.s.cart_timings.get(name, 0) + elapsed, 3)
            if elapsed >= 250:
                LOG.info("[%s] 购物车步骤 %s 耗时 %.1f ms", self.s.task.id, name, elapsed)

    async def navigate(self):
        relative = ((self.s.scheduler.now_ns() - self.s.target_ns) / 1e6
                    if self.s.target_ns is not None else None)
        LOG.info("[%s] 购物车刷新开始，距开售毫秒=%s", self.s.task.id,
                 f"{relative:+.1f}" if relative is not None else "未设定")
        async with self.step("navigation"):
            # 响应开始后检查真实正文/商品行，无需额外等待整页 DOMContentLoaded。
            await self.page.goto("https://cart.jd.com/cart_index", wait_until="commit", timeout=20000)
        async with self.step("page_ready"):
            await self.guard()

    async def open(self):
        await self.preload(refresh=True)

    async def preload(self, refresh=True):
        """只加载购物车，不要求目标已开售，也不改变勾选和数量。"""
        self.s.cart_selection = None
        self.s.cart_selected_ns = None
        self.s.cart_pending = False
        self.s.cart_preloaded = False
        self.s.cart_timings = {}
        if refresh or "cart.jd.com" not in self.page.url:
            await self.navigate()
        else:
            await self.guard()
        async with self.step("rows_ready"):
            await self.ready_snapshot()
        self.s.cart_preloaded = True

    async def ready_snapshot(self):
        """等待可见商品行，并直接返回本次批量读取，省去重复的行定位。"""
        deadline = time.monotonic() + self.s.cfg.jd["action_timeout_ms"] / 1000
        while True:
            self.s.scheduler.check_stop()
            state = await self.snapshot()
            if state:
                return state
            # 商品行未出现时仍检查登录和人工验证，不能刷新掉验证页面。
            await self.guard()
            if time.monotonic() >= deadline:
                raise Retryable("未找到 cart_row，可能尚未开售或需要更新选择器")
            await asyncio.sleep(.05)

    async def snapshot(self):
        # One DOM read for every visible row: avoid awaiting dozens of per-row calls after checking.
        return await self.page.locator(self.s.selectors["cart_row"]).evaluate_all(
            """(rows, selectors) => { const skuIds = """ + SKU_IDS_JS + """;
            const visible = el => !!el && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            return rows.map((row,index) => {
                if (!visible(row)) return null;
                const cb = row.querySelector(selectors.checkbox);
                const quantity = [...row.querySelectorAll(selectors.quantity)].find(visible);
                const unavailableText = [...new Set(row.innerText.match(/等待抢购|待发布|尚未开售|暂未开售|已售罄|暂时缺货|商品补货中|补货中|已抢光|已售完|无货(?!订购)/g) || [])].join('、');
                return {index, ids:skuIds(row), checkbox:!!cb,
                    checked:!!cb?.checked, disabled:!!cb && (cb.matches(':disabled') || cb.getAttribute('aria-disabled')==='true'),
                    unavailable:!!unavailableText, unavailable_text:unavailableText,
                    quantity:quantity?.value ?? null};
            }).filter(Boolean); }""", {"checkbox": self.s.selectors["cart_checkbox"], "quantity": self.s.selectors["cart_quantity"]})

    def target_row(self, state, task=None):
        task = task if task is not None else self.s.task
        targets = [row for row in state if task.sku in row["ids"]]
        if not targets:
            self.s.cart_pending = False
            raise Retryable("购物车未找到目标 SKU，需要先加入购物车")
        if len(targets) != 1:
            raise StopOrder("购物车有多个相同 SKU 的商品行，请整理后运行")
        target = targets[0]
        if target["ids"] != [task.sku]:
            raise StopOrder("目标商品行包含冲突的 SKU 信息，停止结算")
        if not target["checkbox"] or target["disabled"] or target.get("unavailable", False):
            self.s.cart_pending = False
            reasons = []
            if not target["checkbox"]:
                reasons.append("未找到勾选控件")
            if target["disabled"]:
                reasons.append("勾选控件禁用")
            if target.get("unavailable_text"):
                reasons.append("商品状态=" + target["unavailable_text"])
            elif target.get("unavailable"):
                reasons.append("商品标记为不可购买")
            raise Retryable("目标商品暂不可勾选，可能尚未开售或无货（" + "；".join(reasons) + "）；刷新购物车后重试")
        return target

    def pending(self, reason):
        self.s.cart_selection = None
        self.s.cart_pending = True
        LOG.info("[%s] %s；保留当前购物车页面，不立即刷新", self.s.task.id, reason)
        return Retryable(reason)

    async def verify(self):
        state = await self.snapshot()
        target = self.target_row(state)
        selected = [row["ids"] for row in state if row["checked"]]
        if not selected:
            raise self.pending("购物车更新后勾选为空，等待当前页面同步后重新勾选")
        if selected != [[self.s.task.sku]]:
            self.s.cart_selection = None
            LOG.error("[%s] 购物车勾选核对失败：目标 SKU=%s，实际已选 SKU=%s", self.s.task.id, self.s.task.sku, selected)
            raise StopOrder("购物车最终勾选项不等于目标商品，停止结算")
        if target["quantity"] != str(self.s.task.quantity):
            raise self.pending("购物车更新后数量与任务不符，保持当前页面重新核对")
        self.s.cart_pending = False
        self.s.cart_selection = {"sku": self.s.task.sku, "quantity": self.s.task.quantity}

    async def set_checked(self, state, expected):
        timeout = self.action_timeout
        rows = self.page.locator(self.s.selectors["cart_row"])
        row = rows.nth(state["index"])
        if await row.evaluate(SKU_IDS_JS, timeout=timeout) != state["ids"]:
            raise self.pending("购物车行顺序更新，重新读取后再操作")
        checkbox = row.locator(self.s.selectors["cart_checkbox"]).first
        if not await checkbox.is_enabled(timeout=timeout):
            self.s.cart_pending = False
            raise Retryable("购物车商品暂时无法改变勾选，稍后刷新重试")
        try:
            if await checkbox.is_checked(timeout=timeout) != expected:
                if await checkbox.is_visible():
                    await self.set_checkbox(checkbox, expected, timeout=timeout)
                else:
                    await self.activate(await self.find("cart_checkbox_click", scope=row, timeout=timeout), timeout=timeout)
        except PlaywrightError as exc:
            if self.page.is_closed():
                raise
            raise self.pending("购物车勾选控件正在更新，保留页面稍后核对") from exc

    async def select(self, refresh=True):
        self.s.cart_timings = {}
        self.s.cart_selection = None
        self.s.cart_selected_ns = None
        preserve = self.s.cart_pending and "cart.jd.com" in self.page.url
        self.s.cart_pending = False
        if "cart.jd.com" not in self.page.url or (refresh and not preserve):
            await self.navigate()
        else:
            await self.guard()
        async with self.step("rows_ready"):
            state = await self.ready_snapshot()
        target = self.target_row(state)
        # 已开始选择同步后，即使阶段计时取消等待，也应在当前页核对。
        self.s.cart_pending = True
        # Cancel other items first. Selecting the target early can race their server updates.
        for row in state:
            if row["index"] != target["index"] and row["checked"]:
                async with self.step("uncheck_other"):
                    await self.set_checked(row, False)
        state = await self.snapshot()
        target = self.target_row(state)
        row = self.page.locator(self.s.selectors["cart_row"]).nth(target["index"])
        async with self.step("quantity"):
            quantity = await self.find("cart_quantity", scope=row, timeout=self.action_timeout)
            if await quantity.input_value(timeout=self.action_timeout) != str(self.s.task.quantity):
                await quantity.fill(str(self.s.task.quantity), timeout=self.action_timeout)
                await quantity.press("Tab", timeout=self.action_timeout)
        state = await self.snapshot()
        target = self.target_row(state)
        if any(item["checked"] and item["index"] != target["index"] for item in state):
            raise StopOrder("购物车最终勾选项不等于目标商品，停止结算")
        async with self.step("target_checkbox"):
            await self.set_checked(target, True)
        async with self.step("verify"):
            await self.verify()
        self.s.cart_selected_ns = self.s.scheduler.now_ns()

    async def checkout(self, refresh=True):
        if not self.s.cart_checkout_started and self._transition_origin is not None:
            pages = await self.transition_pages()
            if any(checkout_url(page.url) or page is not self._transition_origin for page in pages):
                # 上轮等待结束到重试之间可能刚出现原来的结算窗口，先跟随，不能再点一次。
                self.s.cart_checkout_started = True
        if self.s.cart_checkout_started:
            if self.page.url.startswith("chrome-error:"):
                self.s.cart_checkout_started = False
                self.s.cart_checkout_reached = False
                self.stop_transition_watch()
                refresh = True
            elif self.s.cart_checkout_reached and urlparse(self.page.url).hostname == "cart.jd.com":
                # 网站已退回购物车；之前的点击标志不代表仍在结算，必须重新核对。
                self.s.cart_checkout_started = False
                self.s.cart_checkout_reached = False
                self.stop_transition_watch()
                refresh = False
                LOG.info("[%s] 结算页已返回购物车，保留当前页面重新核对后发起结算", self.s.task.id)
            else:
                LOG.info("[%s] 已发起购物车结算，等待实际结算窗口，不刷新购物车；页面=%s",
                    self.s.task.id, self.page_location())
                await self.wait_checkout()
                return
        expected = self.expected_selection
        if self.s.cart_selection == expected and "cart.jd.com" in self.page.url:
            try:
                await self.verify()
            except Retryable:
                await self.select(refresh=False)
        else:
            await self.select(refresh=refresh)
        button = await self.find("checkout")
        if self.s.cart_selected_ns is not None:
            elapsed = (self.s.scheduler.now_ns() - self.s.cart_selected_ns) / 1e6
            LOG.info("[%s] 勾选成功至发起结算耗时 %.1f ms", self.s.task.id, elapsed)
            self.s.store.event(self.s.task.id, "checkout_start", {"since_selection_ms": round(elapsed, 3)},
                update_state=False)
        self.start_transition_watch()
        self.s.cart_checkout_started = True
        self.s.cart_checkout_reached = False
        try:
            await self.click_transition(button, trigger=True)
            await self.wait_checkout()
        except PlaywrightError as exc:
            if "net::ERR_" in str(exc):
                self.s.cart_checkout_started = False
                self.stop_transition_watch()
            raise
