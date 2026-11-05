from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, async_playwright

from core.clock_sync import calibrate
from core.config import Config
from core.errors import Cancelled, LoginRequired, ParallelSubmission, Retryable, StopOrder, VerificationRequired
from core.retry import RetryBudget
from core.purchase_lock import PurchaseLanes, PurchaseLease, SubmissionGuard, validate_task_concurrency
from core.scheduler import Scheduler
from core.state_machine import StateStore
from jd.browser import BrowserConnection, Session, launch
from jd.cart_scan import CartScanner
from core.config_store import catalog_failure
from core.payment_secret import PaymentSecret, read_payment_secret
from core.platforms import LABELS, readiness
from core.recurrence import next_target
from marketplaces.web import WebPurchase
from utils.logger import configure, measure
from utils.notifier import notify

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("main")


async def recover(session, exc):
    await session.login.wait_manual(str(exc), login=isinstance(exc, LoginRequired))


async def stage(session, operation):
    for _ in range(3):
        try:
            return await operation()
        except (LoginRequired, VerificationRequired) as exc:
            await recover(session, exc)
        except (Retryable, PlaywrightError) as exc:
            LOG.warning("[%s] 预热暂未完成：%s", session.task.id, str(exc).splitlines()[0])
            await asyncio.sleep(session.task.retry["interval_ms"] / 1000)
    raise Retryable("预热操作重试后仍未完成")


async def purchase_attempt(session, warmed, attempt):
    if session.task.mode == "direct":
        if attempt > 1 or not getattr(session, "product_prepared", False) or "item.jd.com" not in session.page.url:
            await session.product.open()
            await session.product.specs()
        await session.product.buy()
        from jd.embedded_checkout import bind_embedded
        from jd.submit_button import checkout_url
        deadline = time.monotonic() + session.cfg.jd['action_timeout_ms'] / 1000
        while not checkout_url(session.page.url):
            if await bind_embedded(session):
                break
            session.scheduler.check_stop()
            if time.monotonic() >= deadline:
                raise Retryable('详情页购买后尚未出现完整结算页或已识别的嵌入式结算')
            await asyncio.sleep(.05)
    else:
        try:
            await session.cart.checkout(refresh=not warmed or attempt > 1)
        except Retryable:
            if session.task.source == "cart":
                raise
            await session.product.open()
            await session.product.specs()
            await session.product.buy()
            await session.cart.checkout()
    if getattr(session.checkout, 'fast_cart_ready', False):
        # 购物车选中后直达提交入口；默认优惠和支付均沿用页面，不逐项扫描。
        discounts = session.task.discounts
        if (discounts['coupon_text'] or not discounts['use_existing_coupons'] or
                discounts['use_beans'] or discounts['use_red_packets']):
            await session.checkout.discounts()
        return
    await session.tools.guard()
    await session.tools.find("checkout_goods")
    await session.checkout.discounts()


async def preload_cart(session, purchase_lock, deadline_ns, *, refresh=False, check_login=False, attempts=3):
    """准备阶段的页面超时不终止定时任务；每轮和整个阶段都有截止时间。"""
    scheduler = session.scheduler
    for attempt in range(attempts):
        scheduler.check_stop()
        remaining = (deadline_ns - scheduler.now_ns()) / 1e9
        if remaining <= 0:
            return False
        session.store.event(session.task.id, "preloading", "提前加载购物车，未开售商品无需勾选")
        try:
            async with asyncio.timeout(min(session.cfg.timing["cart_preload_timeout_seconds"], remaining)):
                async with purchase_lock:
                    await session.cart.preload(refresh=refresh or attempt > 0)
                if check_login:
                    async with session.auth_lock:
                        await session.login.ensure(manual=False)
            if scheduler.now_ns() >= deadline_ns:
                session.cart_preloaded = False
                return False
            session.store.event(session.task.id, "waiting", "购物车已预加载，等待开售；暂不可勾选属于正常等待")
            LOG.info("[%s] 购物车已提前加载，不操作未开售商品，等待抢购时间", session.task.id)
            return True
        except (LoginRequired, VerificationRequired) as exc:
            async with session.auth_lock:
                await recover(session, exc)
        except (Retryable, PlaywrightError, TimeoutError) as exc:
            session.cart_preloaded = False
            if session.page.is_closed():
                raise StopOrder("浏览器页面已关闭，请重新启动任务并保持 Chrome 打开") from exc
            reason = (str(exc).splitlines() or ["页面预加载仍在等待响应"])[0]
            LOG.info("[%s] 购物车预加载暂未就绪，保留定时任务：%s", session.task.id, reason)
            session.store.event(session.task.id, "preloading", "页面仍在加载或暂不可用，将在开售前继续准备")
        if attempt + 1 < attempts:
            await scheduler.wait(min(deadline_ns, scheduler.now_ns() + 2_000_000_000))
    return False


async def prepare_scheduled_cart(session, purchase_lock, target_ns):
    scheduler = session.scheduler
    polling_start = target_ns - int(session.cfg.timing["cart_refresh_start_before_seconds"] * 1e9)
    # 启动即显示真实购物车，不让新标签页空白等待到开售前两秒。
    await preload_cart(session, purchase_lock,
        min(polling_start, scheduler.now_ns() + 8_000_000_000), refresh=True, attempts=1)
    await scheduler.wait(target_ns - int(session.cfg.timing["prewarm_seconds"] * 1e9))
    login_deadline = target_ns - 30_000_000_000
    if login_deadline <= scheduler.now_ns():
        login_deadline = polling_start
    ready = await preload_cart(session, purchase_lock, login_deadline, check_login=True)
    # 可选的只读订单基线不能占掉页面准备和开售窗口。
    baseline_seconds = min(5, (target_ns - 30_000_000_000 - scheduler.now_ns()) / 1e9)
    if ready and session.task.auto_submit and baseline_seconds > 0:
        try:
            async with asyncio.timeout(baseline_seconds):
                await session.order.baseline()
        except TimeoutError:
            LOG.warning("[%s] 订单基线读取较慢，停止等待，优先准备购物车；未知结果仍保持防重", session.task.id)
    await scheduler.wait(target_ns - 30_000_000_000)
    if scheduler.now_ns() < polling_start:
        # 留足加载时间；临近开售时复用刚加载的页面，避免反复导航。
        refresh = scheduler.now_ns() < target_ns - 10_000_000_000
        await preload_cart(session, purchase_lock, polling_start, refresh=refresh)


async def preload_product(session, deadline_ns, *, refresh=False, check_login=False, attempts=2):
    """详情页预热有硬截止；定位旧版失败也不能吞掉开售后的购买窗口。"""
    scheduler = session.scheduler
    for attempt in range(attempts):
        scheduler.check_stop()
        remaining = (deadline_ns - scheduler.now_ns()) / 1e9
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(min(8, remaining)):
                if refresh or attempt > 0 or "item.jd.com" not in session.page.url:
                    await session.product.open()
                else:
                    await session.tools.guard()
                    await session.product.read()
                if check_login:
                    async with session.auth_lock:
                        await session.login.ensure(manual=False)
                await session.product.specs()
                await session.product.state()
            session.store.event(session.task.id, "waiting", "详情页已预加载并核对规格与数量，等待目标时间")
            return True
        except (LoginRequired, VerificationRequired) as exc:
            async with session.auth_lock:
                await recover(session, exc)
        except (Retryable, PlaywrightError, TimeoutError) as exc:
            session.product_prepared = False
            if session.page.is_closed():
                raise StopOrder("浏览器页面已关闭，请重新启动任务并保持 Chrome 打开") from exc
            LOG.info("[%s] 详情页预热暂未就绪，保留到点尝试：%s", session.task.id,
                (str(exc).splitlines() or ["本阶段等待已截止"])[0])
        if attempt + 1 < attempts:
            await scheduler.wait(min(deadline_ns, scheduler.now_ns() + int(session.task.retry["interval_ms"] * 1e6)))
    return False


async def prepare_scheduled_product(session, target_ns):
    scheduler = session.scheduler
    deadline = target_ns - 3_000_000_000
    # 启动后即打开页面；临近开售时跳过已过期的准备阶段，准点进入购买尝试。
    await preload_product(session, min(deadline, scheduler.now_ns() + 8_000_000_000), refresh=True, attempts=1)
    await scheduler.wait(target_ns - int(session.cfg.timing["prewarm_seconds"] * 1e9))
    await preload_product(session, min(deadline, scheduler.now_ns() + 8_000_000_000), check_login=True, attempts=1)
    baseline_seconds = min(5, (target_ns - 30_000_000_000 - scheduler.now_ns()) / 1e9)
    if session.task.auto_submit and baseline_seconds > 0:
        try:
            async with asyncio.timeout(baseline_seconds):
                await session.order.baseline()
        except TimeoutError:
            LOG.info("[%s] 订单基线读取已截止，优先保留详情页抢购窗口", session.task.id)
    await scheduler.wait(target_ns - 30_000_000_000)
    await preload_product(session, min(deadline, max(target_ns - 10_000_000_000, scheduler.now_ns())), attempts=2)
    await scheduler.wait(target_ns - 10_000_000_000)
    await preload_product(session, deadline, attempts=1)


async def warm_cart(session, purchase_lock, target_ns, *, lease=None):
    """开售前两秒开始，逐轮刷新至勾选核对成功，最晚到任务截止时间。"""
    retain_selected = lease is not None
    claim = lease if lease is not None else PurchaseLease(purchase_lock)
    if claim.lock is not purchase_lock:
        raise ValueError("购物车和结算必须使用同一个账号锁")
    scheduler = session.scheduler
    cutoff = target_ns + int(session.task.retry["timeout_seconds"] * 1e9)
    start = target_ns - int(session.cfg.timing["cart_refresh_start_before_seconds"] * 1e9)
    interval = int(session.cfg.timing["cart_refresh_interval_ms"] * 1e6)
    await scheduler.wait(start)
    count = 0
    while scheduler.now_ns() < cutoff:
        scheduler.check_stop()
        count += 1
        warmed = False
        started = scheduler.now_ns()
        lock_started = time.perf_counter_ns()
        lock_wait_ms = None
        result = "ready"
        try:
            # 每轮顺序加载、核对、再等待间隔；不叠加导航或超过任务截止时间。
            # 开售前的慢请求不能把首轮检查拖到开售后5秒才超时。
            round_deadline = min(cutoff, target_ns if started < target_ns else cutoff)
            async with asyncio.timeout(min(10, (round_deadline - started) / 1e9)):
                await claim.acquire()
                lock_wait_ms = (time.perf_counter_ns() - lock_started) / 1e6
                try:
                    scheduler.check_stop()
                    await session.cart.select(refresh=not (count == 1 and getattr(session, "cart_preloaded", False)))
                    expected = getattr(session.cart, 'expected_selection', {"sku": session.task.sku, "quantity": session.task.quantity})
                    if session.cart_selection != expected:
                        raise Retryable("尚未确认目标商品勾选与数量")
                    if scheduler.now_ns() >= cutoff:
                        raise TimeoutError("勾选核对完成时已超过任务截止时间")
                    session.cart_selected_ns = scheduler.now_ns()
                    warmed = True
                finally:
                    # 成功时由 run_one 的首轮结算尝试接续持有，不能先释放再排队。
                    # 未勾选、异常或独立预热调用仍立即释放，允许其他任务继续。
                    if not (warmed and retain_selected):
                        claim.release()
            LOG.info("[%s] 购物车轮询第 %d 次：目标商品勾选与数量核对成功，停止刷新", session.task.id, count)
            if retain_selected:
                LOG.info("[%s] 已保留购物车操作锁，连续进入本次结算，其他购物车任务等待", session.task.id)
        except (Retryable, PlaywrightError, TimeoutError) as exc:
            if session.page.is_closed():
                raise StopOrder("浏览器页面已关闭，请重新启动任务并保持 Chrome 打开") from exc
            result = (str(exc).splitlines() or ["本轮等待已到开售时刻，立即继续检查" if started < target_ns else "本轮刷新达到任务截止时间"])[0]
            LOG.info("[%s] 购物车轮询第 %d 次：%s", session.task.id, count, result)
        except (LoginRequired, VerificationRequired) as exc:
            result = str(exc)
            async with session.auth_lock:
                await recover(session, exc)
        if not warmed:
            session.cart_selection = None
            session.cart_selected_ns = None
        if lock_wait_ms is None:
            lock_wait_ms = (time.perf_counter_ns() - lock_started) / 1e6
        session.store.event(session.task.id, "cart_refresh", {"round": count, "ready": warmed,
            "result": result, "elapsed_ms": (scheduler.now_ns() - started) / 1e6,
            "lock_wait_ms": round(lock_wait_ms, 3), "selection_lock_retained": warmed and retain_selected,
            "steps_ms": dict(getattr(session, "cart_timings", {}))}, update_state=False)
        if warmed:
            return True
        await scheduler.wait(min(cutoff, target_ns if started < target_ns else cutoff, scheduler.now_ns() + interval))
    return False


async def run_one(session, purchase_lock, dry_run=False, now=False):
    lease = PurchaseLease(purchase_lock)
    try:
        return await _run_one(session, purchase_lock, lease, dry_run, now)
    finally:
        # 覆盖提前等待、截止、取消、人工验证和提交异常，不能遗留账号锁。
        lease.release()


async def _run_one(session, purchase_lock, lease, dry_run=False, now=False):
    task, scheduler = session.task, session.scheduler
    if not dry_run and task.auto_submit and session.store.get(session.key):
        raise StopOrder("本任务已存在提交记录，请核实京东订单中心")
    target = scheduler.now_ns() if now or dry_run else round(task.target.timestamp() * 1e9)
    if target < scheduler.now_ns() - 2_000_000_000:
        raise StopOrder("目标时间已过期，请修改配置或使用 --now")
    session.target_ns = target
    session.store.event(task.id, "waiting", f"计划 {task.target.isoformat()}，SKU {task.sku}")
    warmed = False
    from_cart = task.mode == "cart" and task.source == "cart"
    cart_polling = from_cart and session.cfg.timing["cart_refresh_enabled"] and not (now or dry_run)
    if cart_polling:
        await prepare_scheduled_cart(session, purchase_lock, target)
        session.store.event(task.id, "cart_polling", "进入抢购窗口，等待目标可勾选")
        warmed = await warm_cart(session, purchase_lock, target, lease=lease)
        if not warmed:
            raise StopOrder("购物车轮询已到任务截止时间，仍未勾选目标商品；未提交订单")
    elif task.mode == "direct" and not (now or dry_run):
        await prepare_scheduled_product(session, target)
    else:
        await scheduler.wait(target - int(session.cfg.timing["prewarm_seconds"] * 1e9))
        async with session.auth_lock:
            await stage(session, session.login.ensure)
        if not dry_run and task.auto_submit:
            await session.order.baseline()
        await scheduler.wait(target - 30_000_000_000)
        try:
            await stage(session, session.cart.open if from_cart else session.product.open)
        except Retryable:
            LOG.warning("[%s] 商品预加载未完成，到点继续尝试", task.id)
        await scheduler.wait(target - 10_000_000_000)
        try:
            if from_cart:
                async with purchase_lock:
                    await session.cart.select(refresh=False)
                    warmed = True
            elif "item.jd.com" not in session.page.url:
                await stage(session, session.product.open)
            if not from_cart:
                await stage(session, session.product.specs)
                await session.product.state()
            if task.mode == "cart" and task.source != "cart":
                async with purchase_lock:
                    await session.product.buy()
                    await session.cart.select()
                    warmed = True
        except (Retryable, PlaywrightError) as exc:
            if session.page.is_closed():
                raise StopOrder("浏览器页面已关闭") from exc
            session.cart_selection = None
            LOG.info("[%s] 开售前购买条件暂未满足：%s", task.id, str(exc).splitlines()[0])
        except (LoginRequired, VerificationRequired) as exc:
            await recover(session, exc)
    if cart_polling:
        selected_ns = scheduler.now_ns()
        if selected_ns < target:
            session.store.event(task.id, "armed", "目标商品已勾选，到点进入结算")
            await scheduler.wait(target)
        else:
            # 目标时刻是最早执行时间；已过时则直接结算，不再走定时等待。
            scheduler.check_stop()
            LOG.info("[%s] 勾选核对成功时已过目标时间 %.3f ms，立即进入结算，无额外定时等待",
                task.id, (selected_ns - target) / 1e6)
            session.store.event(task.id, "armed", "目标商品已勾选，已到或超过目标时间，立即进入结算")
    else:
        await scheduler.wait(target - 3_000_000_000)
        session.store.event(task.id, "armed", "进入最后 3 秒精确计时阶段")
        await scheduler.wait(target)
    budget = RetryBudget(task.retry)
    budget.deadline = time.monotonic() + max(0, (target + int(task.retry["timeout_seconds"] * 1e9) - scheduler.now_ns()) / 1e9)
    last_error = ""
    while budget.available():
        scheduler.check_stop()
        budget.begin()
        last_error = ""
        session.store.event(task.id, "attempt", {"attempt": budget.count, "result": "started"})
        started = time.perf_counter_ns()
        try:
            # 购物车通道保持连续锁；直接购买通道各自执行，不排在购物车轮询后。
            async with lease:
                scheduler.check_stop()
                if time.monotonic() >= budget.deadline:
                    raise StopOrder("等待账号结算锁时已超过重试截止时间")
                guard = getattr(session, "submission_guard", None)
                if guard is not None and not (dry_run or not task.auto_submit):
                    guard.check(task, session.target_ns)
                with measure(task.id, f"尝试 {budget.count} 购买与结算"):
                    async with asyncio.timeout(max(0, budget.deadline - time.monotonic())):
                        await purchase_attempt(session, warmed, budget.count)
                if time.monotonic() >= budget.deadline:
                    raise StopOrder("已超过本任务重试截止时间，停止最终提交")
                if dry_run or not task.auto_submit:
                    await session.checkout.validate()
                    await session.tools.snapshot("dry-run")
                    session.store.event(task.id, "dry_run", "结算已核对，没有提交订单")
                    LOG.info("[%s] 演练完成，没有提交订单", task.id)
                else:
                    order_id = await session.order.submit()
                    if getattr(task, 'auto_pay', False):
                        await session.auto_payment.finish(session.checkout_quote, order_id)
            return True
        except (LoginRequired, VerificationRequired) as exc:
            if session.submitted:
                raise StopOrder("提交后需要人工验证，请先核实订单中心") from exc
            await recover(session, exc)
            warmed = False
            last_error = str(exc)
        except (Retryable, PlaywrightError, TimeoutError) as exc:
            if session.submitted:
                raise StopOrder("提交后的页面状态未知，停止重试") from exc
            if session.page.is_closed():
                raise StopOrder("浏览器页面已关闭") from exc
            last_error = (str(exc).splitlines() or ["购买步骤达到本任务截止时间"])[0]
            delay = budget.delay(network=isinstance(exc, PlaywrightError))
            LOG.warning("[%s] 尝试 %d 未提交：%s；重试间隔 %.2f 秒", task.id, budget.count, last_error, delay)
            await session.tools.snapshot(f"attempt-{budget.count}-error")
            if budget.available():
                await scheduler.wait(scheduler.now_ns() + int(delay * 1e9))
            warmed = False
        finally:
            session.store.event(task.id, "attempt_result", {"attempt": budget.count,
                "elapsed_ms": (time.perf_counter_ns() - started) / 1e6,
                "result": "submitted" if session.submitted else (last_error or "completed")}, update_state=False)
    raise StopOrder(f"重试次数或时间窗口耗尽，未提交订单。最后状态：{last_error}")


async def execute(cfg, args, payment_secret=None):
    payment_secret = payment_secret if payment_secret is not None else PaymentSecret()
    stop_file = cfg.data_dir / "stop.flag"
    if not args.noninteractive:
        stop_file.unlink(missing_ok=True)
    offset, source = await calibrate(cfg.timing["ntp_servers"]) if cfg.timing["sync_clock"] else (0, "local-configured")
    offset += round(cfg.timing["clock_offset_ms"] * 1e6)
    scheduler = Scheduler(offset, cfg.timing["precision_mode"], stop_file)
    store = StateStore(cfg.data_dir / "state.sqlite3")
    platform_filter = getattr(args, "platform", None)
    tasks = [cfg.context_task(platform_filter or "jd")] if args.command in ("login", "scan-cart", "browser-check") else [
        task for task in cfg.tasks if task.enabled and (not args.task or task.id == args.task)
        and (not platform_filter or task.platform == platform_filter)]
    if not tasks:
        raise StopOrder("暂无启用的任务，请先从购物车添加商品或启用指定任务")
    if args.command == 'run':
        if not (args.now or args.dry_run):
            tasks = [replace(task, target=next_target(task, scheduler.now_ns()))
                if task.recurrence == 'daily' else task for task in tasks]
    purchase_lanes = PurchaseLanes()
    run_budget = cfg.jd.get('run_budget', {})
    submission_guard = SubmissionGuard(run_budget.get('max_total'), run_budget.get('max_orders'))
    auth_locks = {platform: asyncio.Lock() for platform in LABELS}
    retry_next_day = set()
    try:
        if args.command == 'run':
            validate_task_concurrency(tasks, immediate=args.now or args.dry_run,
                prewarm_seconds=cfg.timing['prewarm_seconds'])
        async with async_playwright() as p, AsyncExitStack() as stack:
            contexts, context_errors = {}, {}
            async def open_platform(platform):
                try:
                    context = await launch(p, cfg.for_platform(platform))
                    contexts[platform] = context
                    stack.push_async_callback(context.close)
                except Exception as exc:
                    context_errors[platform] = exc
            # 独立平台并行启动；未适配的平台不打开浏览器，也不阻断京东。
            await asyncio.gather(*(open_platform(platform) for platform in {task.platform for task in tasks}
                if platform == "jd" or readiness(cfg, platform)["ready"] or args.command == "inspect"))
            async def worker(task):
                session = None
                task_store = store
                local_cfg = cfg.for_platform(task.platform)
                purchase_lock = purchase_lanes.for_task(task)
                try:
                    if task.platform in context_errors:
                        raise context_errors[task.platform]
                    if task.platform not in contexts:
                        raise StopOrder(readiness(cfg, task.platform)["detail"])
                    context = contexts[task.platform]
                    if args.command == "browser-check":
                        count = sum(1 for page in context.pages if not page.is_closed()
                                    and urlsplit(page.url).hostname == "cart.jd.com")
                        detail = f"{'现有 Chrome 已连接' if getattr(context, 'attached', False) else '独立浏览器可用'}，可见购物车标签 {count} 个；未操作商品或提交订单"
                        store.event("browser", "browser_connected", detail)
                        LOG.info(detail)
                        return True
                    page = (await context.acquire_page(reuse_cart=task.platform == "jd" and args.command != "login"
                            and (args.command == "scan-cart" or (task.mode == "cart" and task.source == "cart")))
                            if isinstance(context, BrowserConnection) else await context.new_page())
                    if task.platform != "jd":
                        session = WebPurchase(context, page, local_cfg, task, store, scheduler)
                        session.auth_lock = auth_locks[task.platform]
                        if args.command == "scan-cart":
                            raise StopOrder("附加平台购物车扫描尚未适配，请使用商品链接创建任务")
                        if args.command == "login":
                            await session.login()
                        elif args.command in ("inspect", "prepare"):
                            await session.inspect()
                        else:
                            await session.run(purchase_lock, args.dry_run, args.now)
                        return True
                    session = Session(context, page, local_cfg, task, task_store, scheduler)
                    session.concurrent_ui = sum(item.platform == 'jd' for item in tasks) > 1
                    if task.mode == 'direct' and args.command == 'run':
                        left, right = submission_guard.window(task)
                        session.parallel_detail_count = sum(item.platform == 'jd' and item.mode == 'direct' and
                            (args.now or args.dry_run or (submission_guard.window(item)[0] <= right and
                                submission_guard.window(item)[1] >= left)) for item in tasks)
                    session.submission_guard = submission_guard
                    session.payment_secret = payment_secret
                    session.auth_lock = auth_locks[task.platform]
                    if args.command == "login":
                        await session.login.login()
                        store.event(task.id, "logged_in", "登录状态已持久保存")
                    elif args.command == "scan-cart":
                        await CartScanner(session).scan()
                    elif args.command in ("prepare", "inspect"):
                        await stage(session, session.login.ensure)
                        from_cart = task.mode == "cart" and task.source == "cart"
                        if from_cart:
                            await stage(session, session.cart.open)
                        else:
                            await stage(session, session.product.open)
                            await session.product.specs()
                            await session.product.state()
                        if args.command == "inspect":
                            await session.tools.inspect()
                        elif task.mode == "cart":
                            async with purchase_lock:
                                if not from_cart:
                                    await session.product.buy()
                                await session.cart.select()
                        store.event(task.id, "prepared", "页面检查完成")
                    else:
                        await run_one(session, purchase_lock, args.dry_run, args.now)
                    return True
                except ParallelSubmission as exc:
                    task_store.event(task.id, "duplicate_stopped", str(exc))
                    LOG.info("[%s] %s", task.id, exc)
                    return True
                except Exception as exc:
                    if args.command == "scan-cart" and task.platform == "jd":
                        catalog_failure(cfg.data_dir, str(exc).splitlines()[0])
                    submitted = session is not None and session.submitted
                    status = "unknown" if submitted else "stopped" if isinstance(exc, Cancelled) else "failed"
                    task_store.event(task.id, status, str(exc).splitlines()[0])
                    LOG.error("[%s] %s", task.id, str(exc).splitlines()[0])
                    if not isinstance(exc, (StopOrder, Retryable)):
                        LOG.debug("异常详情", exc_info=True)
                    if session is not None:
                        if task.platform == "jd":
                            await session.tools.snapshot("stopped")
                        else:
                            await session.snapshot("stopped")
                    if not submitted and (isinstance(exc, Retryable) or str(exc).startswith(("重试次数或时间窗口耗尽", "任务重试窗口"))):
                        retry_next_day.add(task.id)
                    await notify(cfg.notification["desktop"], LABELS[task.platform] + "任务已停止", f"{task.name}：{str(exc).splitlines()[0][:160]}")
                    return False
                finally:
                    if session is not None and task.platform == "jd":
                        session.cart.stop_transition_watch()
                        session.cart.clear_selection_handoff()
                    if session is not None and isinstance(contexts.get(task.platform), BrowserConnection):
                        contexts[task.platform].release_page(page)
                    if (session is not None and task.recurrence == "daily" and args.command == "run"
                            and not getattr(contexts.get(task.platform), "attached", False) and not session.page.is_closed()):
                        await session.page.close()

            async def scheduled_worker(task):
                daily = task.recurrence == "daily" and args.command == "run" and not (args.now or args.dry_run)
                if not daily:
                    return await worker(task)
                if store.terminal_for_task(task.id):
                    store.event(task.id, "stopped", "本每日任务已有成功或未确认提交，停止后续日期购买，请核实所属平台订单")
                    return False
                current = replace(task, target=next_target(task, scheduler.now_ns()))
                while True:
                    scheduler.check_stop()
                    retry_next_day.discard(task.id)
                    result = await worker(current)
                    if result or store.terminal_for_task(current.id) or task.id not in retry_next_day:
                        return result
                    from datetime import timedelta
                    current = replace(current, target=next_target(replace(current, target=current.target + timedelta(days=1)), scheduler.now_ns()))
                    store.event(task.id, "waiting", "当日未提交；下次尝试 " + current.target.isoformat())
            if args.command in ("login", "scan-cart", "browser-check"):
                results = [await worker(tasks[0])]
            else:
                results = await asyncio.gather(*(scheduled_worker(task) for task in tasks))
            if cfg.jd["keep_browser_open"] and not args.noninteractive and sys.stdin.isatty():
                try:
                    await asyncio.to_thread(input, "浏览器保留供检查，回车后关闭：")
                except EOFError:
                    pass
            return 0 if all(results) else 2
    finally:
        payment_secret.clear()
        scheduler.close()
        store.close()


def parser():
    result = argparse.ArgumentParser(description="京东定时自动下单工具")
    result.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    sub = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("browser-check", "只检测浏览器连接，不操作商品"), ("login", "扫码登录"), ("scan-cart", "只读扫描购物车商品"), ("prepare", "检查商品和购买条件"),
            ("inspect", "保存商品页定位摘要与截图"), ("run", "定时购买")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--task", help="只运行指定任务 id")
        command.add_argument("--platform", choices=tuple(LABELS), help="平台；省略时运行全部已启用平台")
        command.add_argument("--noninteractive", action="store_true", help="完成后自动关闭浏览器；扫码/验证仍在浏览器完成")
        if name == "run":
            command.add_argument("--payment-secret-stdin", action="store_true", help=argparse.SUPPRESS)
            command.add_argument("--dry-run", action="store_true", help="立即演练，不点击最终提交")
            command.add_argument("--now", action="store_true", help="立即执行")
    sub.add_parser("check", help="验证 YAML 配置")
    sub.add_parser("status", help="查看本地记录")
    web = sub.add_parser("web", help="打开本地管理页面")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true")
    return result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        if args.command == "web":
            from webapp import serve
            serve(args.config.resolve(), args.port, args.open)
            return 0
        cfg = Config.load(args.config)
        configure(cfg)
        if args.command == "check":
            for task in cfg.tasks:
                LOG.info("配置通过：%s %s SKU=%s 时间=%s 最高应付=%s 自动提交=%s",
                    task.id, task.name, task.sku, task.target.isoformat(), task.max_price, task.auto_submit)
            return 0
        if args.command == "status":
            store = StateStore(cfg.data_dir / "state.sqlite3")
            print(json.dumps(store.snapshot(), ensure_ascii=False, indent=2))
            store.close()
            return 0
        secret = read_payment_secret(sys.stdin) if getattr(args, "payment_secret_stdin", False) else PaymentSecret()
        try:
            return asyncio.run(execute(cfg, args, secret))
        finally:
            secret.clear()
    except (StopOrder, Retryable) as exc:
        print(f"任务停止：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("任务已停止。若已点击提交，请核实京东订单中心。")
        return 130
    except Exception:
        logging.exception("启动或运行失败，请查看日志")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
