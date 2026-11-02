import asyncio
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from core.config import money
from core.errors import StopOrder
from jd.common import PageTools, quantity_text, sku_ids
from jd.submit_button import button_text, checkout_url

LOG = logging.getLogger(__name__)


@dataclass
class Quote:
    total: Decimal
    subtotal: Decimal | None
    freight: Decimal | None
    discount: str


class Checkout(PageTools):
    async def payment_selected(self, node):
        """仅用于识别页面明确选中的非在线方式，不匹配银行卡或付款方案。"""
        return await node.evaluate("""el => {
            if(el.matches('input[type=radio],input[type=checkbox]'))return el.checked;
            const native=[...el.querySelectorAll('input[type=radio],input[type=checkbox]')];
            if(native.length)return native.some(input=>input.checked);
            return !!(el.querySelector('input[aria-checked=true], .selected-payment') ||
                el.getAttribute('aria-checked') === 'true' || el.getAttribute('data-selected') === 'true' ||
                [...el.classList].some(c=>/^(?:item-selected|selected|curr|active)$/.test(c)||/^_selected_|^highlighting-/.test(c)));
        }""")

    async def select_payment(self):
        await self.verify_payment()
        LOG.info("[%s] 保留京东结算页默认支付方式，不切换通道、银行卡或分期", self.s.task.id)

    async def verify_payment(self):
        self.s.scheduler.check_stop()
        if self.s.task.payment_method != "online":
            raise StopOrder("仅支持在线支付，请更新旧任务的支付方式")
        if not checkout_url(self.page.url):
            raise StopOrder("当前不是京东订单结算页，停止处理支付方式")
        # 站点负责默认在线通道。不可因看不到卡号/不分期控件而阻止创建订单。
        # 不展开明细、不点击支付控件，也不要求识别具体银行卡/方案的内部结构。
        offline = self.page.locator(".payment-channel [class^='modePaymentLi-'], .payment-item, .payment-list [data-value], [role=radio]").filter(has_text="货到付款")
        for index in range(await offline.count()):
            node = offline.nth(index)
            if await node.is_visible() and await self.payment_selected(node):
                raise StopOrder("当前页面选择了货到付款，请在京东改为在线支付后再运行")

    def password_fields(self):
        return self.page.locator(".payment-summary-password__input:visible input:visible")

    async def password_fields_complete(self):
        return await self.password_fields().evaluate_all("""inputs =>
            inputs.length===1 ? inputs[0].value.length===6 :
            inputs.length===6 && inputs.every(el=>el.value.length===1)""")

    async def password_error(self):
        errors = self.page.get_by_text(re.compile(r"支付密码(?:错误|不正确|已锁定)|密码输入错误|密码不正确|密码已锁定"))
        return any([await errors.nth(i).is_visible() for i in range(await errors.count())])

    async def fill_payment_password(self):
        if self.s.submitted:
            return False
        fields = self.password_fields()
        count = await fields.count()
        if not count or not self.s.payment_secret:
            return False
        if not checkout_url(self.page.url):
            raise StopOrder("支付密码输入框不在京东结算页，停止填写")
        await self.verify_payment()
        if await self.password_error():
            raise StopOrder("京东提示支付密码校验未通过，停止自动填写，请人工核实")
        if self.s.payment_secret_attempted:
            raise StopOrder("本轮任务已自动填写过支付密码，停止重复尝试，请人工核实")
        if count not in (1, 6):
            raise StopOrder("支付密码输入框结构未识别，停止填写")
        self.s.payment_secret_attempted = True
        try:
            value = self.s.payment_secret.value()
            if count == 1:
                await fields.fill(value)
            else:
                for index, digit in enumerate(value):
                    await fields.nth(index).fill(digit)
            if not await self.password_fields_complete():
                raise ValueError
        except Exception:
            # Playwright 异常可能附带 fill 的原文；不能让它进入日志或响应。
            try:
                for index in range(await fields.count()):
                    await fields.nth(index).fill("")
            except Exception:
                pass
            raise StopOrder("支付密码自动填写未完成，已停止任务，请在京东人工处理") from None
        if await self.password_error():
            raise StopOrder("京东提示支付密码校验未通过，停止任务，不会重复尝试")
        LOG.info("[%s] 已填写本次运行的支付密码，将重新核对结算信息", self.s.task.id)
        return True

    async def assert_submission_allowed(self, button, final=False):
        if not checkout_url(self.page.url):
            raise StopOrder("最终按钮不在京东订单结算页，停止提交")
        label = await button_text(button)
        await self.verify_payment()
        if re.search("支付|付款", label) and not await button.evaluate(
                "el => !!el.closest('.payment-action-submit, #order-submit, #submitOrder, [class^=submitOrder-]')"):
            raise StopOrder("付款文案按钮不在已识别的订单提交区域，已停止最终点击")
        if final and await self.password_fields().count():
            if await self.password_error():
                raise StopOrder("京东提示支付密码校验未通过，已停止最终点击")
            if not await self.password_fields_complete():
                raise StopOrder("当前结算页要求支付密码，请在管理页填写本次运行密码后重新启动，或在京东人工处理")

    async def verify_goods_identity(self, card, task=None, cart_selection=None):
        task = task if task is not None else self.s.task
        ids = await sku_ids(card)
        if ids:
            if ids != [task.sku]:
                raise StopOrder("结算 SKU 包含其他商品，已停止")
            return
        # 新版商品卡没有链接/SKU 属性：核对本轮购买入口证据和真实名称、规格。
        modern = await card.evaluate("el => el.classList.contains('sku-item')")
        name, expected_spec = None, None
        if task.mode == "direct":
            proof = self.s.product_selection
            if (proof and proof.get("sku") == task.sku and proof.get("quantity") == task.quantity
                    and proof.get("name") and proof.get("options")):
                name, expected_spec = proof["name"], "".join(proof["options"])
            if not modern or name is None:
                raise StopOrder("结算 SKU 不可直接读取，且缺少本轮详情页购买与规格核对证据，已停止")
        elif (modern and task.source == "cart" and
                (self.s.cart_selection if cart_selection is None else cart_selection) == {"sku": task.sku, "quantity": task.quantity} and task.cart_spec):
            name, expected_spec = task.name, task.cart_spec
        else:
            raise StopOrder("结算 SKU 不可直接读取，且缺少本轮购物车与规格核对证据，已停止")
        title = card.locator("[class^='title-'] [class^='ellipsis-'], [class^='title-'] div.text-ellipsis")
        spec = card.locator("[class^='sku-']")
        normalize = lambda text: re.sub(r"\s+", "", text)
        if (await title.count() != 1 or await spec.count() != 1 or
                normalize(await title.inner_text()) != normalize(name) or
                normalize(await spec.inner_text()) != normalize(expected_spec)):
            raise StopOrder("新版结算商品名称/规格与本轮购买证据不一致，已停止")

    async def optional_toggle(self, key, desired, label):
        checkbox = await self.find(key, required=False)
        if checkbox is None:
            labelled = self.page.get_by_label(re.compile(label))
            if await labelled.count() == 1 and await labelled.is_visible():
                checkbox = labelled
        if checkbox is None:
            hidden = self.page.locator(self.s.selectors[key])
            for index in range(await hidden.count()):
                item = hidden.nth(index)
                if await item.get_attribute("type") == "checkbox" and await item.is_checked() != desired:
                    raise StopOrder(f"{label}是隐藏的自定义组件，无法按配置设置，请更新适配")
            if desired:
                raise StopOrder(f"配置启用{label}，但页面找不到可用开关，请更新 {key} 选择器")
            return
        if (await checkbox.get_attribute("type") or "").lower() != "checkbox":
            raise StopOrder(f"{label}开关不是原生复选框，请按真实页面适配，避免不明确的扣减")
        if await checkbox.is_checked() != desired:
            await self.set_checkbox(checkbox, desired)

    async def discounts(self):
        discounts = self.s.task.discounts
        coupon_text = discounts["coupon_text"]
        if coupon_text:
            if not discounts["use_existing_coupons"]:
                raise StopOrder("coupon_text 与 use_existing_coupons=false 冲突")
            coupons = self.page.locator(self.s.selectors["coupon"]).filter(has_text=coupon_text)
            if await coupons.count() != 1:
                raise StopOrder("指定已有优惠券无法唯一匹配，请检查 coupon_text 和 coupon 选择器")
            coupon = coupons.first
            checked = await coupon.get_attribute("aria-checked") == "true" or "selected" in (await coupon.get_attribute("class") or "").split()
            if not checked:
                await self.activate(coupon)
                await self.page.wait_for_function("el => el.getAttribute('aria-checked') === 'true' || el.classList.contains('selected')",
                    arg=await coupon.element_handle(), timeout=self.s.cfg.jd["action_timeout_ms"], polling=50)
        elif not discounts["use_existing_coupons"]:
            # 没有通用的“禁用优惠券”按钮，不能静默忽略用户的禁用要求。
            raise StopOrder("禁用已有优惠券需按真实结算页适配；默认支持京东自动匹配的已有优惠券")
        await self.optional_toggle("beans", discounts["use_beans"], "京豆")
        await self.optional_toggle("red_packets", discounts["use_red_packets"], "红包")

    async def freight_not_itemized(self):
        """已核对的新版付款摘要可省略运费行；缺省值保持未知，不推算为零。"""
        if "freight" in self.s.cfg.selectors:
            return False
        return await self.page.evaluate("""() => {
            const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            const groups = [...document.querySelectorAll('.payment-summary > .payment-summary-inner')].filter(visible);
            const totals = [...document.querySelectorAll('.payment-action .priceInfo')].filter(visible);
            if (groups.length!==1 || totals.length!==1) return false;
            const rows = [...groups[0].querySelectorAll(':scope > .payment-summary-item')].filter(visible);
            const label = row => row.querySelector('.payment-summary-item__title em')?.innerText.trim() || '';
            const subtotal = rows.filter(row=>label(row)==='商品总额');
            if (subtotal.length!==1 || [...subtotal[0].querySelectorAll('.payment-summary-item__price')].filter(visible).length!==1) return false;
            return rows.every(row=>label(row) && !/运费|配送费|邮费/.test(label(row)));
        }""")

    async def amount(self, key, required=False):
        node = await self.find(key, required=False if key == "freight" else required)
        if node is None and key == "freight" and required:
            if await self.freight_not_itemized():
                return None
            node = await self.find(key, required=True)
        if node is None:
            return None
        text = (await node.inner_text()).strip()
        if key == "freight" and ("免运费" in text or text in ("免", "免费")):
            return Decimal("0")
        return money(text, allow_zero=True)

    async def validate_goods(self, goods):
        quantities = []
        for index in range(await goods.count()):
            card = goods.nth(index)
            if not await card.is_visible():
                continue
            await self.verify_goods_identity(card)
            quantity = await self.find("checkout_quantity", scope=card)
            quantities.append(quantity_text(await quantity.inner_text()))
        return quantities

    async def validate(self):
        await self.guard()
        button = await self.find("submit")
        await self.find("checkout_goods")
        goods = self.page.locator(self.s.selectors["checkout_goods"])
        quantities = await self.validate_goods(goods)
        if not quantities or sum(quantities) != self.s.task.quantity:
            raise StopOrder("结算商品数量与配置不一致")
        # 选择优惠后等待总额短暂稳定，避免读取上一轮计算值。
        total = await self.amount("total", required=True)
        for _ in range(5):
            await asyncio.sleep(0.08)
            current = await self.amount("total", required=True)
            if current == total:
                break
            total = current
        else:
            raise StopOrder("订单应付金额仍在变化，停止提交")
        if self.s.task.max_price is not None and total > self.s.task.max_price:
            raise StopOrder(f"最终应付 {total} 超过价格上限 {self.s.task.max_price}")
        subtotal = await self.amount("subtotal", required=self.s.task.require_breakdown)
        freight = await self.amount("freight", required=self.s.task.require_breakdown)
        discount_node = await self.find("discount", required=False)
        discount = (await discount_node.inner_text()).strip() if discount_node is not None else "页面未单列，以最终应付为准"
        if self.s.task.address_keyword:
            nodes = self.page.locator(self.s.selectors["address"])
            text = " ".join([await nodes.nth(i).inner_text() for i in range(await nodes.count()) if await nodes.nth(i).is_visible()])
            if self.s.task.address_keyword not in text:
                raise StopOrder("所选地址与 address_keyword 不匹配")
        LOG.info("[%s] 结算核对 SKU=%s 数量=%d 商品金额=%s 运费=%s 优惠=%s 最终应付=%s",
            self.s.task.id, self.s.task.sku, sum(quantities), subtotal,
            freight if freight is not None else "未单列（以最终应付为准）", discount, total)
        quote = Quote(total, subtotal, freight, discount)
        await self.assert_submission_allowed(button)
        LOG.info("[%s] 保留京东结算页默认支付方式，不切换通道、银行卡或分期", self.s.task.id)
        self.s.store.event(self.s.task.id, "validated", {"sku": self.s.task.sku, "quantity": sum(quantities),
            "subtotal": str(subtotal) if subtotal is not None else None,
            "freight": str(freight) if freight is not None else None,
            "freight_status": "itemized" if freight is not None else "not_itemized",
            "discount": discount, "total": str(total)})
        return quote
