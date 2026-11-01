"""只定位结算动作，识别与点击分开，付款动作在提交前单独核对。"""
import re
from urllib.parse import urlparse

from core.errors import StopOrder


def checkout_url(url):
    parsed = urlparse(url)
    return ((parsed.hostname == "trade.jd.com" and parsed.path.startswith("/shopping/order/")) or
        (parsed.scheme == 'https' and parsed.hostname == 'pc-settlement-lite-pro.pf.jd.com' and parsed.path == '/'))


async def button_text(button):
    if await button.evaluate("el => el.tagName === 'INPUT'"):
        text = await button.input_value()
    else:
        text = await button.inner_text()
    return re.sub(r"\s+", "", text or await button.get_attribute("aria-label") or "")


async def find_submit(page, selector, labels, custom=False):
    if not checkout_url(page.url):
        return None
    # CSS 保留旧版 ID 和新版付款操作区域；语义按钮还兼容 input、链接。
    nodes = page.locator(selector)
    if not custom:
        nodes = nodes.or_(page.locator("button, a, input[type=button], input[type=submit], [role=button]"))
    matches = await nodes.evaluate_all("""(nodes,labels) => nodes.map((el,index) => ({el,index,
        text:(el.tagName==='INPUT' ? el.value : el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g,'')}))
        .filter(x=>!x.el.closest('footer, .mod_help, .mod_footer, .mod_help_nav') &&
            (labels.includes(x.text) || /^分期付款(?:白条|信用卡)?\\d+期免息$/.test(x.text)))
        .map(({index,text})=>({index,text}))""", labels)
    candidates = []
    for match in matches:
        node = nodes.nth(match["index"])
        if not await node.is_visible():
            continue
        candidates.append((node, match["text"], await node.is_enabled() and await node.get_attribute("aria-disabled") != "true"))
    if not candidates:
        return None
    enabled = [candidate for candidate in candidates if candidate[2]]
    usable = enabled or candidates
    if len({text for _, text, _ in usable}) > 1:
        raise StopOrder("结算页有多个不同的提交动作，无法确定当前按钮，请检查页面或更新 submit 定位器")
    # 同文案的固定栏/底部栏属于同一结算动作，优先可用的可见按钮。
    text = usable[0][1]
    pattern = re.compile(r"^\s*" + r"\s*".join(re.escape(char) for char in text) + r"\s*$")
    stable = nodes.filter(has_text=pattern).or_(nodes.and_(page.get_by_role("button", name=pattern)))
    visible = ":visible:not([aria-disabled=true]):not(:disabled)" if enabled else ":visible"
    return stable.and_(page.locator(visible)).first
