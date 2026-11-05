import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from core.config import CST, money, start_time
from core.errors import StopOrder, SubmissionRejected
from jd.common import PageTools, quantity_text, sku_ids
from jd.dom import main_body_text, result_body_text
from utils.logger import measure
from utils.notifier import notify
from jd.password_prompt import dismiss_password_dialog, PASSWORD_TEXT, jd_page
from jd.submission_prompt import dismiss_submission_prompt
from jd.submit_button import checkout_url

LOG = logging.getLogger(__name__)


def submission_feedback(text):
    # 只保存已知提示短语，不将收货地址、手机号或整页正文写入日志。
    for kind, pattern in (
        ("rate_limit", r"(?:您)?多次提交过快|提交过快|操作过于频繁|操作频繁|请求过于频繁|提交频繁"),
        ("stock", r"在所选择的地址下暂时无货|(?:商品|赠品)(?:或赠品)?(?:暂时|暂)?无货(?!订购)|库存不足|已被抢光|已抢光"),
        ("rejected", r"(?:订单)?提交失败|下单失败|系统繁忙|请勿重复提交"),
        ("verification", r"验证一下[，,]?\s*购物无忧|请完成安全验证"),
        ("password", r"支付密码(?:错误|不正确|已锁定)|密码输入错误|密码不正确|密码已锁定"),
    ):
        match = re.search(pattern, text)
        if match:
            result = {"kind": kind, "message": match[0]}
            delay = re.search(r"(\d{1,3})\s*秒后(?:重试|再试)", text)
            if kind == "rate_limit" and delay:
                result['retry_after_seconds'] = int(delay[1])
            return result
    return None


def order_id(text):
    match = re.search(r"(?:订单号|订单编号)\s*[:：]?\s*(\d{6,30})(?!\d)", text)
    return match[1] if match else None


def success_result(text):
    found = set(re.findall(r"(?<!历史)(?:订单号|订单编号)\s*[:：]?\s*(\d{6,30})(?!\d)", text))
    if len(found) == 1 and re.search(r"订单提交成功|订单已提交|下单成功|收银台|支付剩余时间|请.{0,8}付款|待付款|待支付|等待付款|支付成功|付款成功", text):
        return next(iter(found))
    return None


class Order(PageTools):
    def success(self, text):
        return success_result(text)

    async def capture_feedback(self, started_ns, feedback, found):
        if feedback:
            return
        feedback.update(found, after_submit_ms=round(
            (time.perf_counter_ns() - started_ns) / 1e6, 3), page=self.page_location())
        self.s.store.event(self.s.task.id, "submit_feedback", dict(feedback), update_state=False)
        LOG.warning("[%s] 提交阶段首次提示：%s；距提交尝试 %.1f ms；页面=%s",
            self.s.task.id, found["message"], feedback["after_submit_ms"], feedback["page"])
        if found['kind'] in ('rate_limit', 'stock', 'rejected'):
            try:
                feedback['closed'] = await dismiss_submission_prompt(self.page, found['message'], self.activate,
                    timeout=min(700, self.s.cfg.jd['action_timeout_ms']))
            except Exception:
                feedback['closed'] = False
            if feedback['closed']:
                self.s.store.event(self.s.task.id, 'submission_prompt_closed',
                    {'kind': found['kind'], 'message': found['message']}, update_state=False)
                LOG.info('[%s] 已关闭已识别提示：%s，未刷新页面', self.s.task.id, found['message'])
                return
        # 截图有硬截止；文本已先落盘，不让截图失败拖延后续状态判断。
        try:
            async with asyncio.timeout(1.5):
                await self.snapshot("submit-feedback")
        except TimeoutError:
            LOG.warning("[%s] 首次提交提示截图超时，文字和观察时间已保存", self.s.task.id)

    async def watch_feedback(self, started_ns, feedback):
        """提交动作等待期间也读取当前页；首次失败提示先留证据，再查单。"""
        while True:
            try:
                found = submission_feedback(await main_body_text(self.page))
                if found:
                    await self.capture_feedback(started_ns, feedback, found)
                    return
            except Exception as exc:
                # 导航会销毁旧文档，继续读实际页面；不把异常中的正文或密码写出。
                LOG.debug("[%s] 等待提交提示页面：%s", self.s.task.id, type(exc).__name__)
            await asyncio.sleep(.05)

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
        self.s.store.event(self.s.task.id, "success", {"order_id": oid, "price": str(quote.total) if quote.total is not None else None,
            "payment_status": 'paid' if self.s.auto_payment.status == 'paid' else "pending" if pending else "未核实支付状态"})
        if pending:
            payment = "订单待付款，请手动付款"
        else:
            payment = "在线支付，等待手动付款"
        if self.s.task.auto_pay:
            payment = '订单已生成，继续自动付款' if self.s.auto_payment.status != 'paid' else '已核对付款成功'
        price = f"应付 ¥{quote.total}" if quote.total is not None else "金额请在订单页查看"
        LOG.info("[%s] 已确认订单号=%s，%s，%s", self.s.task.id, oid, price, payment)
        await self.snapshot("success")
        await notify(self.s.cfg.notification["desktop"], "京东下单成功", f"{self.s.task.name}，{price}，订单号 {oid}，{payment}")

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
        policy = self.s.task.retry
        start = self.s.scheduler.now_ns()
        cutoff = ((self.s.target_ns if self.s.target_ns is not None else start)
                  + int(policy['timeout_seconds'] * 1e9))
        attempts = policy['max_attempts'] if policy['enabled'] else 1
        previous = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._submit_once(previous, retry=attempt > 1)
            except SubmissionRejected as exc:
                previous = self.s.checkout_quote
                delay = max(policy['interval_ms'] / 1000 * min(2 ** (attempt - 1), 8),
                            exc.feedback.get('retry_after_seconds', 0))
                now = self.s.scheduler.now_ns()
                if (attempt >= attempts or now + int(delay * 1e9) >= cutoff or
                        self.s.auto_payment.triggered):
                    self.s.store.finish(self.s.key, 'unknown', detail='明确提示：' + str(exc) + '；重试窗口已截止，保留防重记录')
                    raise StopOrder('京东提示“' + str(exc) + '”，已关闭弹窗；本轮重试次数或时间窗口耗尽，未确认订单') from None
                self.s.store.event(self.s.task.id, 'submission_retry',
                    {'attempt': attempt, 'delay_ms': round(delay * 1000), 'reason': str(exc)})
                LOG.info('[%s] 已关闭提交过快提示，%.2f 秒后在当前结算页有限重试，不刷新购物车', self.s.task.id, delay)
                await self.s.scheduler.wait(now + int(delay * 1e9))

    async def _submit_once(self, previous=None, *, retry=False):
        if (urlparse(self.page.url).hostname == 'pc-settlement-lite-pro.pf.jd.com' and
                not getattr(self, 'payment_guard', None)):
            raise StopOrder('轻量结算必须先启用仅生成订单的付款拦截，停止最终点击')
        if self.s.store.get(self.s.key) and not retry:
            raise StopOrder("同一任务已有提交记录，请先核实京东订单中心")
        guard = getattr(self.s, "submission_guard", None)
        if guard is not None and not retry:
            guard.check(self.s.task, self.s.target_ns)
        fast_cart = self.s.checkout.fast_cart_ready and not retry
        pre_submit_text = await result_body_text(self.page)
        if fast_cart:
            await self.s.checkout.check_fast_cart_interrupt(pre_submit_text)
        # 清掉操作之前就存在的限频提示，不能误把旧提示当作本次请求被明确拒绝。
        existing = submission_feedback(pre_submit_text)
        if existing and existing['kind'] == 'rate_limit':
            if not await dismiss_submission_prompt(self.page, existing['message'], self.activate,
                    timeout=min(700, self.s.cfg.jd['action_timeout_ms'])):
                raise StopOrder('结算页已有提交过快提示，但无法确认关闭，未继续点击订单按钮')
        if fast_cart:
            quote, button = await self.s.checkout.prepare_fast_cart()
        else:
            quote = await self.s.checkout.validate()
            if await self.s.checkout.fill_payment_password():
                quote = await self.s.checkout.validate()
        if previous is not None and previous.total is not None and quote.total > previous.total:
            raise StopOrder('限频重试前应付金额上涨，停止继续提交')
        self.s.checkout_quote = quote
        self.s.auto_payment.arm(quote)
        if not fast_cart:
            button = await self.find("submit")
            if not await button.is_enabled() or await button.get_attribute("aria-disabled") == "true":
                from core.errors import Retryable
                raise Retryable("提交订单按钮暂不可用")
            await self.s.checkout.assert_submission_allowed(button, final=True)
        elif not self.s.checkout.fast_cart_ready:
            raise StopOrder('购物车结算窗口已切换，停止最终提交')
        self.s.scheduler.check_stop()
        if (self.s.target_ns is not None and
                self.s.scheduler.now_ns() >= self.s.target_ns + int(self.s.task.retry['timeout_seconds'] * 1e9)):
            raise StopOrder('本任务购买窗口已截止，停止最终提交')
        persist = lambda: self.s.store.claim(self.s.key, self.s.task.id, quote.total)
        if not retry:
            if guard is None:
                persist()
            else:
                guard.claim(self.s.task, self.s.target_ns, persist, amount=quote.total)
        else:
            self.s.store.update_quote(self.s.key, quote.total)
        self.s.submitted = True
        if self.s.task.auto_pay:
            # “立即支付”入口可能由页面直接继续扣款；第一次点击之前就保留付款意图。
            self.s.auto_payment.reserve(quote.total)
        submitted_at = self.s.scheduler.now_ns() / 1e9
        error = None
        origin = self.page
        password_closed = False
        closed_at = None
        success_seen_at = None
        feedback = {}
        attempt_started_ns = time.perf_counter_ns()
        watcher = asyncio.create_task(self.watch_feedback(attempt_started_ns, feedback),
            name="jd-submit-feedback:" + self.s.task.id)
        try:
            with measure(self.s.task.id, "提交点击与导航"):
                click_started_ns = self.s.scheduler.now_ns()
                completed = False
                try:
                    await self.click_transition(button)
                    completed = True
                finally:
                    click_ended_ns = self.s.scheduler.now_ns()
                    stamp = lambda ns: datetime.fromtimestamp(ns / 1e9, CST).isoformat(timespec="milliseconds")
                    click = {"started": stamp(click_started_ns), "ended": stamp(click_ended_ns),
                        "action_completed": completed, "fast_cart": fast_cart}
                    for attr, name in (('cart_selected_ns', 'since_selection_ms'),
                            ('cart_checkout_reached_ns', 'since_checkout_ms')):
                        point = getattr(self.s, attr, None)
                        if point is not None:
                            click[name] = round((click_started_ns - point) / 1e6, 3)
                    self.s.store.event(self.s.task.id, "submit_click", click, update_state=False)
                    LOG.info("[%s] 提交动作校准时间：开始=%s 结束=%s 动作完成=%s；不代表订单已受理",
                        self.s.task.id, click["started"], click["ended"], completed)
                    if fast_cart:
                        LOG.info('[%s] 购物车直接提交（沿用页面默认支付）：勾选至提交 %.1f ms，结算页出现至提交 %.1f ms',
                            self.s.task.id, click.get('since_selection_ms', 0), click.get('since_checkout_ms', 0))
            deadline = time.monotonic() + self.s.cfg.jd["result_timeout_seconds"]
            while time.monotonic() < deadline:
                self.s.scheduler.check_stop()
                if await self.s.checkout.password_error():
                    error = StopOrder("京东提示支付密码校验未通过，请核实订单状态")
                    break
                if self.s.task.auto_pay:
                    preview = await result_body_text(self.page)
                    known = self.success(preview) if urlparse(self.page.url).hostname != 'order.jd.com' else None
                    if known:
                        await self.confirmed(known, quote, pending=bool(re.search(r'待付款|待支付|等待付款', preview)))
                        return known
                    await self.s.auto_payment.advance(quote)
                elif not password_closed and await self.dismiss_post_submit_password(origin):
                    password_closed = True
                    closed_at = time.monotonic()
                    continue
                text = await result_body_text(self.page)
                found = self.success(text) if urlparse(self.page.url).hostname != "order.jd.com" else None
                if found:
                    if success_seen_at is None:
                        success_seen_at = time.monotonic()
                    if password_closed or time.monotonic() - success_seen_at >= .35:
                        await self.confirmed(found, quote, pending=bool(re.search(r"待付款|待支付|等待付款", text)))
                        return found
                else:
                    success_seen_at = None
                if not feedback:
                    observed = submission_feedback(text)
                    if observed:
                        await self.capture_feedback(attempt_started_ns, feedback, observed)
                if feedback and not found:
                    error = StopOrder("提交阶段页面提示：" + feedback["message"])
                    break
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
        finally:
            # 截图先于只读查单完成，避免查单数十秒后才记录原页面。
            if feedback and not watcher.done():
                try:
                    async with asyncio.timeout(1.7):
                        await asyncio.shield(watcher)
                except TimeoutError:
                    pass
            if not watcher.done():
                watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if (completed and feedback.get('kind') == 'rate_limit' and feedback.get('closed') and
                self.page is origin and checkout_url(self.page.url) and
                not getattr(self, 'payment_guard', None) and not self.s.auto_payment.triggered):
            raise SubmissionRejected(feedback)
        self.s.store.finish(self.s.key, "unknown", detail="提交后未读到成功页，正在只读查询订单中心")
        found = await self.reconcile(quote, submitted_at)
        if found:
            await self.confirmed(found, quote, pending=True)
            return found
        detail = "未能确认订单；不会重发，请人工核实"
        if feedback:
            detail = "提交阶段页面提示：" + feedback["message"] + "；" + detail
        self.s.store.finish(self.s.key, "unknown", detail=detail)
        await self.snapshot("unknown-order")
        message = "已尝试提交，成功页和订单中心均未能确认；已停止重试，请核实京东订单中心"
        if feedback:
            message = "提交阶段页面提示“" + feedback["message"] + "”；" + message
        raise StopOrder(message) from error
