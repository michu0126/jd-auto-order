"""可测试的附加平台浏览器执行器。真实站点需先绑定实际页面控件。"""
import asyncio
import logging
import re
import time
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError

from core.config import money
from core.errors import Cancelled, LoginRequired, Retryable, StopOrder, VerificationRequired
from core.platforms import LABELS, HOMES, allowed_url, readiness
from core.retry import RetryBudget
from utils.notifier import notify

LOG = logging.getLogger(__name__)
VERIFY = re.compile(r"安全验证|滑块验证|完成验证|操作过于频繁|访问受限")
SOLD_OUT = re.compile(r"已抢光|已售罄|商品无货|库存不足|暂不可购买")


class WebPurchase:
    def __init__(self, context, page, cfg, task, store, scheduler):
        self.context, self.page, self.cfg, self.task = context, page, cfg, task
        self.store, self.scheduler = store, scheduler
        self.profile = cfg.platforms.get(task.platform, {})
        self.bindings = self.profile.get("selectors", {})
        self.key = task.key(cfg.session_dir)
        self.submitted = False
        self.auth_lock = asyncio.Lock()
        self.target_ns = round(task.target.timestamp() * 1e9)

    async def node(self, key, scope=None):
        if key not in self.bindings:
            raise StopOrder(f"{LABELS[self.task.platform]}缺少实际页面定位：{key}，请先完成页面适配")
        nodes = (scope if scope is not None else self.page).locator(self.bindings[key])
        await nodes.first.wait_for(state="visible", timeout=self.cfg.jd["action_timeout_ms"])
        visible = [nodes.nth(i) for i in range(await nodes.count()) if await nodes.nth(i).is_visible()]
        if len(visible) != 1:
            raise StopOrder(f"页面定位 {key} 不能唯一确认，停止操作")
        return visible[0]

    async def guard(self):
        self.scheduler.check_stop()
        if not allowed_url(self.task.platform, self.page.url):
            raise StopOrder("页面已离开所属平台，停止自动操作")
        text = await self.page.locator("body").inner_text(timeout=self.cfg.jd["action_timeout_ms"])
        if VERIFY.search(text):
            raise VerificationRequired("平台要求人工验证，请在浏览器完成")
        return text

    async def login(self):
        if "auth_user" not in self.bindings:
            raise StopOrder("尚未绑定真实页面的登录状态，不能自动判断登录成功")
        await self.page.goto(self.profile.get("login_url", HOMES[self.task.platform]), wait_until="domcontentloaded")
        try:
            await self.guard()
            await self.node("auth_user")
        except (PlaywrightError, VerificationRequired):
            await self.manual("请完成正常登录或人工验证")

    async def manual(self, reason):
        self.store.event(self.task.id, "manual", reason)
        await notify(self.cfg.notification["desktop"], LABELS[self.task.platform] + "需要人工操作", reason)
        await self.page.bring_to_front()
        deadline = time.monotonic() + self.cfg.jd["manual_timeout_seconds"]
        while time.monotonic() < deadline:
            self.scheduler.check_stop()
            try:
                await self.guard()
                await self.node("auth_user")
                return
            except (PlaywrightError, VerificationRequired):
                await asyncio.sleep(.5)
        raise StopOrder("等待人工验证超时")

    async def inspect(self):
        await self.page.goto(self.task.url, wait_until="domcontentloaded")
        await self.guard()
        if self.task.platform == "taobao" and urlparse(self.page.url).hostname == "buy.taobao.com":
            from marketplaces.taobao_checkout import inspect_taobao_checkout
            report = await inspect_taobao_checkout(self.page)
            self.store.event(self.task.id, "prepared", report)
            return report
        if self.task.platform == "taobao" and urlparse(self.page.url).hostname == "detail.tmall.com":
            from marketplaces.taobao_product import inspect_tmall_product
            report = await inspect_tmall_product(self.page)
            if not self.task.sku.startswith("link-") and report["product_id"] != self.task.sku:
                raise StopOrder("实际天猫商品 ID 与任务不一致")
            self.store.event(self.task.id, "prepared", report)
            await self.snapshot("product-inspect")
            return report
        # 只保存可见控件摘要，不读取 Cookie、令牌、密码或页面网络数据。
        controls = await self.page.locator("button, a, input, [role=button]").evaluate_all("""nodes => nodes
            .filter(el => el.getClientRects().length && el.type !== 'password')
            .slice(0, 100).map(el => ({tag:el.tagName, role:el.getAttribute('role'),
                text:(el.innerText || el.getAttribute('aria-label') || '').slice(0,80)}))""")
        self.store.event(self.task.id, "prepared", {"platform": self.task.platform,
            "controls": controls, "automatic_checkout_ready": readiness(self.cfg, self.task.platform)["ready"]})
        await self.snapshot("inspect")

    async def snapshot(self, reason):
        if not self.cfg.logging["save_screenshots"] or self.page.is_closed():
            return
        from datetime import datetime
        folder = self.cfg.data_dir / "screenshots" / self.task.id
        folder.mkdir(parents=True, exist_ok=True)
        try:
            await self.page.screenshot(path=str(folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + reason + ".png")),
                mask=[self.page.locator("input[type=password]"), self.page.locator("input[autocomplete*=password]")])
        except PlaywrightError:
            LOG.warning("[%s] 页面截图未完成", self.task.id)

    async def prepare(self):
        await self.page.goto(self.task.url, wait_until="domcontentloaded", timeout=20000)
        text = await self.guard()
        if self.task.platform == "taobao" and urlparse(self.page.url).hostname == "detail.tmall.com":
            from marketplaces.taobao_product import prepare_tmall_product
            if SOLD_OUT.search(text):
                raise Retryable("商品暂不可购买")
            report = await prepare_tmall_product(self.page, self.task)
            self.store.event(self.task.id, "prepared", report)
            return report
        await self.node("auth_user")
        title = re.sub(r"\s+", "", await (await self.node("product_name")).inner_text())
        if re.sub(r"\s+", "", self.task.name) != title:
            raise StopOrder("商品名称与任务不一致，停止操作")
        if SOLD_OUT.search(text):
            raise Retryable("商品暂不可购买")
        for group, value in self.task.sku_options.items():
            option = await self.node("spec_" + group)
            if re.sub(r"\s+", "", await option.inner_text()) != re.sub(r"\s+", "", value):
                raise StopOrder("规格控件原文与配置不一致")
            await option.click()
            chosen = await self.node("selected_" + group)
            if re.sub(r"\s+", "", await chosen.inner_text()) != re.sub(r"\s+", "", value):
                raise StopOrder("所选规格未按任务生效")
        if "product_quantity" in self.bindings:
            quantity = await self.node("product_quantity")
            await quantity.fill(str(self.task.quantity))
            await quantity.press("Tab")

    def in_paths(self, key):
        return allowed_url(self.task.platform, self.page.url) and any(
            urlparse(self.page.url).path.startswith(path) for path in self.profile.get(key, []))

    async def validate(self):
        await self.guard()
        if not self.in_paths("checkout_paths"):
            raise StopOrder("当前不是已绑定的订单结算页面")
        native_report = None
        if self.task.platform == "taobao" and urlparse(self.page.url).hostname == "buy.taobao.com":
            from marketplaces.taobao_checkout import inspect_taobao_checkout, verify_product_evidence
            native_report = await inspect_taobao_checkout(self.page)
            verify_product_evidence(native_report, self.task)
            self.store.event(self.task.id, "prepared", native_report)
            if not native_report["amount_verified"]:
                raise StopOrder("淘宝最终合计尚未独立核对，停止继续操作")
        root = await self.node("checkout_root")
        items = root.locator(self.bindings["checkout_item"])
        if await items.count() != 1 or not await items.is_visible():
            raise StopOrder("结算页必须仅包含一个目标商品")
        identifier = await self.node("checkout_item_id", scope=items)
        attribute = self.profile.get("item_id_attribute", "")
        actual_id = await identifier.get_attribute(attribute) if attribute else await identifier.inner_text()
        if self.task.sku.startswith("link-") or (actual_id or "").strip() != self.task.sku:
            raise StopOrder("无法以真实商品 ID 核对结算商品；短链占位不作为核对证据")
        if self.task.variant_id:
            variant = await self.node("checkout_variant_id", scope=items)
            attribute = self.profile.get("variant_id_attribute", "")
            actual_variant = await variant.get_attribute(attribute) if attribute else await variant.inner_text()
            if (actual_variant or "").strip() != self.task.variant_id:
                raise StopOrder("结算所选 SKU ID 与任务不一致")
        quantity = await self.node("checkout_quantity", scope=items)
        value = await quantity.input_value() if await quantity.evaluate("el => el.matches('input')") else await quantity.inner_text()
        match = re.fullmatch(r"\s*[xX×]?\s*(\d+)\s*(?:件)?\s*", value)
        if not match or int(match[1]) != self.task.quantity:
            raise StopOrder("结算数量与任务不一致")
        for group, expected in self.task.sku_options.items():
            spec = await self.node("checkout_spec_" + group, scope=items)
            if re.sub(r"\s+", "", await spec.inner_text()) != re.sub(r"\s+", "", expected):
                raise StopOrder("结算规格与任务不一致")
        address = await (await self.node("address", scope=root)).inner_text()
        if not address.strip() or self.task.address_keyword not in address:
            raise StopOrder("无法核对默认收货地址")
        total = money(await (await self.node("total", scope=root)).inner_text(), allow_zero=True)
        if native_report and total != money(native_report["final_payable_amount"], allow_zero=True):
            raise StopOrder("淘宝配置金额定位与实际最终合计不一致")
        if native_report:
            payment = native_report["payment"]
            if not payment["verified"]:
                raise StopOrder("淘宝当前支付方式尚未唯一核对")
            if payment["selected_method"] != "alipay":
                raise StopOrder("淘宝当前选择分期或代付，不能当作普通在线支付继续")
        if self.task.max_price is not None and total > self.task.max_price:
            raise StopOrder("订单最终应付超过配置限额")
        if await root.locator("input[type=password]:visible").count():
            raise StopOrder("附加平台不接收京东支付密码，请人工完成平台要求")
        return total

    async def submit(self):
        if self.store.get(self.key):
            raise StopOrder("本任务已存在提交记录，请先核实订单")
        total = await self.validate()
        button = await self.node("submit")
        text = re.sub(r"\s+", "", await button.inner_text())
        if text not in ("提交订单", "立即下单", "确认下单") or not await button.is_enabled():
            raise StopOrder("附加平台最终按钮须明确表示下单，不自动点击付款")
        self.scheduler.check_stop()
        self.store.claim(self.key, self.task.id, total)
        self.submitted = True
        try:
            await button.click()
            async with asyncio.timeout(self.cfg.jd["result_timeout_seconds"]):
                await self.node("order_status")
                await self.guard()
                if not self.in_paths("result_paths"):
                    raise StopOrder("提交后没有进入已绑定的结果页")
                status = await (await self.node("order_status")).inner_text()
                oid = (await (await self.node("order_id")).inner_text()).strip()
                if not re.fullmatch(r"\d{6,40}", oid) or not re.search(r"待付款|待支付|订单提交成功|下单成功", status):
                    raise StopOrder("未读取到唯一订单号与明确订单状态")
                self.store.finish(self.key, "confirmed", oid, "已确认订单，未操作付款")
                self.store.event(self.task.id, "success", {"platform": self.task.platform, "order_id": oid, "price": str(total)})
                await notify(self.cfg.notification["desktop"], LABELS[self.task.platform] + "下单成功", self.task.name + "，订单号 " + oid)
                return oid
        except BaseException:
            record = self.store.get(self.key)
            if record and record[0] != "confirmed":
                self.store.finish(self.key, "unknown", detail="提交后状态未明，不自动重发，请核实所属平台订单")
            raise

    async def run(self, purchase_lock, dry_run=False, now=False):
        status = readiness(self.cfg, self.task.platform)
        if not status["ready"]:
            raise StopOrder(status["detail"])
        if self.store.get(self.key):
            raise StopOrder("已有提交记录，不重复下单")
        target = self.scheduler.now_ns() if now or dry_run else self.target_ns
        if target < self.scheduler.now_ns() - 2_000_000_000:
            raise StopOrder("目标时间已过期")
        self.store.event(self.task.id, "waiting", {"platform": self.task.platform, "target": self.task.target.isoformat()})
        await self.scheduler.wait(target - int(self.cfg.timing["prewarm_seconds"] * 1e9))
        async with self.auth_lock:
            await self.login()
        await self.scheduler.wait(target - 30_000_000_000)
        try:
            await self.prepare()
        except (Retryable, PlaywrightError):
            pass
        await self.scheduler.wait(target)
        budget = RetryBudget(self.task.retry)
        if not (now or dry_run):
            budget.deadline = time.monotonic() + max(0, (target + int(self.task.retry["timeout_seconds"] * 1e9) - self.scheduler.now_ns()) / 1e9)
        while budget.available():
            self.scheduler.check_stop()
            budget.begin()
            try:
                async with purchase_lock:
                    if time.monotonic() >= budget.deadline:
                        raise StopOrder("等待结算锁时已超出任务窗口")
                    await self.prepare()
                    self.scheduler.record_trigger(self.task.id, target)
                    await (await self.node("buy_now")).click()
                    await self.node("checkout_root")
                    await self.validate()
                    if time.monotonic() >= budget.deadline:
                        raise StopOrder("已超过任务尝试窗口，停止最终提交")
                    if dry_run or not self.task.auto_submit:
                        self.store.event(self.task.id, "dry_run", "结算已核对，未提交")
                    else:
                        await self.submit()
                    return True
            except (LoginRequired, VerificationRequired) as exc:
                if self.submitted:
                    raise StopOrder("已提交后要求验证，请先核实订单") from exc
                await self.manual(str(exc))
            except (Retryable, PlaywrightError) as exc:
                if self.submitted or self.page.is_closed():
                    raise StopOrder("页面或提交状态需人工核实") from exc
                LOG.info("[%s] 第 %s 次未提交：%s", self.task.id, budget.count, type(exc).__name__)
                if budget.available():
                    await self.scheduler.wait(self.scheduler.now_ns() + int(budget.delay(network=isinstance(exc, PlaywrightError)) * 1e9))
        raise StopOrder("任务重试窗口或次数耗尽，未提交")
