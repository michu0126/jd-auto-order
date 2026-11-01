"""读取主文档，避免浏览器扩展的 Shadow DOM 造成正文定位歧义。"""


async def main_body_text(page):
    # CSS 定位会穿透开放的 Shadow DOM；document.body 明确指向主文档。
    # 尚无正文时返回空文本，由登录稳定检查/订单结果循环继续等待。
    return await page.evaluate("() => document.body?.innerText ?? ''")
