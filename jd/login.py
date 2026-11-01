import asyncio
import logging
import time
from urllib.parse import urlparse

from core.errors import LoginRequired, Retryable, StopOrder, VerificationRequired
from jd.auth import AuthState, credential_names_valid, observe
from jd.common import PageTools
from utils.notifier import notify

LOG = logging.getLogger(__name__)


async def has_credentials(context):
    cookies = await context.cookies(["https://www.jd.com", "https://cart.jd.com"])
    return credential_names_valid({c["name"] for c in cookies if c.get("value")})


class Login(PageTools):
    async def status(self):
        return await observe(self.page, self.s.context, self.s.selectors)

    async def valid(self):
        state = await self.status()
        if state.state == "verification":
            raise VerificationRequired(state.reason)
        return state.state == "authenticated"

    async def settled(self, timeout, *, require_stable_account=True):
        deadline = time.monotonic() + timeout
        authenticated_since = None
        logged_out_since = None
        state = AuthState("pending", "账号组件尚未读取")
        while time.monotonic() < deadline:
            self.s.scheduler.check_stop()
            state = await self.status()
            if state.state == "verification":
                raise VerificationRequired(state.reason)
            if state.state == "authenticated":
                # guard 已读到明确账号标识即可继续，不必再固定等一次稳定时长。
                if not require_stable_account:
                    return state
                logged_out_since = None
                if authenticated_since is None:
                    authenticated_since = time.monotonic()
                if time.monotonic() - authenticated_since >= self.s.cfg.jd["login_settle_seconds"]:
                    return state
            elif state.state == "logged_out":
                authenticated_since = None
                if logged_out_since is None:
                    logged_out_since = time.monotonic()
                if (not state.credentials_present and
                        time.monotonic() - logged_out_since >= self.s.cfg.jd["login_settle_seconds"]):
                    return state
            else:
                authenticated_since = None
                logged_out_since = None
            # 刷新后的占位登录入口只需等真实账号出现；缩短本地采样间隔，
            # 不发送登录/库存请求，也不缩短明确登出所需的稳定时间。
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(0.25 if require_stable_account else 0.05, remaining))
        # 一次页面采样本身可能跨过截止时间；结束前复查验证弹层，
        # 不把采样过程中出现的人工验证误归为普通加载超时。
        self.s.scheduler.check_stop()
        latest = await self.status()
        if latest.state == "verification":
            raise VerificationRequired(latest.reason)
        if latest.state == "authenticated" and not require_stable_account:
            return latest
        if (latest.state == "logged_out" and state.state == "logged_out" and logged_out_since is not None and
                time.monotonic() - logged_out_since >= self.s.cfg.jd["login_settle_seconds"]):
            return latest
        return AuthState("pending", "账号状态尚未稳定或登录组件尚未加载", latest.credentials_present)

    async def record(self, state):
        url = urlparse(self.page.url)
        detail = {"state": state.state, "reason": state.reason,
            "credentials_present": state.credentials_present, "page": (url.hostname or "") + url.path}
        self.s.store.event(self.s.task.id, "login_check", detail)
        LOG.info("[%s] 登录检测=%s 原因=%s 凭据存在=%s 页面=%s", self.s.task.id,
            state.state, state.reason, state.credentials_present, detail["page"])

    async def wait_manual(self, reason, login=False):
        taskid = self.s.task.id
        if login and "passport.jd.com" not in self.page.url and await self.valid():
            # 刷新时的登录占位符可能已被真实账号替换；先复查，再截图/发通知。
            LOG.info("[%s] 登录入口已恢复为账号标识，保留当前页面继续，无需人工操作", taskid)
            return
        self.s.store.event(taskid, "manual", reason)
        await self.snapshot("manual")
        await notify(self.s.cfg.notification["desktop"], "京东需要人工操作",
            f"{self.s.task.name}：{reason}，请在 Chrome 完成，完成后自动继续")
        LOG.warning("[%s] %s；等待人工完成，随后自动继续", taskid, reason)
        if login and "passport.jd.com" not in self.page.url:
            # 页面已恢复登录时不再次送往扫码入口。
            if await self.valid():
                return
            await self.page.goto("https://passport.jd.com/new/login.aspx", wait_until="domcontentloaded")
        await self.page.bring_to_front()
        deadline = time.monotonic() + self.s.cfg.jd["manual_timeout_seconds"]
        authenticated_since = None
        while time.monotonic() < deadline:
            self.s.scheduler.check_stop()
            await asyncio.sleep(0.25)
            try:
                state = await self.status()
            except Exception as exc:
                from playwright.async_api import Error as PlaywrightError
                if not isinstance(exc, PlaywrightError) or self.page.is_closed():
                    raise
                # 扫码后的导航可能暂时销毁旧文档；继续等待新文档。
                authenticated_since = None
                continue
            if state.state == "authenticated":
                if authenticated_since is None:
                    authenticated_since = time.monotonic()
                if time.monotonic() - authenticated_since >= self.s.cfg.jd["login_settle_seconds"]:
                    await self.record(state)
                    LOG.info("[%s] 登录/人工验证状态已稳定，继续执行", taskid)
                    self.s.store.event(taskid, "resumed", "账号状态稳定，保留当前页面")
                    return
            else:
                authenticated_since = None
        raise StopOrder("等待扫码/人工验证超时；未自动反复打开登录页")

    async def ensure(self, manual=True):
        host = urlparse(self.page.url).hostname or ""
        if host == "passport.jd.com" or not (host == "jd.com" or host.endswith(".jd.com")):
            await self.page.goto("https://www.jd.com/", wait_until="domcontentloaded", timeout=20000)
        try:
            state = await self.settled(self.s.cfg.jd["login_check_timeout_seconds"])
            await self.record(state)
            if state.state == "authenticated":
                return
            if state.state == "logged_out":
                if not manual:
                    raise LoginRequired("页面明确要求登录，请扫码一次")
                await self.wait_manual("页面明确要求登录，请扫码一次", login=True)
                return
            raise Retryable("登录组件尚未确认，保留现有会话等待；不将页面加载中误判为登出")
        except VerificationRequired as exc:
            if not manual:
                raise
            await self.wait_manual(str(exc))

    async def login(self):
        # 用户重复点击“扫码登录”时，优先复用已有效的会话。
        await self.page.goto("https://www.jd.com/", wait_until="domcontentloaded", timeout=20000)
        try:
            state = await self.settled(self.s.cfg.jd["login_check_timeout_seconds"])
            await self.record(state)
            if state.state == "authenticated":
                LOG.info("[%s] 已登录，复用现有会话，无需再次扫码", self.s.task.id)
                return
            if state.state == "verification":
                await self.wait_manual(state.reason)
                return
        except VerificationRequired as exc:
            await self.wait_manual(str(exc))
            return
        await self.page.goto("https://passport.jd.com/new/login.aspx", wait_until="domcontentloaded")
        await self.wait_manual("请使用京东 App 扫码登录", login=True)
        # 不再为“验证登录”强制跳到购物车，不重复开启登录流程。
