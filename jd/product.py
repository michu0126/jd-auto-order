import logging
import re
from urllib.parse import urlparse

from core.errors import Retryable, StopOrder
from jd.common import PageTools

LOG = logging.getLogger(__name__)


def stock_state(text, enabled=True):
    if re.search(r"无货|缺货|售罄|已抢光|已售完|到货通知", text):
        return "out_of_stock"
    if re.search(r"未开始|即将开始|即将开售|等待开抢", text) or not enabled:
        return "not_started"
    return "available" if re.search(r"有货|立即购买|立即抢购|加入购物车|抢购", text) else "unknown"


class Product(PageTools):
    async def open(self):
        from jd.embedded_checkout import FrameSurface
        if isinstance(self.s.page, FrameSurface):
            from jd.checkout import Checkout
            from jd.order import Order
            self.s.page = self.s.page.owner
            self.s.selectors = self.s.base_selectors.copy()
            self.s.checkout, self.s.order = Checkout(self.s), Order(self.s)
        self.s.product_selection = None
        self.s.product_prepared = False
        await self.page.goto(self.s.task.url, wait_until="commit", timeout=20000)
        await self.guard()
        return await self.read()

    async def read(self):
        node = await self.find("product_name", check_page=True)
        name = (await node.inner_text()).strip()
        actual = await self.page.evaluate("""() => String(window.pageConfig?.product?.skuid || window.pageConfig?.product?.skuId || '')""")
        match = re.fullmatch(r"/(\d+)\.html", urlparse(self.page.url).path)
        url_sku = match[1] if match else None
        if (actual and actual != self.s.task.sku) or url_sku != self.s.task.sku:
            raise StopOrder("商品页 SKU 与配置不一致，可能规格点击后跳到了其他 SKU")
        if not name:
            raise Retryable("商品名称尚未加载")
        info = {"sku": actual or url_sku, "name": name}
        LOG.info("[%s] 商品：%s SKU=%s", self.s.task.id, name, info["sku"])
        return info

    async def specs(self):
        task = self.s.task
        for label, wanted in task.sku_options.items():
            groups = self.page.locator(self.s.selectors["spec_group"])
            matches = []
            for index in range(await groups.count()):
                group = groups.nth(index)
                names = group.locator(self.s.selectors["spec_label"])
                if await names.count() and label in (await names.first.inner_text()):
                    matches.append(group)
            if len(matches) != 1:
                raise StopOrder(f"无法唯一定位规格组 {label!r}，请更新 spec_group/spec_label")
            group = matches[0]
            items = group.locator(self.s.selectors["spec_item"])
            targets = []
            for index in range(await items.count()):
                node = items.nth(index)
                text = " ".join((await node.inner_text()).split())
                if text == wanted or await node.get_attribute("data-value") == wanted:
                    targets.append(node)
            if len(targets) != 1:
                raise StopOrder(f"规格 {label}={wanted} 不存在或不唯一")
            classes = (await targets[0].get_attribute("class") or "").split()
            if not any(c == "selected" or c.endswith("--selected") for c in classes) and await targets[0].get_attribute("aria-checked") != "true":
                await self.click_transition(targets[0])
            # 页面可能导航，重新定位该组选中项。
            selected = self.page.locator(self.s.selectors["spec_group"]).filter(
                has=self.page.locator(self.s.selectors["spec_label"]).filter(has_text=label)).locator(self.s.selectors["spec_selected"])
            selected_texts = [" ".join((await selected.nth(i).inner_text()).split()) for i in range(await selected.count())]
            if selected_texts != [wanted]:
                raise StopOrder(f"规格 {label}={wanted} 没有选中")
        await self.read()
        quantity = await self.find("product_quantity", required=False)
        if quantity is not None:
            if await quantity.input_value() != str(task.quantity):
                await quantity.fill(str(task.quantity))
                await quantity.press("Tab")
            if await quantity.input_value() != str(task.quantity):
                raise StopOrder("商品购买数量未生效")
        elif task.quantity != 1:
            raise StopOrder("无法设置商品购买数量")
        self.s.product_prepared = True

    async def state(self):
        stock = await self.find("stock", required=False)
        button = await self.find("buy_now" if self.s.task.mode == "direct" else "add_cart", required=False)
        text = await stock.inner_text() if stock is not None else ""
        enabled = True
        if button is not None:
            text += " " + await button.inner_text()
            enabled = await button.is_enabled() and await button.get_attribute("aria-disabled") != "true" and not re.search(
                r"(?:^|[\s_-])disabled(?:\s|$)", await button.get_attribute("class") or "")
        # 只读取当前选中规格的缺货标记，不能被其他尺寸的“无货”污染。
        selected = self.page.locator(self.s.selectors["spec_group"]).locator(
            self.s.selectors["spec_selected"]).filter(visible=True)
        selected_lack = await selected.evaluate_all("els => els.some(e => e.classList.contains('specification-item-sku--lack'))")
        status = "out_of_stock" if selected_lack else stock_state(text, enabled)
        if button is None and status not in ("out_of_stock", "not_started"):
            status = "unknown"
        LOG.info("[%s] 购买状态=%s 文本=%s", self.s.task.id, status, text.strip()[:100])
        self.s.store.event(self.s.task.id, "stock", {"status": status, "text": text.strip()[:100]})
        return status

    async def buy(self, fresh=False):
        self.s.product_selection = None
        if fresh:
            await self.open()
            await self.specs()
        await self.guard()
        status = await self.state()
        if status in ("out_of_stock", "not_started"):
            raise Retryable("商品无货或尚未开售")
        if status == "unknown":
            raise Retryable("未识别到可购买入口或购买状态，不能据此认定无货；请检查详情页定位")
        key = "buy_now" if self.s.task.mode == "direct" else "add_cart"
        button = await self.find(key)
        proof = None
        if self.s.task.mode == "direct":
            info = await self.read()
            quantity = await self.find("product_quantity", required=False)
            if quantity is not None:
                if await quantity.input_value() != str(self.s.task.quantity):
                    raise StopOrder("商品页数量与任务不一致，停止购买")
            elif self.s.task.quantity != 1:
                raise StopOrder("无法核对商品页购买数量")
            selected = self.page.locator(self.s.selectors["spec_group"]).locator(
                self.s.selectors["spec_selected"]).filter(visible=True)
            # 系列品是跨商品系列入口，不一定出现在结算规格内；保留实际 SKU 规格。
            if "spec_selected" not in self.s.cfg.selectors:
                selected = selected.filter(has_not=self.page.locator(".specification-series-item-text"))
            options = [text.strip() for text in await selected.all_inner_texts() if text.strip()]
            proof = {"sku": info["sku"], "name": info["name"], "quantity": self.s.task.quantity,
                "options": options}
        await self.click_transition(button, trigger=self.s.task.mode == "direct")
        # 只保存本轮已实际点击详情页购买入口的证据，普通只读检查不生成它。
        self.s.product_selection = proof
