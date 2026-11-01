"""复现用户截图中的最小结构；全部请求本地拦截，无淘宝网络访问。"""
import unittest
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from playwright.async_api import async_playwright

from core.errors import StopOrder
from marketplaces.taobao_checkout import inspect_taobao_checkout, parse_submit_evidence, product_link_evidence, verify_product_evidence
from marketplaces.web import WebPurchase


class TaobaoSubmitTextTests(unittest.TestCase):
    def test_split_label_currency_and_amount_are_read_only_evidence(self):
        report = parse_submit_evidence("立即支付\n￥\n35.80")
        self.assertEqual(report["displayed_amount"], "35.80")
        self.assertFalse(report["automatic_submit_ready"])
        self.assertFalse(report["amount_verified"])

    def test_multiple_amounts_or_other_action_are_not_guessed(self):
        for text in ("立即支付¥35.80¥28.80", "返回", "立即支付", "确认付款¥35.80"):
            with self.subTest(text=text), self.assertRaises(StopOrder):
                parse_submit_evidence(text)

    def test_product_link_rejects_external_or_ambiguous_identity(self):
        for href in ("https://example.test/item.htm?id=123", "//item.taobao.com/item.htm?id=123&id=456", "javascript:void(0)"):
            with self.subTest(href=href), self.assertRaises(StopOrder):
                product_link_evidence(href, "测试商品")

    def test_link_sku_parameter_is_not_proof_of_actual_variant(self):
        item = product_link_evidence("//item.taobao.com/item.htm?id=123&skuId=456", "测试商品")
        self.assertEqual(item["product_id"], "123")
        self.assertFalse(item["variant_verified"])


class TaobaoCheckoutScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def load(self, *, duplicate=False, outside=False, total=None, label="合计", total_copies=1, bound_total="35.80", product_ids=None, detailed=False, specs=None, quantity="1", payment_options=None, address_cards=0, payment_input_checked=None):
        action = '<div class="btn--changedHash" onclick="document.body.dataset.submitted=1"><div>立即支付<br>￥<br>35.80</div></div>'
        region = '<div id="submitOrder"><div class="SettlementSubmit--changedHash trade-buy-btn-submit"><div class="btnBox--changedHash"><a href="/return">返回</a>' + action + (action if duplicate else '') + '</div></div></div>'
        summary = "付款详情"
        if total is not None:
            integer, decimal = total.split(".")
            summary += ('<div class="cartSettlementTotalDiscountV2--changedHash"><div class="trade-price-container">'
                        f'<span class="trade-price-label">{label}</span><span class="trade-price-symbol">¥</span>'
                        f'<span class="trade-price-integer">{integer}</span><span class="trade-price-point">.</span>'
                        f'<span class="trade-price-decimal">{decimal}</span></div></div>') * total_copies
        # A subtotal outside the observed total wrapper must never be used as final payable.
        summary += '<div class="trade-price-container"><span class="trade-price-label">商品总价</span>¥28.80</div>'
        if payment_options is not None:
            labels = []
            for text, selected in payment_options:
                marker = ' ant-checkbox-wrapper-checked' if selected else ''
                checked = '' if payment_input_checked is None else '<input type="checkbox"' + (' checked' if payment_input_checked else '') + '>'
                labels.append('<div class="settlementOption--changedHash"><label class="ant-checkbox-wrapper' + marker + '"><span class="ant-checkbox">' + checked + '</span><span>' + escape(text) + '</span></label></div>')
            summary += '<div id="paymentCard">' + ''.join(labels) + '</div>'
        fixture_item = '<article class="fixture-item"><span class="fixture-id">123</span><span class="fixture-qty">1</span></article><span class="fixture-address">模拟地址</span><span class="fixture-total">¥' + bound_total + '</span>'
        if address_cards:
            fixture_item += '<div id="addressCard"><div class="buyAddress--changedHash"><div class="listWrap--changedHash">' + '<div class="listItemWrap--changedHash">模拟收货卡片，内容不得进入报告</div>' * address_cards + '</div></div></div>'
        if product_ids is not None:
            attrs = qty = '待补充子节点'
            if detailed:
                attrs = '<div class="attrsWrapper--changedHash">' + ''.join('<span class="attr--changedHash">' + escape(group) + '\n:\n' + escape(value) + '</span>' for group, value in (specs if specs is not None else [("颜色分类", "测试黑色一个")])) + '</div>'
                qty = '<div class="buyQuantity--changedHash"><div class="quantityWrapper--changedHash" title="数量1"><div aria-label="减少按钮">−</div><input class="ant-input inputWrapper--changedHash" type="text" aria-label="数量1" value="' + escape(quantity, quote=True) + '"><div aria-label="增加按钮">+</div></div></div>'
            rows = ''.join('<div class="trade-buy-item-info buyItemInfoWrapperWithPresale"><div class="buyItemInfoWrapperNew"><div class="itemDetailWrapper"><a class="itemTitle--changedHash" href="//item.taobao.com/item.htm?id=' + pid + '&mi_id=fixture">测试商品</a></div><div class="itemAttrsWrapper">' + attrs + '</div><div class="itemQuantityWrapper">' + qty + '</div></div></div>' for pid in product_ids)
            fixture_item += '<div id="orderCard">' + rows + '</div>'
        html = '<div id="__ultron_rootContainer_node">' + fixture_item + '<div id="settlementContainer"><div id="settlementPanelContainer">' + summary + '</div>' + ('' if outside else region) + '</div></div>' + (region if outside else '')
        async def fulfill(route):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        await self.page.route("**/*", fulfill)
        await self.page.goto("https://buy.taobao.com/auction/order/confirm_order.htm")

    async def test_screenshot_div_is_detected_without_clicking(self):
        await self.load()
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["submit_tag"], "div")
        self.assertEqual(report["displayed_amount"], "35.80")
        self.assertFalse(report["automatic_submit_ready"])
        self.assertIn("final_payable", report["missing_evidence"])
        self.assertIsNone(await self.page.locator("body").get_attribute("data-submitted"))

    async def test_two_visible_payment_controls_block(self):
        await self.load(duplicate=True)
        with self.assertRaisesRegex(StopOrder, "不能唯一确认"):
            await inspect_taobao_checkout(self.page)

    async def test_submit_outside_checkout_root_blocks(self):
        await self.load(outside=True)
        with self.assertRaisesRegex(StopOrder, "不在已识别结算容器"):
            await inspect_taobao_checkout(self.page)

    async def test_split_total_matches_button_but_does_not_enable_submit(self):
        await self.load(total="35.80")
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["final_payable_amount"], "35.80")
        self.assertTrue(report["amount_verified"])
        self.assertNotIn("final_payable", report["missing_evidence"])
        self.assertFalse(report["automatic_submit_ready"])
        self.assertIsNone(await self.page.locator("body").get_attribute("data-submitted"))

    async def test_total_and_button_mismatch_blocks(self):
        await self.load(total="36.80")
        with self.assertRaisesRegex(StopOrder, "显示金额不一致"):
            await inspect_taobao_checkout(self.page)

    async def test_subtotal_label_is_not_accepted_as_final_payable(self):
        await self.load(total="35.80", label="商品总价")
        with self.assertRaisesRegex(StopOrder, "合计金额结构或文案不符"):
            await inspect_taobao_checkout(self.page)

    async def test_two_visible_totals_block(self):
        await self.load(total="35.80", total_copies=2)
        with self.assertRaisesRegex(StopOrder, "不能唯一确认"):
            await inspect_taobao_checkout(self.page)

    async def test_incomplete_decimal_is_not_guessed(self):
        await self.load(total="35.8")
        with self.assertRaisesRegex(StopOrder, "合计金额结构或文案不符"):
            await inspect_taobao_checkout(self.page)

    async def test_configured_subtotal_cannot_override_native_final_payable(self):
        await self.load(total="35.80", bound_total="28.80")
        # The fixture selectors below model a mistaken user binding; none are vendor selectors.
        profile = {"checkout_paths": ["/auction/order/confirm_order.htm"], "selectors": {
            "checkout_root": "#__ultron_rootContainer_node", "checkout_item": ".fixture-item",
            "checkout_item_id": ".fixture-id", "checkout_quantity": ".fixture-qty",
            "address": ".fixture-address", "total": ".fixture-total"}}
        cfg = SimpleNamespace(platforms={"taobao": profile}, session_dir=Path("unused-test-profile"),
                              jd={"action_timeout_ms": 500})
        task = SimpleNamespace(platform="taobao", id="test", sku="123", variant_id="", quantity=1,
                               sku_options={}, address_keyword="", max_price=None,
                               target=SimpleNamespace(timestamp=lambda: 0), key=lambda path: "fixture")
        flow = WebPurchase(self.page.context, self.page, cfg, task, Mock(), Mock())
        with self.assertRaisesRegex(StopOrder, "配置金额定位与实际最终合计不一致"):
            await flow.validate()
        self.assertIsNone(await self.page.locator("body").get_attribute("data-submitted"))

    async def test_screenshot_item_link_proves_product_but_not_quantity_or_variant(self):
        await self.load(total="35.80", product_ids=["782961113232"])
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["items"][0]["product_id"], "782961113232")
        self.assertEqual(report["items"][0]["name"], "测试商品")
        self.assertNotIn("checkout_item_id", report["missing_evidence"])
        self.assertIn("checkout_variant_id", report["missing_evidence"])
        self.assertIn("checkout_quantity", report["missing_evidence"])
        self.assertFalse(report["automatic_submit_ready"])
        task = SimpleNamespace(sku="1094601672942", name="测试商品")
        with self.assertRaisesRegex(StopOrder, "实际结算商品 ID 与任务不一致"):
            verify_product_evidence(report, task)
        task.sku = "782961113232"
        verify_product_evidence(report, task)
        task.name = "其他商品"
        with self.assertRaisesRegex(StopOrder, "实际结算商品名称"):
            verify_product_evidence(report, task)

    async def test_multiple_checkout_items_block(self):
        await self.load(product_ids=["123", "456"])
        with self.assertRaisesRegex(StopOrder, "仅包含一个目标商品行"):
            await inspect_taobao_checkout(self.page)

    async def test_expanded_specs_and_quantity_are_read_without_variant_assumption(self):
        await self.load(total="35.80", product_ids=["123"], detailed=True)
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["items"][0]["specs"], {"颜色分类": "测试黑色一个"})
        self.assertEqual(report["items"][0]["quantity"], 1)
        self.assertNotIn("checkout_quantity", report["missing_evidence"])
        self.assertNotIn("checkout_specs", report["missing_evidence"])
        self.assertIn("checkout_variant_id", report["missing_evidence"])
        task = SimpleNamespace(sku="123", name="测试商品", quantity=1, sku_options={"颜色分类": "测试黑色一个"})
        verify_product_evidence(report, task)
        task.quantity = 2
        with self.assertRaisesRegex(StopOrder, "实际结算数量"):
            verify_product_evidence(report, task)
        task.quantity = 1
        task.sku_options = {"尺码": "M"}
        with self.assertRaisesRegex(StopOrder, "实际结算规格"):
            verify_product_evidence(report, task)
        self.assertFalse(report["automatic_submit_ready"])
        self.assertIsNone(await self.page.locator("body").get_attribute("data-submitted"))

    async def test_quantity_uses_current_input_property_not_initial_attribute_or_label(self):
        await self.load(product_ids=["123"], detailed=True)
        node = self.page.locator(".itemQuantityWrapper input")
        await node.fill("2")
        self.assertEqual(await node.get_attribute("value"), "1")
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["items"][0]["quantity"], 2)

    async def test_duplicate_spec_group_blocks(self):
        await self.load(product_ids=["123"], detailed=True, specs=[("颜色分类", "黑色"), ("颜色分类", "白色")])
        with self.assertRaisesRegex(StopOrder, "规格组重复"):
            await inspect_taobao_checkout(self.page)

    async def test_non_integer_quantity_is_not_guessed(self):
        for value in ("", "0", "-1", "1.5"):
            with self.subTest(value=value):
                await self.load(product_ids=["123"], detailed=True, quantity=value)
                with self.assertRaisesRegex(StopOrder, "数量不是明确的正整数"):
                    await inspect_taobao_checkout(self.page)

    async def test_selected_phone_protection_is_not_an_extra_payment_method(self):
        await self.load(payment_options=[("支付宝", True), ("分期付款", False), ("朋友代付", False), ("号码保护", True)])
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["payment"]["selected_method"], "alipay")
        self.assertTrue(report["payment"]["verified"])
        self.assertNotIn("selected_payment_method", report["missing_evidence"])
        self.assertFalse(report["automatic_submit_ready"])
        self.assertIsNone(await self.page.locator("body").get_attribute("data-submitted"))

    async def test_unknown_selected_option_keeps_payment_unverified(self):
        await self.load(payment_options=[("支付宝", True), ("尚未识别的选项", True)])
        report = await inspect_taobao_checkout(self.page)
        self.assertFalse(report["payment"]["verified"])
        self.assertEqual(report["payment"]["unknown_selected_count"], 1)
        self.assertIn("selected_payment_method", report["missing_evidence"])

    async def test_phone_protection_alone_does_not_prove_online_payment(self):
        await self.load(payment_options=[("支付宝", False), ("号码保护", True)])
        report = await inspect_taobao_checkout(self.page)
        self.assertFalse(report["payment"]["verified"])

    async def test_multiple_payment_methods_block(self):
        await self.load(payment_options=[("支付宝", True), ("分期付款", True)])
        with self.assertRaisesRegex(StopOrder, "同时选中多个支付方式"):
            await inspect_taobao_checkout(self.page)

    async def test_native_checkbox_disagrees_with_checked_class_blocks(self):
        await self.load(payment_options=[("支付宝", True)], payment_input_checked=False)
        with self.assertRaisesRegex(StopOrder, "选中标记与控件状态不一致"):
            await inspect_taobao_checkout(self.page)

    async def test_address_list_does_not_prove_selected_address_or_expose_content(self):
        await self.load(address_cards=6)
        report = await inspect_taobao_checkout(self.page)
        self.assertEqual(report["address"], {"container_present": True, "card_count": 6, "verified": False})
        self.assertIn("address", report["missing_evidence"])
        self.assertNotIn("模拟收货卡片", str(report))
