"""用户提供的 Elements 截图所示结算区域；只读，不点击或认为下单已获验证。"""
import re
from urllib.parse import parse_qs, urljoin, urlparse

from core.config import money
from core.errors import StopOrder


# 来自用户截图中的稳定 ID/类名；不绑定截图内会变化的散列后缀。
PARTIAL_SELECTORS = {
    "checkout_root": "#__ultron_rootContainer_node",
    "settlement_panel": "#settlementPanelContainer",
    "submit_region": "#submitOrder",
    "submit": '#submitOrder .trade-buy-btn-submit div[class^="btn--"]',
    "final_payable": '[class^="cartSettlementTotalDiscountV2--"] .trade-price-container',
    "order_card": "#orderCard",
    "checkout_item": ".trade-buy-item-info",
    "item_link": 'a[class^="itemTitle--"]',
    "item_specs": ".itemAttrsWrapper",
    "item_quantity": ".itemQuantityWrapper",
    "item_spec_value": 'span[class^="attr--"]',
    "quantity_input": '[class^="buyQuantity--"] input.ant-input',
    "payment_card": "#paymentCard",
    "payment_options": "label.ant-checkbox-wrapper",
    "address_card": "#addressCard",
    "address_items": '[class^="listItemWrap--"]',
}


def parse_submit_evidence(text):
    compact = re.sub(r"\s+", "", text)
    match = re.fullmatch(r"立即支付[¥￥](\d+(?:,\d{3})*(?:\.\d{1,2})?)", compact)
    if not match:
        raise StopOrder("淘宝结算操作文案或金额未唯一识别，需更新页面证据")
    return {"label": "立即支付", "displayed_amount": str(money(match[1], allow_zero=True)),
            "amount_verified": False, "action_semantics": "payment_wording_unverified",
            "automatic_submit_ready": False}


def product_link_evidence(href, title):
    parsed = urlparse(urljoin("https://buy.taobao.com/", href or ""))
    query = parse_qs(parsed.query)
    ids = query.get("id", [])
    if (parsed.scheme != "https" or parsed.hostname not in ("item.taobao.com", "detail.tmall.com")
            or parsed.path != "/item.htm" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or len(ids) != 1 or not re.fullmatch(r"\d{1,30}", ids[0])
            or not title.strip()):
        raise StopOrder("淘宝结算商品链接不能唯一证明商品 ID 与名称")
    return {"product_id": ids[0], "name": title.strip(), "variant_verified": False}


def verify_product_evidence(report, task):
    """已有原生商品证据时必须与任务一致，不让配置错误的定位器覆盖它。"""
    items = report["items"]
    if items:
        if len(items) != 1 or items[0]["product_id"] != task.sku:
            raise StopOrder("淘宝实际结算商品 ID 与任务不一致")
        if re.sub(r"\s+", "", items[0]["name"]) != re.sub(r"\s+", "", task.name):
            raise StopOrder("淘宝实际结算商品名称与任务不一致")
        if items[0]["quantity"] is not None and items[0]["quantity"] != task.quantity:
            raise StopOrder("淘宝实际结算数量与任务不一致")
        if items[0]["specs_read"]:
            actual = {re.sub(r"\s+", "", group): re.sub(r"\s+", "", value)
                      for group, value in items[0]["specs"].items()}
            for group, value in task.sku_options.items():
                if actual.get(re.sub(r"\s+", "", group)) != re.sub(r"\s+", "", value):
                    raise StopOrder("淘宝实际结算规格与任务不一致")


async def inspect_payment_options(card):
    """读取截图所示 Ant checkbox；不按顺序猜测名称，不选择或点击。"""
    labels = card.locator(PARTIAL_SELECTORS["payment_options"])
    options = []
    names = {"支付宝": "alipay", "分期付款": "installment", "朋友代付": "friend_pay", "号码保护": "phone_protection"}
    for index in range(await labels.count()):
        label = labels.nth(index)
        if not await label.is_visible():
            continue
        selected = "ant-checkbox-wrapper-checked" in (await label.get_attribute("class") or "").split()
        aria = await label.get_attribute("aria-checked")
        inputs = label.locator('input[type="checkbox"]')
        if await inputs.count() > 1:
            raise StopOrder("淘宝支付选项包含多个复选控件，状态不明确")
        if ((aria in ("true", "false") and (aria == "true") != selected)
                or (await inputs.count() == 1 and await inputs.is_checked() != selected)):
            raise StopOrder("淘宝支付选项的选中标记与控件状态不一致")
        text = re.sub(r"\s+", "", await label.inner_text())
        matched = [value for name, value in names.items() if name in text]
        kind = matched[0] if len(matched) == 1 else "unknown"
        options.append({"kind": kind, "selected": selected})
    selected_methods = [option["kind"] for option in options if option["selected"] and option["kind"] in ("alipay", "installment", "friend_pay")]
    if len(selected_methods) > 1:
        raise StopOrder("淘宝同时选中多个支付方式，停止继续操作")
    unknown_selected = sum(option["selected"] and option["kind"] == "unknown" for option in options)
    method = selected_methods[0] if len(selected_methods) == 1 and not unknown_selected else None
    return {"options": options, "selected_method": method, "verified": method is not None,
            "unknown_selected_count": unknown_selected}


async def inspect_taobao_checkout(page):
    """仅用于用户已授权的页面或本地模拟；不得用来规避工具侧站点访问限制。"""
    parsed = urlparse(page.url)
    if (parsed.scheme != "https" or parsed.hostname != "buy.taobao.com"
            or parsed.path != "/auction/order/confirm_order.htm"
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise StopOrder("不是截图所示淘宝确认订单页")

    async def unique_visible(selector, scope=None):
        nodes = (scope if scope is not None else page).locator(selector)
        visible = [nodes.nth(i) for i in range(await nodes.count()) if await nodes.nth(i).is_visible()]
        if len(visible) != 1:
            raise StopOrder("淘宝结算区域或操作控件不能唯一确认")
        return visible[0]

    root = await unique_visible(PARTIAL_SELECTORS["checkout_root"])
    panel = await unique_visible(PARTIAL_SELECTORS["settlement_panel"], root)
    await unique_visible(PARTIAL_SELECTORS["submit_region"])
    button = await unique_visible(PARTIAL_SELECTORS["submit"])
    if not await root.locator(PARTIAL_SELECTORS["submit_region"]).count():
        raise StopOrder("淘宝提交区域不在已识别结算容器中")
    report = parse_submit_evidence(await button.inner_text())
    payment = {"options": [], "selected_method": None, "verified": False, "unknown_selected_count": 0}
    if await panel.locator(PARTIAL_SELECTORS["payment_card"]).count():
        payment_card = await unique_visible(PARTIAL_SELECTORS["payment_card"], panel)
        payment = await inspect_payment_options(payment_card)
    address_present = False
    address_count = 0
    if await root.locator(PARTIAL_SELECTORS["address_card"]).count():
        address_card = await unique_visible(PARTIAL_SELECTORS["address_card"], root)
        address_present = True
        address_count = await address_card.locator(PARTIAL_SELECTORS["address_items"]).count()
    items = []
    if await root.locator(PARTIAL_SELECTORS["order_card"]).count():
        card = await unique_visible(PARTIAL_SELECTORS["order_card"], root)
        nodes = card.locator(PARTIAL_SELECTORS["checkout_item"])
        visible_items = [nodes.nth(i) for i in range(await nodes.count()) if await nodes.nth(i).is_visible()]
        if len(visible_items) != 1:
            raise StopOrder("淘宝结算必须仅包含一个目标商品行")
        item = visible_items[0]
        link = await unique_visible(PARTIAL_SELECTORS["item_link"], item)
        evidence = product_link_evidence(await link.get_attribute("href"), await link.inner_text())
        evidence.update({"spec_container_present": await item.locator(PARTIAL_SELECTORS["item_specs"]).count() == 1,
                         "quantity_container_present": await item.locator(PARTIAL_SELECTORS["item_quantity"]).count() == 1,
                         "quantity": None, "specs": {}, "specs_read": False})
        if evidence["spec_container_present"]:
            attrs = await unique_visible(PARTIAL_SELECTORS["item_specs"], item)
            values = attrs.locator(PARTIAL_SELECTORS["item_spec_value"])
            for index in range(await values.count()):
                attr = values.nth(index)
                if not await attr.is_visible():
                    continue
                text = re.sub(r"\s+", "", await attr.inner_text())
                match = re.fullmatch(r"([^:：]+)[:：](.+)", text)
                if not match or match[1] in evidence["specs"]:
                    raise StopOrder("淘宝结算规格文字格式不明或规格组重复")
                evidence["specs"][match[1]] = match[2]
            evidence["specs_read"] = bool(evidence["specs"])
        if evidence["quantity_container_present"]:
            quantities = await unique_visible(PARTIAL_SELECTORS["item_quantity"], item)
            if await quantities.locator(PARTIAL_SELECTORS["quantity_input"]).count():
                quantity = await unique_visible(PARTIAL_SELECTORS["quantity_input"], quantities)
                value = (await quantity.input_value()).strip()
                if not re.fullmatch(r"[1-9]\d*", value):
                    raise StopOrder("淘宝结算数量不是明确的正整数")
                evidence["quantity"] = int(value)
        items.append(evidence)
    final_payable = None
    totals = panel.locator(PARTIAL_SELECTORS["final_payable"])
    if await totals.count():
        total = await unique_visible(PARTIAL_SELECTORS["final_payable"], panel)
        parts = {}
        for name in ("label", "symbol", "integer", "point", "decimal"):
            node = await unique_visible(".trade-price-" + name, total)
            parts[name] = re.sub(r"\s+", "", await node.inner_text())
        if (parts["label"] != "合计" or parts["symbol"] not in ("¥", "￥")
                or not re.fullmatch(r"\d+(?:,\d{3})*", parts["integer"])
                or parts["point"] != "." or not re.fullmatch(r"\d{2}", parts["decimal"])):
            raise StopOrder("淘宝最终合计金额结构或文案不符，不能完成核价")
        final_payable = money(parts["symbol"] + parts["integer"] + "." + parts["decimal"], allow_zero=True)
        current_submit = parse_submit_evidence(await button.inner_text())
        if (money(report["displayed_amount"], allow_zero=True) != final_payable
                or money(current_submit["displayed_amount"], allow_zero=True) != final_payable):
            raise StopOrder("淘宝合计与立即支付显示金额不一致，停止继续操作")
        report["amount_verified"] = True
    report.update({"platform": "taobao", "page_kind": "checkout",
        "submit_tag": await button.evaluate("el => el.tagName.toLowerCase()"),
        "disabled": await button.get_attribute("aria-disabled") == "true" or not await button.is_enabled(),
        "evidence_source": "user_supplied_elements_screenshot",
        "final_payable_amount": str(final_payable) if final_payable is not None else None,
        "amount_verification_scope": "total_matches_submit" if final_payable is not None else "unverified",
        "items": items,
        "payment": payment,
        "address": {"container_present": address_present, "card_count": address_count, "verified": False},
        "missing_evidence": ["checkout_item_id", "checkout_variant_id", "checkout_quantity", "checkout_specs",
                             "final_payable", "address", "selected_payment_method", "order_status", "order_id"]})
    if final_payable is not None:
        report["missing_evidence"].remove("final_payable")
    if items:
        report["missing_evidence"].remove("checkout_item_id")
        if items[0]["quantity"] is not None:
            report["missing_evidence"].remove("checkout_quantity")
        if items[0]["specs_read"]:
            report["missing_evidence"].remove("checkout_specs")
    if payment["verified"]:
        report["missing_evidence"].remove("selected_payment_method")
    return report
