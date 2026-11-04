"""只关闭带有已识别订单反馈的提示框，不点击未知弹窗的付款/协议按钮。"""
import re
import time
import asyncio

from jd.password_prompt import jd_page


ACK = re.compile(r"^(?:知道了|我知道了|确定|确认|关闭)$")


async def dismiss_submission_prompt(page, message, activate, timeout=700):
    if not jd_page(page.url):
        return False
    messages = page.get_by_text(re.compile(re.escape(message))).filter(visible=True)
    if await messages.count() != 1:
        return False
    text = messages.first
    scope = text
    for _ in range(8):
        tag = await scope.evaluate('el=>el.tagName')
        if tag in ('BODY', 'HTML'):
            return False
        controls = scope.get_by_role('button', name=ACK).or_(scope.get_by_role('link', name=ACK)).or_(
            scope.get_by_text(ACK, exact=True)).filter(visible=True)
        count = await controls.count()
        if count > 1:
            return False
        if count == 1:
            button = controls.first
            if not await button.is_enabled():
                return False
            await activate(button, timeout=timeout)
            deadline = time.monotonic() + timeout / 1000
            while await text.is_visible():
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(.02)
            return True
        scope = scope.locator('xpath=..')
    return False
