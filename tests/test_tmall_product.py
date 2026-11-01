"""真实 Chrome + 本地 DOM 响应；没有访问淘宝服务或提交订单。"""
import unittest
from types import SimpleNamespace

from playwright.async_api import async_playwright

from core.errors import LoginRequired, Retryable, StopOrder
from marketplaces.taobao_product import prepare_tmall_product


URL = "https://detail.tmall.com/item.htm?id=1094601672942&skuId=6313432066497"


class TmallProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(500)
        self.task = SimpleNamespace(sku="1094601672942", variant_id="6313432066497",
                                    name="测试限定版", quantity=1, sku_options={})
        self.quantity = "1"

    async def load(self, *, presale=False, sizes=False, authenticated=True, clamp=False, ambiguous=False):
        options = '''<div class="skuItem--example"><label class="ItemLabel--example">商品规格</label>
        <div data-vid="-3" class="valueItem--example isSelected--example" data-disabled="false">限定版</div></div>'''
        if sizes:
            options += '''<div class="skuItem--size"><label class="ItemLabel--size">尺码</label>
            <button data-vid="1" class="valueItem--size isSelected--size" data-disabled="false">S</button>
            <button data-vid="2" class="valueItem--size" data-disabled="false" onclick="
            this.parentElement.querySelectorAll('[data-vid]').forEach(el=>el.className='valueItem--size');
            this.className+=' isSelected--size'">M</button></div>'''
        if ambiguous:
            options += '<div class="skuItem--example"><label class="ItemLabel--example">商品规格</label><div data-vid="9" class="valueItem--example isSelected--example">其他版</div></div>'
        button = "加入购物车 即将开始，10.26 10:00开售" if presale else "加入购物车"
        auth = '<span class="site-nav-login-info-nick">模拟用户</span>' if authenticated else ''
        onblur = 'onblur="this.value=1"' if clamp else ''
        html = f'''<title>测试限定版-tmall.com天猫</title>{auth}
        <section id="SkuPanel_tbpcDetail_ssr2025"><div id="skuOptionsArea">{options}</div>
        <input class="countValue--example" value="{self.quantity}" {onblur}>
        <footer id="tbpcDetail_SkuPanelFoot"><button onclick="document.body.dataset.bought='yes'">{button}</button></footer></section>'''
        async def route_response(route):
            if route.request.url == URL:
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            else:
                await route.abort()
        await self.page.route("**/*", route_response)
        await self.page.goto(URL)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_presale_is_retryable_even_when_button_is_enabled(self):
        await self.load(presale=True)
        with self.assertRaisesRegex(Retryable, "尚未开售"):
            await prepare_tmall_product(self.page, self.task)
        self.assertIsNone(await self.page.locator("body").get_attribute("data-bought"))

    async def test_missing_size_is_not_silently_assumed(self):
        await self.load()
        self.task.sku_options = {"尺码": "M"}
        with self.assertRaisesRegex(StopOrder, "页面没有该规格"):
            await prepare_tmall_product(self.page, self.task)

    async def test_selects_present_size_and_verifies_quantity_without_buying(self):
        await self.load(sizes=True)
        self.task.sku_options = {"尺码": "M"}
        self.task.quantity = 2
        report = await prepare_tmall_product(self.page, self.task)
        self.assertEqual(report["quantity"], "2")
        self.assertEqual([o["text"] for o in report["options"] if o["group"] == "尺码" and o["selected"]], ["M"])
        self.assertFalse(report["variant_verified"])
        self.assertIsNone(await self.page.locator("body").get_attribute("data-bought"))

    async def test_quantity_reverted_by_page_blocks(self):
        await self.load(clamp=True)
        self.task.quantity = 2
        with self.assertRaisesRegex(StopOrder, "数量未按任务生效"):
            await prepare_tmall_product(self.page, self.task)

    async def test_login_loss_requires_normal_login(self):
        await self.load(authenticated=False)
        with self.assertRaises(LoginRequired):
            await prepare_tmall_product(self.page, self.task)

    async def test_wrong_requested_variant_blocks(self):
        await self.load()
        self.task.variant_id = "6313432066498"
        with self.assertRaisesRegex(StopOrder, "链接 SKU"):
            await prepare_tmall_product(self.page, self.task)

    async def test_multiple_selected_variants_in_same_group_block(self):
        await self.load(ambiguous=True)
        with self.assertRaisesRegex(StopOrder, "尚未唯一选中"):
            await prepare_tmall_product(self.page, self.task)
