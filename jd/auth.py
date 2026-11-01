"""只读页面登录状态。页面未加载完与已退出登录是不同状态。"""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from jd.dom import main_body_text

AUTH_USER = ".nickname, #ttbar-login .user-name, #ttbar-login .user_name, a[href*='logout.action']"
CHALLENGE = re.compile(r"请完成.{0,6}验证|拖动.{0,8}滑块|安全验证|访问过于频繁|验证码验证|登录保护|账号存在风险|账户存在风险")
QUICK_CHALLENGE = re.compile(r"验证一下[，,、\s]*购物无忧")
LOGIN_GATE = re.compile(r"您尚未登录|请先登录|请点击下方按钮登录|前往登录|你好，请登录")


@dataclass
class AuthState:
    state: str
    reason: str
    credentials_present: bool = False


def credential_names_valid(names):
    return {"pt_key", "pt_pin"} <= names or ("thor" in names and bool({"pin", "pinId", "unick"} & names))


async def observe(page, context, selectors=None, *, _recheck=True):
    selectors = selectors or {}
    host = urlparse(page.url).hostname or ""
    if not (host == "jd.com" or host.endswith(".jd.com")):
        return AuthState("pending", "尚未进入京东页面")
    # 只处理名称，不输出或保存 Cookie 的内容。
    cookies = await context.cookies(["https://www.jd.com", "https://cart.jd.com"])
    credentials = credential_names_valid({c["name"] for c in cookies if c.get("value")})
    body = await main_body_text(page)
    # 京东独立验证页没有“安全验证”四字，也没有购物车商品行。
    if QUICK_CHALLENGE.search(body):
        return AuthState("verification", "京东显示“验证一下，购物无忧”，请完成人工快速验证", credentials)
    if "快速验证" in body:
        if await page.get_by_text("快速验证", exact=True).filter(visible=True).count():
            return AuthState("verification", "京东显示“快速验证”入口，请完成人工验证", credentials)
    if CHALLENGE.search(body):
        return AuthState("verification", "页面要求人工安全验证", credentials)
    if host == "passport.jd.com":
        return AuthState("logged_out", "当前为京东登录入口", credentials)
    # 由浏览器一次过滤、读取，避免逐个账号节点跨进程往返。
    users = page.locator(selectors.get("auth_user", AUTH_USER)).filter(visible=True)
    for text in await users.all_inner_texts():
        text = text.strip()
        if text and not LOGIN_GATE.search(text):
            return AuthState("authenticated", "可见账号标识已加载", credentials)
    # 有可见账号标识时，上方已返回；未加载的隐藏登录模板不用于判定。
    controls = page.get_by_role("link", name=re.compile(r"^(?:你好，?请登录|请登录|前往登录|立即登录|登录)$"))
    controls = controls.or_(page.get_by_role("button", name=re.compile(r"^(?:前往登录|立即登录|登录)$")))
    if await controls.filter(visible=True).count():
        return AuthState("logged_out", "存在可见的登录入口", credentials)
    if re.search(r"请点击下方按钮登录|您尚未登录|请先登录", body):
        return AuthState("logged_out", "页面明确要求登录", credentials)
    # 老版已登录购物车可能没有昵称；有凭据和实际商品行才作辅助证据。
    if host == "cart.jd.com" and credentials:
        rows = page.locator(selectors.get("cart_row", ".item-item")).filter(visible=True)
        if await rows.count():
            return AuthState("authenticated", "登录凭据及可见购物车商品行", True)
    # 正文与登录控件分开读取；渲染切换可能使第一次正文仍是登录入口，
    # 后续控件却已消失。只在这类未确认结果中复查变化，最多重采样一次。
    if _recheck and await main_body_text(page) != body:
        return await observe(page, context, selectors, _recheck=False)
    return AuthState("pending", "页面登录组件尚未确认，不能判为退出登录", credentials)
