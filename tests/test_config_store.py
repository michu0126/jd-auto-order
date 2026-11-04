import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.config import CST, Config
from core.config_store import catalog_failure, catalog_read, catalog_write, delete_task, raw_config, upsert_task
from core.errors import StopOrder

ROOT = Path(__file__).resolve().parents[1]


class TaskEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.yaml"
        shutil.copy2(ROOT / "tests" / "fixtures" / "config.yaml", self.path)
        catalog_write(self.path.parent / "data", {"status": "ready", "items": [
            {"sku_id": "111111111", "name": "模拟手机", "spec": "黑色 256GB", "quantity": 1},
            {"sku_id": "222222222", "name": "模拟耳机", "spec": "白色", "quantity": 1}]})
        self.form = {"sku_id": "111111111", "quantity": 2, "max_price": "1999.00",
            "target_time": (datetime.now(CST) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            "payment_method": "online", "purchase_mode": "cart", "auto_submit": True, "enabled": True}

    def tearDown(self):
        self.temp.cleanup()

    def test_create_from_cart_replaces_only_example(self):
        result = upsert_task(self.path, self.form)
        cfg = Config.load(self.path)
        self.assertEqual(len(cfg.tasks), 1)
        task = cfg.tasks[0]
        self.assertEqual(task.sku, "111111111")
        self.assertEqual(task.payment_method, "online")
        self.assertEqual(task.cart_spec, "黑色 256GB")
        self.assertEqual(task.source, "cart")
        self.assertTrue(self.path.with_suffix(".backup.yaml").exists())
        self.assertFalse((cfg.data_dir / "state.sqlite3").exists())

    def test_auto_pay_must_be_explicit_and_is_preserved_when_editing(self):
        task = upsert_task(self.path, self.form)
        self.assertFalse(task['auto_pay'])
        saved = upsert_task(self.path, self.form | {'id': task['id'], 'auto_pay': True})
        self.assertTrue(saved['auto_pay'])
        again = upsert_task(self.path, self.form | {'id': task['id']})
        self.assertTrue(again['auto_pay'])
        self.assertTrue(Config.load(self.path).tasks[0].auto_pay)

    def test_edit_and_delete_task_allow_empty_configuration(self):
        result = upsert_task(self.path, self.form)
        updated = upsert_task(self.path, self.form | {"id": result["id"], "payment_method": "online", "enabled": False})
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["payment_method"], "online")
        delete_task(self.path, result["id"])
        cfg = Config.load(self.path)
        self.assertEqual(cfg.tasks, [])
        self.assertEqual(cfg.context_task().id, "session")

    def test_unknown_sku_rejected_without_modifying_config(self):
        before = self.path.read_bytes()
        with self.assertRaises(StopOrder):
            upsert_task(self.path, self.form | {"sku_id": "999999999"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_scan_keeps_last_success_timestamp_and_blocks_old_items(self):
        data_dir = self.path.parent / 'data'
        previous = catalog_read(data_dir) | {'scanned_at': '2026-10-25T23:45:33+08:00'}
        catalog_write(data_dir, previous)
        failed = catalog_failure(data_dir, '模拟读取失败')
        self.assertEqual(failed['status'], 'error')
        self.assertFalse(failed['complete'])
        self.assertEqual(failed['scanned_at'], previous['scanned_at'])
        self.assertEqual(failed['items'], previous['items'])
        self.assertEqual(datetime.fromisoformat(failed['failed_at']).tzinfo, CST)
        with self.assertRaises(StopOrder):
            upsert_task(self.path, self.form)

    def test_form_without_price_cap_clears_inherited_and_existing_cap(self):
        form = dict(self.form)
        form.pop("max_price")
        result = upsert_task(self.path, form)
        self.assertIsNone(result["max_price"])
        self.assertIsNone(Config.load(self.path).tasks[0].max_price)
        limited = upsert_task(self.path, self.form | {"id": result["id"]})
        self.assertEqual(limited["max_price"], "1999.00")
        cleared = upsert_task(self.path, form | {"id": result["id"]})
        self.assertIsNone(cleared["max_price"])
        self.assertIsNone(Config.load(self.path).tasks[0].max_price)

    def test_invalid_time_price_payment_and_quantity_are_rejected(self):
        for patch in ({"target_time": "2000-01-01 00:00:00"}, {"max_price": "abc"},
                      {"payment_method": "wechat"}, {"payment_method": "cod"}, {"quantity": 0}):
            before = self.path.read_bytes()
            with self.subTest(patch=patch), self.assertRaises(StopOrder):
                upsert_task(self.path, self.form | patch)
            self.assertEqual(self.path.read_bytes(), before)

    def test_multiple_times_for_same_sku_get_unique_ids(self):
        one = upsert_task(self.path, self.form)
        two = upsert_task(self.path, self.form)
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(len(Config.load(self.path).tasks), 2)

    def test_real_existing_task_is_preserved(self):
        data = raw_config(self.path)
        data["purchase"]["sku_id"] = "333333333"
        data["purchase"]["product_url"] = "https://item.jd.com/333333333.html"
        data["purchase"]["name"] = "已有真实任务"
        from core.config_store import save_config
        save_config(self.path, data)
        upsert_task(self.path, self.form)
        self.assertEqual({t.sku for t in Config.load(self.path).tasks}, {"111111111", "333333333"})

    def test_failed_catalog_is_not_used_for_new_task(self):
        catalog_write(self.path.parent / "data", {"status": "error", "items": [{"sku_id": "111111111", "name": "旧数据"}]})
        with self.assertRaises(StopOrder):
            upsert_task(self.path, self.form)

    def product_form(self, **extra):
        return {"source": "manual", "product_url": "https://item.jd.com/333333333.html?from=share",
            "target_time": self.form["target_time"], "quantity": 1} | extra

    def test_product_link_needs_no_cart_scan_or_name(self):
        (self.path.parent / 'data' / 'cart.json').unlink()
        result = upsert_task(self.path, self.product_form())
        self.assertEqual(result['sku_id'], '333333333')
        self.assertEqual(result['product_url'], 'https://item.jd.com/333333333.html')
        self.assertEqual(result['name'], '京东商品 333333333')
        self.assertEqual(result['purchase_mode'], 'direct')
        self.assertEqual(result['source'], 'manual')
        self.assertEqual(result['cart_spec'], '')
        self.assertEqual(result['payment_method'], 'online')
        self.assertIsNone(result['max_price'])
        self.assertEqual(len(Config.load(self.path).tasks), 1)
        self.assertFalse((self.path.parent / 'data' / 'state.sqlite3').exists())

    def test_product_link_preserves_cart_tasks_and_has_unique_id(self):
        cart = upsert_task(self.path, self.form)
        first = upsert_task(self.path, self.product_form(name='详情页抢购'))
        second = upsert_task(self.path, self.product_form())
        self.assertNotEqual(first['id'], second['id'])
        tasks = {t.id: task for t, task in zip(Config.load(self.path).tasks, raw_config(self.path)['tasks'])}
        self.assertEqual(tasks[cart['id']]['source'], 'cart')
        self.assertEqual(tasks[first['id']]['source'], 'manual')
        self.assertEqual(len(tasks), 3)

    def test_product_link_edit_does_not_depend_on_cart_catalog(self):
        first = upsert_task(self.path, self.product_form(name='我的商品'))
        catalog_write(self.path.parent / 'data', {'status': 'error', 'items': []})
        edited = upsert_task(self.path, self.product_form(id=first['id'], quantity=2, auto_submit=False))
        self.assertEqual(edited['quantity'], 2)
        self.assertFalse(edited['auto_submit'])
        self.assertEqual(edited['name'], '我的商品')
        delete_task(self.path, first['id'])
        self.assertEqual(Config.load(self.path).tasks, [])

    def test_product_link_rejects_bad_url_and_conflicting_sku_atomically(self):
        for patch in ({'product_url': 'https://item.jd.com.evil.test/333333333.html'},
                      {'product_url': 'http://item.jd.com/333333333.html'},
                      {'product_url': 'https://item.jd.com/not-an-item'},
                      {'sku_id': '444444444'}, {'name': []}, {'source': 'unknown'}):
            before = self.path.read_bytes()
            with self.subTest(patch=patch), self.assertRaises(StopOrder):
                upsert_task(self.path, self.product_form(**patch))
            self.assertEqual(self.path.read_bytes(), before)

    def test_switch_to_product_clears_cart_evidence_and_switch_back_needs_scan(self):
        cart = upsert_task(self.path, self.form)
        direct = upsert_task(self.path, self.product_form(id=cart['id']))
        self.assertEqual(direct['source'], 'manual')
        self.assertEqual(direct['purchase_mode'], 'direct')
        self.assertEqual(direct['cart_spec'], '')
        catalog_write(self.path.parent / 'data', {'status': 'error', 'items': []})
        with self.assertRaises(StopOrder):
            upsert_task(self.path, self.form | {'id': cart['id'], 'source': 'cart'})
