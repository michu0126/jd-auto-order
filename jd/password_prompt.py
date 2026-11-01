"""提交后密码弹窗只关闭，不填入密码，不确认付款。"""
import re
from urllib.parse import urlparse
from playwright.async_api import Error as PlaywrightError

from core.errors import StopOrder

PASSWORD_TEXT = re.compile(r"(?:支付|交易)密码")
CLOSE_NAMES = ("关闭", "关闭弹窗", "Close", "暂不支付", "稍后支付", "取消支付")


def jd_page(url):
    host = urlparse(url).hostname or ""
    return host == "jd.com" or host.endswith(".jd.com")


async def dismiss_password_dialog(page, selectors, timeout, activate=None):
    if not jd_page(page.url):
        return False
    found = []
    for frame in page.frames:
        if frame != page.main_frame and frame.url != "about:blank" and not jd_page(frame.url):
            continue
        dialogs = frame.locator(selectors["payment_password_dialog"]).filter(has_text=PASSWORD_TEXT)
        for index in range(await dialogs.count()):
            node = dialogs.nth(index)
            if await node.is_visible():
                found.append(node)
    if not found:
        return False
    if len(found) != 1:
        raise StopOrder("提交后出现多个密码弹窗，停止自动处理，请核实订单")
    dialog = found[0]
    close = None
    for label in CLOSE_NAMES:
        controls = dialog.get_by_role("button", name=label, exact=True).or_(dialog.get_by_role("link", name=label, exact=True))
        matches = [controls.nth(i) for i in range(await controls.count()) if await controls.nth(i).is_visible()]
        if len(matches) == 1:
            close = matches[0]
            break
    if close is None:
        nodes = dialog.locator(selectors["payment_password_dialog_close"])
        matches = [nodes.nth(i) for i in range(await nodes.count()) if await nodes.nth(i).is_visible()]
        if len(matches) == 1:
            close = matches[0]
    if close is None:
        raise StopOrder("已识别提交后密码弹窗，但找不到明确的关闭按钮；不会填写密码或再次下单")
    # 不使用“取消订单”等操作代替关闭付款验证。
    close_text = " ".join((await close.inner_text(), await close.get_attribute("aria-label") or "", await close.get_attribute("title") or ""))
    if re.search(r"取消订单|关闭订单|删除订单|撤销订单", close_text):
        raise StopOrder("关闭候选控件会取消订单，未点击，请人工核实")
    if activate is None:
        await close.click(timeout=timeout)
    else:
        await activate(close, timeout=timeout)
    try:
        await dialog.wait_for(state="hidden", timeout=timeout)
    except PlaywrightError:
        if not page.is_closed():
            raise
    return True
