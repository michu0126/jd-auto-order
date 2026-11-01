"""只读天猫商品信息：定位来自本次真实商品页，不据此声称结算已适配。"""
import re
from urllib.parse import urlparse

from core.errors import Retryable, StopOrder
from core.platforms import product_id, variant_id

PANEL = "#SkuPanel_tbpcDetail_ssr2025"
OPTIONS = '#skuOptionsArea [data-vid][class*="valueItem--"]'


def normalized(text):
    return re.sub(r"\s+", "", text)


async def inspect_tmall_product(page):
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != "detail.tmall.com" or parsed.path != "/item.htm":
        raise StopOrder("此商品读取器只适用于已观察到的天猫详情页")
    panel = page.locator(PANEL)
    await panel.wait_for(state="visible")
    title = re.sub(r"-tmall\.com天猫$", "", await page.title()).strip()
    view = await panel.evaluate("""root => {
        const visible = el => !!el && !!el.getClientRects().length;
        const options = [...root.querySelectorAll('#skuOptionsArea [data-vid][class*="valueItem--"]')]
            .filter(visible).map(el => {
                const group = el.closest('[class*="skuItem--"]');
                const label = group?.querySelector('[class*="ItemLabel--"]');
                return {group:label?.innerText.trim() || '', text:el.innerText.trim(),
                    value_id:el.getAttribute('data-vid'), selected:/isSelected--/.test(el.className),
                    disabled:el.getAttribute('data-disabled') === 'true'};
            });
        const quantity = root.querySelector('input[class*="countValue--"]');
        const buttons = [...root.querySelectorAll('#tbpcDetail_SkuPanelFoot button')]
            .filter(visible).map(el => ({text:el.innerText.trim(), disabled:el.disabled || el.getAttribute('aria-disabled')==='true'}));
        return {options,quantity:quantity?.value || '',buttons};
    }""")
    authenticated = await page.locator(".site-nav-login-info-nick:visible").count() > 0
    button_text = " ".join(button["text"] for button in view["buttons"])
    state = "not_started" if re.search(r"即将开始|尚未开售", button_text) else "unknown"
    return {"platform": "taobao", "product_id": product_id("taobao", page.url),
        "requested_variant_id": variant_id(page.url), "variant_verified": False,
        "name": title, "authenticated": authenticated,
        "size_available": any(re.search(r"尺码|尺寸", option["group"]) for option in view["options"]),
        "purchase_state": state, "automatic_checkout_verified": False, **view}


async def prepare_tmall_product(page, task):
    """核对已观察到的详情结构并选择用户明确配置的规格；不点击购买。"""
    report = await inspect_tmall_product(page)
    if report["product_id"] != task.sku or task.sku.startswith("link-"):
        raise StopOrder("天猫商品 ID 与任务不一致")
    if normalized(report["name"]) != normalized(task.name):
        raise StopOrder("天猫商品名称与任务不一致")
    if task.variant_id and report["requested_variant_id"] != task.variant_id:
        raise StopOrder("天猫链接 SKU 与任务不一致")
    if not report["authenticated"]:
        from core.errors import LoginRequired
        raise LoginRequired("天猫详情页未确认登录，请正常登录后继续")
    options = page.locator(PANEL).locator(OPTIONS)
    for group, expected in task.sku_options.items():
        matches = []
        for index in range(await options.count()):
            option = options.nth(index)
            if not await option.is_visible():
                continue
            label = await option.evaluate("""el => el.closest('[class*="skuItem--"]')
                ?.querySelector('[class*="ItemLabel--"]')?.innerText || ''""")
            if normalized(label) == normalized(group) and normalized(await option.inner_text()) == normalized(expected):
                matches.append(option)
        if len(matches) != 1:
            raise StopOrder(f"天猫规格 {group}={expected} 未唯一匹配；页面没有该规格时请留空")
        option = matches[0]
        if await option.get_attribute("data-disabled") == "true":
            raise Retryable("天猫目标规格暂不可选")
        if "isSelected--" not in (await option.get_attribute("class") or ""):
            await option.click()
        # 点击后等待实际选中标记，不能把 click 成功当作规格生效。
        await option.locator('xpath=self::*[contains(@class,"isSelected--")]').wait_for(state="visible")
    report = await inspect_tmall_product(page)
    for group, expected in task.sku_options.items():
        selected = [o for o in report["options"] if normalized(o["group"]) == normalized(group) and o["selected"]]
        if len(selected) != 1 or normalized(selected[0]["text"]) != normalized(expected):
            raise StopOrder("天猫规格选中状态不一致")
    groups = {o["group"] for o in report["options"]}
    if not groups or any(sum(o["selected"] and not o["disabled"] for o in report["options"] if o["group"] == group) != 1 for group in groups):
        raise StopOrder("天猫商品规格尚未唯一选中")
    quantity = page.locator(PANEL).locator('input[class*="countValue--"]')
    if await quantity.count() != 1 or not await quantity.is_visible():
        raise StopOrder("天猫数量控件不能唯一确认")
    maximum = await quantity.get_attribute("max")
    if maximum and maximum.isdigit() and task.quantity > int(maximum):
        raise StopOrder("任务数量超过天猫页面限购数量")
    if await quantity.input_value() != str(task.quantity):
        await quantity.fill(str(task.quantity))
        await quantity.press("Tab")
    if await quantity.input_value() != str(task.quantity):
        raise StopOrder("天猫购买数量未按任务生效")
    report = await inspect_tmall_product(page)
    if report["purchase_state"] == "not_started":
        raise Retryable("天猫商品尚未开售，等待目标时间后再次检查")
    if not any(not button["disabled"] for button in report["buttons"]):
        raise Retryable("天猫商品购买控件暂不可用")
    return report
