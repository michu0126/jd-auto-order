import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from core.config import CST, money, start_time
from core.errors import StopOrder
from jd.common import PageTools, quantity_text, sku_ids
from jd.dom import main_body_text
from utils.logger import measure
from utils.notifier import notify
from jd.password_prompt import dismiss_password_dialog, PASSWORD_TEXT, jd_page

LOG = logging.getLogger(__name__)


def order_id(text):
    match = re.search(r"(?:订单号|订单编号)\s*[:：]?\s*(\d{6,30})(?!\d)", text)
    return match[1] if match else None


def success_result(text):
    found = set(re.findall(r"(?<!历史)(?:订单号|订单编号)\s*[:：]?\s*(\d{6,30})(?!\d)", text))
    if len(found) == 1 and re.search(r"订单提交成功|订单已提交|下单成功|收银台|支付剩余时间|请.{0,8}付款|待付款|待支付|等待付款", text):
        return next(iter(found))
    return None


class Order(PageTools):
    def success(self, text):
        return success_result(text)

    async def baseline(self):
        original = self.s.page
        page = await self.s.context.new_page()
        try:
            self.s.page = page
            await self.goto("https://order.jd.com/center/list.action")
            await self.find("order_row", timeout=3000)
            rows = self.page.locator(self.s.selectors["order_row"])
            existing = set()
            for index in range(await rows.count()):
                oid = order_id(await rows.nth(index).inner_text())
                if oid:
                    existing.add(oid)
            self.s.baseline_ids = existing
        except Exception:
            LOG.warning("[%s] 无法读取提交前订单基线；若提交结果不明将只查询并保留未知状态", self.s.task.id)
        finally:
            self.s.page = original
            await page.close()

    async def reconcile(self, quote, submitted_at):
        """提交结果不明时只读查询；匹配不足就保留未知状态，绝不据此重发。"""
        original = self.s.page
        check = await self.s.context.new_page()
        try:
            self.s.page = check
            await self.goto("https://order.jd.com/center/list.action")
            await self.find("order_row", timeout=8000)
            rows = self.page.locator(self.s.selectors["order_row"])
            candidates = set()
            for index in range(await rows.count()):
                row = rows.nth(index)
                if not await row.is_visible() or await sku_ids(row) != [self.s.task.sku]:
                    continue
                text = await row.inner_text()
                oid = order_id(text)
                if (not oid or self.s.baseline_ids is None or oid in self.s.baseline_ids
                        or not re.search(r"等待付款|待付款|待支付", text)):
                    continue
                stamp_node = await self.find("order_time", scope=row, required=False)
                amount_node = await self.find("order_amount", scope=row, required=False)
                quantity_nodes = row.locator(self.s.selectors["order_quantity"])
                if stamp_node is None or amount_node is None or not await quantity_nodes.count():
                    continue
                stamp = await stamp_node.get_attribute("title") or await stamp_node.inner_text()
                try:
                    created = start_time(stamp.strip()).timestamp()
                    amount = money(await amount_node.inner_text(), allow_zero=True)
                    quantities = sum([quantity_text(await quantity_nodes.nth(i).inner_text()) for i in range(await quantity_nodes.count())])
                except (StopOrder, ValueError):
                    continue
                if submitted_at - 2 <= created <= submitted_at + 120 and amount == quote.total and quantities == self.s.task.quantity:
                    candidates.add(oid)
            return next(iter(candidates)) if len(candidates) == 1 else None
        except Exception as exc:
            LOG.warning("[%s] 只读查单未确认：%s", self.s.task.id, type(exc).__name__)
            return None
        finally:
            self.s.page = original
            await check.close()

    async def confirmed(self, oid, quote, pending=False):
        detail = "待付款订单已核实，未继续付款" if pending else "已读取成功页或通过订单中心严格匹配"
        self.s.store.finish(self.s.key, "confirmed", oid, detail)
        self.s.store.event(self.s.task.id, "success", {"order_id": oid, "price": str(quote.total),
            "payment_status": "pending" if pending else "未核实支付状态"})
        if pending:
            payment = "订单待付款，请手动付款"
        else:
            payment = "在线支付，等待手动付款"
        LOG.info("[%s] 已确认订单号=%s 应付=%s，%s", self.s.task.id, oid, quote.total, payment)
        await self.snapshot("success")
        await notify(self.s.cfg.notification["desktop"], "京东下单成功", f"{self.s.task.name}，应付 ¥{quote.total}，订单号 {oid}，{payment}")

    async def dismiss_post_submit_password(self, origin):
        if not self.s.submitted:
            return False
        closed = await dismiss_password_dialog(self.page, self.s.selectors, self.s.cfg.jd["action_timeout_ms"], activate=self.activate)
        if closed and self.page.is_closed() and not origin.is_closed():
            self.s.page = origin
        # 部分支付验证打开独立窗口；只关闭提交后新开的京东密码窗口。
        if not closed and self.page != origin and jd_page(self.page.url):
            text = await main_body_text(self.page)
            fields = self.page.locator("input[type=password]:visible, .payment-summary-password__input input:visible")
            if PASSWORD_TEXT.search(text) and await fields.count():
                await self.page.close()
                self.s.page = origin
                closed = True
        if closed:
            self.s.store.event(self.s.task.id, "post_submit_password_closed", "已关闭提交后的支付密码框，正在核实待付款订单")
            LOG.info("[%s] 已关闭提交后的密码验证，不输入支付密码；正在核实订单", self.s.task.id)
        return closed

    async def submit(self):
        if (urlparse(self.page.url).hostname == 'pc-settlement-lite-pro.pf.jd.com' and
                not getattr(self, 'payment_guard', None)):
            raise StopOrder('轻量结算必须先启用仅生成订单的付款拦截，停止最终点击')
        if self.s.store.get(self.s.key):
            raise StopOrder("同一任务已有提交记录，请先核实京东订单中心")
        guard = getattr(self.s, "submission_guard", None)
        if guard is not None:
            guard.check(self.s.task, self.s.target_ns)
        quote = await self.s.checkout.validate()
        if await self.s.checkout.fill_payment_password():
            quote = await self.s.checkout.validate()
        button = await self.find("submit")
        if not await button.is_enabled() or await button.get_attribute("aria-disabled") == "true":
            from core.errors import Retryable
            raise Retryable("提交订单按钮暂不可用")
        await self.s.checkout.verify_payment()
        await self.s.checkout.assert_submission_allowed(button, final=True)
        self.s.scheduler.check_stop()
        if (self.s.target_ns is not None and
                self.s.scheduler.now_ns() >= self.s.target_ns + int(self.s.task.retry['timeout_seconds'] * 1e9)):
            raise StopOrder('本任务购买窗口已截止，停止最终提交')
        persist = lambda: self.s.store.claim(self.s.key, self.s.task.id, quote.total)
        if guard is None:
            persist()
        else:
            guard.claim(self.s.task, self.s.target_ns, persist, amount=quote.total)
        self.s.submitted = True
        submitted_at = self.s.scheduler.now_ns() / 1e9
        error = None
        origin = self.page
        password_closed = False
        closed_at = None
        success_seen_at = None
        try:
            with measure(self.s.task.id, "提交点击与导航"):
                await self.click_transition(button)
            deadline = time.monotonic() + self.s.cfg.jd["result_timeout_seconds"]
            while time.monotonic() < deadline:
                self.s.scheduler.check_stop()
                if await self.s.checkout.password_error():
                    error = StopOrder("京东提示支付密码校验未通过，请核实订单状态")
                    break
                if not password_closed and await self.dismiss_post_submit_password(origin):
                    password_closed = True
                    closed_at = time.monotonic()
                    continue
                text = await main_body_text(self.page)
                found = self.success(text) if urlparse(self.page.url).hostname != "order.jd.com" else None
                if found:
                    if success_seen_at is None:
                        success_seen_at = time.monotonic()
                    if password_closed or time.monotonic() - success_seen_at >= .35:
                        await self.confirmed(found, quote, pending=bool(re.search(r"待付款|待支付|等待付款", text)))
                        return found
                else:
                    success_seen_at = None
                if closed_at is not None and time.monotonic() - closed_at >= .5:
                    break
                if re.search(r"提交失败|库存不足|商品无货|系统繁忙", text):
                    break
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            self.s.store.finish(self.s.key, "unknown", detail="任务在提交后被取消，必须核实订单中心")
            raise
        except Exception as exc:
            error = exc
        self.s.store.finish(self.s.key, "unknown", detail="提交后未读到成功页，正在只读查询订单中心")
        found = await self.reconcile(quote, submitted_at)
        if found:
            await self.confirmed(found, quote, pending=True)
            return found
        self.s.store.finish(self.s.key, "unknown", detail="未能确认订单；不会重发，请人工核实")
        await self.snapshot("unknown-order")
        raise StopOrder("已尝试提交，成功页和订单中心均未能确认；已停止重试，请核实京东订单中心") from error
