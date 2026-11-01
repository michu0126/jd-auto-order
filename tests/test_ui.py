import asyncio
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

from core.config import CST
from core.config_store import catalog_read, catalog_write

ROOT = Path(__file__).resolve().parents[1]


class CartTaskUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.yaml"
        shutil.copy2(ROOT / "tests" / "fixtures" / "config.yaml", self.path)
        catalog_write(self.path.parent / "data", {"status": "ready", "count": 2, "complete": True,
            "message": "已读取 2 个购物车商品", "scanned_at": "2026-10-25T12:00:00+08:00", "warnings": [], "items": [
                {"sku_id": "111111111", "name": "模拟手机 Pro 256GB", "spec": "黑色 256GB", "quantity": 1,
                    "price_text": "¥1899.00", "image_url": "", "status": "购物车商品"},
                {"sku_id": "222222222", "name": "模拟无线耳机", "spec": "白色", "quantity": 1,
                    "price_text": "¥199.00", "image_url": "", "status": "预约/待开售"}]})
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--config", str(self.path), "web", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONUTF8="1"))
        for _ in range(100):
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=.5)
            try:
                client.request("GET", "/api/status")
                client.getresponse().read()
                break
            except OSError:
                await asyncio.sleep(.05)
            finally:
                client.close()
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page(viewport={"width": 1440, "height": 1100})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        await self.page.goto(self.base)
        await self.page.wait_for_function("document.querySelectorAll('.product').length===2")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()
        self.process.terminate()
        self.process.wait(timeout=5)
        self.temp.cleanup()

    async def config(self):
        return await (await self.page.request.get(self.base + "/api/config")).json()

    async def test_scan_error_distinguishes_last_success_and_new_failure_time(self):
        data_dir = self.path.parent / 'data'
        failed = catalog_read(data_dir) | {'status': 'error', 'complete': False,
            'message': '模拟定位冲突'}
        catalog_write(data_dir, failed)
        await self.page.evaluate('refresh()')
        meta = await self.page.locator('#cart-meta').inner_text()
        self.assertIn('本次扫描失败：模拟定位冲突', meta)
        self.assertIn('上次成功扫描 2026-10-25 12:00:00', meta)
        self.assertNotIn('失败时间', meta)
        self.assertTrue(await self.page.locator('.product button').first.is_disabled())
        catalog_write(data_dir, failed | {'failed_at': '2026-10-26T15:24:12+08:00'})
        await self.page.evaluate('refresh()')
        meta = await self.page.locator('#cart-meta').inner_text()
        self.assertIn('失败时间 2026-10-26 15:24:12', meta)
        self.assertIn('上次成功扫描 2026-10-25 12:00:00', meta)
        self.assertEqual(self.errors, [])

    async def test_taobao_size_can_be_blank_and_url_keeps_variant(self):
        await self.page.wait_for_function("document.querySelectorAll('.platform-card').length===3")
        self.assertEqual(await self.page.locator("#link-platform").input_value(), "taobao")
        self.assertEqual(await self.page.locator("#link-size").input_value(), "")
        self.assertIsNone(await self.page.locator("#link-size").get_attribute("required"))
        await self.page.locator("#link-url").fill("https://detail.tmall.com/item.htm?id=1094601672942&skuId=6313432066497")
        await self.page.locator("#link-name").fill("模拟天猫眼镜")
        await self.page.get_by_role("button", name="保存平台任务").click()
        await self.page.wait_for_function("document.getElementById('message').innerText.includes('平台任务已保存')")
        task = next(t for t in (await self.config())["tasks"] if t["platform"] == "taobao")
        self.assertEqual(task["sku_options"], {})
        self.assertEqual(task["product_id"], "1094601672942")
        self.assertEqual(task["variant_id"], "6313432066497")
        self.assertFalse(task["enabled"])
        await self.page.locator(".task-card").filter(has_text="模拟天猫眼镜").get_by_role("button", name="编辑", exact=True).click()
        self.assertEqual(await self.page.locator("#link-size").input_value(), "")

    async def test_platform_link_form_shows_blockers_and_saves_daily_m_task(self):
        await self.page.wait_for_function("document.querySelectorAll('.platform-card').length===3")
        self.assertTrue(await self.page.locator("#link-enabled").is_disabled())
        await self.page.locator("#link-platform").select_option("douyin")
        self.assertIn("App", await self.page.locator("#link-adapter-note").inner_text())
        await self.page.locator("#link-url").fill("分享 https://v.douyin.com/local-fixture/ 商品")
        await self.page.locator("#link-name").fill("模拟眼镜")
        await self.page.get_by_role("button", name="保存平台任务").click()
        await self.page.wait_for_function("document.getElementById('message').innerText.includes('平台任务已保存')")
        tasks = (await self.config())["tasks"]
        item = next(task for task in tasks if task["platform"] == "douyin")
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(item["sku_options"], {"尺码": "M"})
        self.assertEqual(item["recurrence"], "daily")
        self.assertIn("T10:00:00", item["target_time"])
        self.assertFalse(item["enabled"])
        self.assertFalse(item["adapter"]["ready"])
        card = self.page.locator(".task-card").filter(has_text="抖音 · 模拟眼镜")
        self.assertTrue(await card.get_by_role("button", name="启动", exact=True).is_disabled())
        await card.get_by_role("button", name="编辑", exact=True).click()
        self.assertEqual(await self.page.locator("#link-task-id").input_value(), item["id"])
        self.assertFalse(self.errors)

    async def test_create_edit_delete_task_from_cart_form(self):
        await self.page.get_by_role("button", name="添加任务").first.click()
        # 非零毫秒避免 Chrome 在秒为 00 时把 datetime-local 规范化为仅小时/分钟。
        target = (datetime.now(CST) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.123")
        await self.page.locator("#target-time").fill(target)
        await self.page.locator("#quantity").fill("2")
        self.assertEqual(await self.page.locator("#max-price").count(), 0)
        self.assertTrue(await self.page.locator("#payment-method").is_disabled())
        self.assertEqual(await self.page.locator("#payment-method option").evaluate_all("nodes=>nodes.map(n=>n.value)"), ["online"])
        self.assertNotIn("货到付款", await self.page.locator("#task-form").inner_text())
        await self.page.locator("#auto-submit").uncheck()
        await self.page.get_by_role("button", name="保存抢购任务").click()
        await self.page.wait_for_function("document.getElementById('message').innerText.includes('已保存')")
        tasks = (await self.config())["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["payment_method"], "online")
        self.assertEqual(tasks[0]["quantity"], 2)
        self.assertEqual(tasks[0]["target_time"], target)
        self.assertFalse(tasks[0]["auto_submit"])
        self.assertEqual(tasks[0]["source"], "cart")
        self.assertIsNone(tasks[0]["max_price"])
        await self.page.get_by_role("button", name="编辑", exact=True).click()
        self.assertEqual(await self.page.locator("#payment-method").input_value(), "online")
        await self.page.get_by_role("button", name="保存抢购任务").click()
        await self.page.wait_for_function("document.querySelector('.task-detail').innerText.includes('在线支付')")
        self.assertEqual((await self.config())["tasks"][0]["payment_method"], "online")
        folder = ROOT / "data" / "qa"
        folder.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(folder / "cart-task-desktop.png"), full_page=True)
        await self.page.get_by_role("button", name="删除", exact=True).click()
        await self.page.wait_for_function("document.getElementById('configured-tasks').innerText.includes('还没有抢购任务')")
        self.assertEqual((await self.config())["tasks"], [])
        state = await (await self.page.request.get(self.base + "/api/status")).json()
        self.assertFalse(state["running"])
        self.assertEqual(state["orders"], [])
        self.assertFalse(self.errors)

    async def test_product_link_form_creates_and_edits_without_cart_scan(self):
        catalog_write(self.path.parent / 'data', {'status': 'error', 'items': [], 'message': '未扫描购物车'})
        await self.page.evaluate('refresh()')
        await self.page.locator('#new-product-task').click()
        self.assertTrue(await self.page.locator('#product-entry').is_visible())
        self.assertTrue(await self.page.locator('#save-task').is_disabled())
        await self.page.locator('#product-url').fill('https://item.jd.com/333333333.html?from=share')
        target = (datetime.now(CST) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.123')
        await self.page.locator('#target-time').fill(target)
        self.assertEqual(await self.page.locator('#purchase-mode').input_value(), 'direct')
        self.assertTrue(await self.page.locator('#purchase-mode').is_disabled())
        await self.page.locator('#save-task').click()
        await self.page.wait_for_function("document.getElementById('message').textContent.includes('已保存')")
        task = (await self.config())['tasks'][0]
        self.assertEqual(task['sku_id'], '333333333')
        self.assertEqual(task['source'], 'manual')
        self.assertEqual(task['purchase_mode'], 'direct')
        self.assertEqual(task['target_time'], target)
        self.assertEqual(task['payment_method'], 'online')
        self.assertIsNone(task['max_price'])
        await self.page.get_by_role('button', name='编辑', exact=True).click()
        self.assertEqual(await self.page.locator('#task-source').input_value(), 'manual')
        self.assertEqual(await self.page.locator('#product-url').input_value(), task['product_url'])
        await self.page.locator('#product-name').fill('详情页定时任务')
        await self.page.locator('#quantity').fill('2')
        await self.page.locator('#save-task').click()
        await self.page.wait_for_function("document.getElementById('configured-tasks').textContent.includes('详情页定时任务')")
        task = (await self.config())['tasks'][0]
        self.assertEqual(task['quantity'], 2)
        self.assertEqual(task['purchase_mode'], 'direct')
        status = await (await self.page.request.get(self.base + '/api/status')).json()
        self.assertFalse(status['running'])
        self.assertEqual(status['orders'], [])
        folder = ROOT / 'data' / 'qa'
        folder.mkdir(parents=True, exist_ok=True)
        await self.page.get_by_role('button', name='编辑', exact=True).click()
        await self.page.locator('.editor').screenshot(path=str(folder / 'product-link-task-desktop-20261030.png'))
        await self.page.set_viewport_size({'width': 390, 'height': 844})
        await self.page.get_by_role('button', name='编辑', exact=True).click()
        self.assertTrue(await self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        await self.page.screenshot(path=str(folder / 'product-link-task-mobile-20261030.png'), full_page=True)
        self.assertFalse(self.errors)

    async def test_old_backend_product_link_entry_requires_restart(self):
        old = await self.config()
        old.pop('task_sources', None)
        await self.page.route('**/api/config', lambda route: route.fulfill(json=old))
        await self.page.evaluate('load()')
        self.assertTrue(await self.page.locator('#new-product-task').is_disabled())
        self.assertIn('start.bat', await self.page.locator('#task-source-note').inner_text())
        self.assertFalse(self.errors)

    async def test_mobile_form_and_search_without_horizontal_overflow(self):
        await self.page.set_viewport_size({"width": 390, "height": 844})
        await self.page.locator("#cart-search").fill("耳机")
        self.assertEqual(await self.page.locator(".product").count(), 1)
        await self.page.get_by_role("button", name="添加任务").click()
        self.assertEqual(await self.page.locator("#sku-id").input_value(), "222222222")
        self.assertEqual(await self.page.locator("#purchase-mode").input_value(), "cart")
        self.assertFalse(await self.page.evaluate("document.documentElement.scrollWidth>innerWidth"))
        folder = ROOT / "data" / "qa"
        folder.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(folder / "cart-task-mobile.png"), full_page=True)
        self.assertFalse(self.errors)

    async def test_runtime_password_is_sent_only_on_run_and_cleared(self):
        sent = []
        async def capture(route):
            sent.append(route.request.post_data_json)
            await route.fulfill(json={"message": "模拟启动成功"})
        await self.page.route("**/api/action", capture)
        field = self.page.locator("#payment-password")
        self.assertEqual(await field.get_attribute("type"), "password")
        await field.fill("654321")
        await self.page.locator('[data-action="dry-run"]').click()
        await self.page.wait_for_function("document.getElementById('message').textContent==='模拟启动成功'")
        self.assertNotIn("payment_password", sent[-1])
        self.assertEqual(await field.input_value(), "654321")
        await self.page.locator('[data-action="run"]').click()
        await self.page.wait_for_function("document.getElementById('payment-password').value===''")
        self.assertEqual(sent[-1]["payment_password"], "654321")
        self.assertNotIn("654321", str(await self.config()))
        self.assertEqual(await self.page.evaluate("localStorage.length+sessionStorage.length"), 0)
        self.assertFalse(self.errors)

    async def test_existing_browser_mode_can_be_saved_without_starting_tasks(self):
        before = await self.config()
        await self.page.locator('#browser-connection').select_option('existing')
        await self.page.get_by_role('button', name='保存浏览器方式', exact=True).click()
        await self.page.wait_for_function("document.getElementById('message').textContent.includes('浏览器方式已保存')")
        after = await self.config()
        self.assertEqual(after['browser']['browser_connection'], 'existing')
        self.assertEqual(before['tasks'], after['tasks'])
        self.assertEqual(await self.page.get_by_role('button', name='检测浏览器连接').count(), 1)
        status = await (await self.page.request.get(self.base + '/api/status')).json()
        self.assertFalse(status['running'])
        self.assertFalse(self.errors)

    async def test_old_backend_shows_browser_restart_notice(self):
        old = await self.config()
        old.pop('browser', None)
        await self.page.route('**/api/config', lambda route: route.fulfill(json=old))
        await self.page.locator('.advanced summary').click()
        await self.page.locator('#reload').click()
        await self.page.wait_for_function("document.getElementById('browser-connection-note').textContent.includes('重新运行 start.bat')")
        self.assertTrue(await self.page.locator('#save-browser').is_disabled())
        self.assertTrue(await self.page.locator('[data-action="browser-check"]').is_disabled())
        self.assertFalse(self.errors)
