"""购物车只读扫描：不点击勾选框、数量、删除或结算按钮。"""
import asyncio
import logging
import re
import time
from datetime import datetime

from core.config import CST
from core.config_store import catalog_write
from core.errors import Retryable, StopOrder
from jd.common import PageTools

LOG = logging.getLogger(__name__)
EXTRACT = r'''selectors => {
    const visible = e => !!e && e.getClientRects().length > 0;
    const text = e => (e?.innerText || '').trim().replace(/\s+/g,' ');
    const rows = [...document.querySelectorAll(selectors.cart_row)].filter(visible);
    const items = []; let skipped = 0;
    for (const row of rows) {
        const main = row.querySelectorAll('a[class*="_goodTitle_"], .p-name a[href], a[data-cart-title]');
        const links = main.length ? [...main] : [...row.querySelectorAll('a[href]')];
        const link = links.find(a => /item\.jd\.com\/\d+\.html/.test(a.href) && text(a));
        const id = link?.href.match(/item\.jd\.com\/(\d+)\.html/)?.[1] || row.getAttribute('data-sku');
        const name = text(link) || row.getAttribute('data-name') || '';
        if (!/^\d+$/.test(id||'') || !name) { skipped++; continue; }
        const quantityInput = row.querySelector(selectors.cart_quantity);
        const quantity = Number(quantityInput?.value);
        const checkbox = row.querySelector(selectors.cart_checkbox);
        const specNode = row.querySelector('.p-props, [class*="_extra-select_"] [class*="_txt_"], [class*="_sku-info_"], [class*="_skuInfo_"], [class*="_goodType_"], [class*="_productSku_"]');
        const priceNode = row.querySelector('.p-price strong, .p-price, [class*="_price-show_"], [class*="_price_"]');
        const rowText = text(row);
        const money = rowText.match(/[¥￥]\s*([\d,]+(?:\.\d{1,2})?)/);
        const image = row.querySelector('img');
        let imageUrl = image?.currentSrc || image?.getAttribute('src') || '';
        try {const u=new URL(imageUrl,location.href); if(!/(^|\.)(360buyimg\.com|jd\.com)$/.test(u.hostname))imageUrl=''; else imageUrl=u.href;} catch{imageUrl='';}
        const status = /无货|缺货|售罄|已抢光|已售完/.test(rowText) ? '无货/售罄' :
            /待发布|预约|即将开售|未开始/.test(rowText) ? '预约/待开售' : checkbox?.disabled ? '当前不可勾选' : '购物车商品';
        items.push({sku_id:id,name,product_url:`https://item.jd.com/${id}.html`,
            spec:text(specNode), quantity:Number.isInteger(quantity)&&quantity>0?quantity:1,
            quantity_known:Number.isInteger(quantity)&&quantity>0,price_text:text(priceNode)||(money?'¥'+money[1]:''),
            image_url:imageUrl,status,selected:!!checkbox?.checked,
            row_key:row.getAttribute('data-skuuuid')||id});
    }
    const heading = [...document.querySelectorAll('h1,h2,[class*="_cart-title_"],[class*="_cartTitle_"],[class*="_title_"]')]
        .map(text).find(t=>/^购物车\s*[（(]\d+[）)]$/.test(t));
    const expected = heading ? Number(heading.match(/\d+/)[0]) : null;
    const next = [...document.querySelectorAll('a,button')].some(e=>visible(e)&&/^下一页$/.test(text(e))&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');
    return {items,skipped,expected_count:expected,has_next_page:next,
        empty:/购物车(?:还是|是)?空(?:空)?的|购物车中没有商品|快去挑选/.test(document.body.innerText),
        layout:document.querySelector('[data-skuuuid]')?'jd-pc-current':'jd-pc-classic'};
}'''


async def extract(page, selectors):
    return await page.evaluate(EXTRACT, selectors)


class CartScanner(PageTools):
    async def scan(self):
        await self.s.login.ensure()
        await self.goto("https://cart.jd.com/cart_index")
        deadline = time.monotonic() + 30
        result = None
        # 等待真实商品行完成渲染，而不是把骨架屏当成空购物车。
        while time.monotonic() < deadline:
            self.s.scheduler.check_stop()
            await self.guard()
            result = await extract(self.page, self.s.selectors)
            if result["items"] or result["empty"]:
                break
            await asyncio.sleep(.3)
        if not result or (not result["items"] and not result["empty"]):
            raise Retryable("购物车商品尚未加载或页面结构未识别，没有把它当成空购物车")
        collected = {}
        stable = 0
        previous_count = -1
        for _ in range(20):
            self.s.scheduler.check_stop()
            result = await extract(self.page, self.s.selectors)
            for item in result["items"]:
                collected[item["row_key"]] = item
            metrics = await self.page.evaluate("""() => {
                const e=document.scrollingElement; return {y:e.scrollTop,height:e.scrollHeight,view:innerHeight};
            }""")
            at_bottom = metrics["y"] + metrics["view"] >= metrics["height"] - 4
            stable = stable + 1 if len(collected) == previous_count and at_bottom else 0
            previous_count = len(collected)
            if stable >= 2:
                break
            await self.page.evaluate("window.scrollBy(0,Math.max(500,innerHeight*.85))")
            await asyncio.sleep(.25)
        else:
            stable = 0
        await self.page.evaluate("window.scrollTo(0,0)")
        items = list(collected.values())
        seen = set()
        unique = []
        for item in items:
            if item["sku_id"] not in seen:
                unique.append(item)
                seen.add(item["sku_id"])
        warnings = []
        if result["skipped"]:
            warnings.append(f"{result['skipped']} 个商品行无法识别 SKU/名称")
        if result["has_next_page"]:
            warnings.append("检测到下一页，当前扫描只包含已加载的购物车页面")
        if result["expected_count"] is not None and result["expected_count"] != len(items):
            warnings.append(f"页面显示 {result['expected_count']} 个商品，当前读取 {len(items)} 行")
        if stable < 2:
            warnings.append("滚动扫描达到上限，可能仍有未加载商品")
        if len(unique) != len(items):
            warnings.append("相同 SKU 的多个购物车行已合并显示，请核对规格和数量")
        report = {"status": "ready", "items": unique, "count": len(unique),
            "scanned_at": datetime.now(CST).isoformat(timespec="seconds"), "warnings": warnings,
            "complete": not warnings, "layout": result["layout"],
            "message": f"已读取 {len(unique)} 个购物车商品" + ("；请留意扫描提示" if warnings else "")}
        catalog_write(self.s.cfg.data_dir, report)
        self.s.store.event(self.s.task.id, "cart_scanned", {"count": len(unique), "warnings": warnings})
        LOG.info("购物车扫描完成：%s", report["message"])
        return report
