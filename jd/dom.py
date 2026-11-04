"""读取主文档，避免浏览器扩展的 Shadow DOM 造成正文定位歧义。"""
import asyncio
import time

from playwright.async_api import Error as PlaywrightError


async def main_body_text(page):
    # CSS 定位会穿透开放的 Shadow DOM；document.body 明确指向主文档。
    # 尚无正文时返回空文本，由登录稳定检查/订单结果循环继续等待。
    return await page.evaluate("() => document.body?.innerText ?? ''")


async def result_body_text(page):
    """提交或付款刚导航时短暂重读新文档，不将文档销毁当作下单失败。"""
    deadline = time.monotonic() + .5
    while True:
        try:
            return await main_body_text(page)
        except PlaywrightError as exc:
            if (page.is_closed() or 'Execution context was destroyed' not in str(exc)
                    or time.monotonic() >= deadline):
                raise
            await asyncio.sleep(.025)
