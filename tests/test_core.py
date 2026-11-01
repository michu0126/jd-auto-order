import asyncio
import copy
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from core.clock_sync import NTP_EPOCH, Sample, calibrate, query
from core.config import Config, money, sku_id, start_time
from core.errors import Cancelled, StopOrder
from core.retry import RetryBudget
from core.scheduler import Scheduler
from core.state_machine import StateStore
from jd.order import success_result
from jd.product import stock_state
from jd.auth import credential_names_valid
from utils.notifier import toast_script

ROOT = Path(__file__).resolve().parents[1]


def config_data():
    return yaml.safe_load((ROOT / "tests" / "fixtures" / "config.yaml").read_text(encoding="utf-8"))


class CoreTests(unittest.TestCase):
    def test_pc_cookie_variants(self):
        self.assertTrue(credential_names_valid({"thor", "pinId"}))
        self.assertTrue(credential_names_valid({"thor", "unick"}))
        self.assertFalse(credential_names_valid({"pinId", "unick"}))
    def test_config_yaml_and_multi_task(self):
        data = config_data()
        data["tasks"] = [{"id": "one"}, {"id": "two", "max_price": 1999}]
        cfg = Config.parse(ROOT / "config.yaml", data)
        self.assertEqual([t.id for t in cfg.tasks], ["one", "two"])
        self.assertEqual(str(cfg.tasks[1].max_price), "1999")

    def test_link_sku_must_agree(self):
        data = config_data()
        data["purchase"]["product_url"] = "https://item.jd.com/999.html"
        with self.assertRaises(StopOrder):
            Config.parse(ROOT / "config.yaml", data)

    def test_price_cap_can_be_absent_null_or_empty(self):
        for value in (None, "", "absent"):
            data = config_data()
            if value == "absent":
                data["purchase"].pop("max_price", None)
            else:
                data["purchase"]["max_price"] = value
            with self.subTest(value=value):
                cfg = Config.parse(ROOT / "config.yaml", data)
                self.assertIsNone(cfg.tasks[0].max_price)

    def test_extra_submit_texts_are_validated_and_keep_known_labels(self):
        data = config_data()
        data["jd"]["submit_button_texts"] = ["同意并下单"]
        cfg = Config.parse(ROOT / "config.yaml", data)
        self.assertIn("立即支付", cfg.jd["submit_button_texts"])
        self.assertIn("无货订购", cfg.jd["submit_button_texts"])
        self.assertIn("同意并下单", cfg.jd["submit_button_texts"])
        for invalid in ("立即下单", [""], [False]):
            data["jd"]["submit_button_texts"] = invalid
            with self.subTest(value=invalid), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)

    def test_default_online_payment_uses_page_default(self):
        cfg = Config.parse(ROOT / "config.yaml", config_data())
        self.assertEqual(cfg.jd["payment_preference"], {"channel": "default"})
        for invalid in ("app", [], {"channel": "unknown"}):
            data = config_data()
            data["jd"]["payment_preference"] = invalid
            with self.subTest(value=invalid), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)

    def test_legacy_bank_preferences_cannot_reactivate_card_selection(self):
        for channel in ("default", "auto", "jd_pay", "app"):
            data = config_data()
            data["jd"]["payment_preference"] = {"channel": channel, "bank_name": "旧配置示例银行",
                "card_type": "信用卡", "installment": "不分期", "card_last4": "1234"}
            with self.subTest(channel=channel):
                self.assertEqual(Config.parse(ROOT / "config.yaml", data).jd["payment_preference"], {"channel": "default"})

    def test_only_online_payment_is_supported(self):
        for value in ("cod", "wechat", "", None):
            data = config_data()
            data["purchase"]["payment_method"] = value
            with self.subTest(value=value), self.assertRaisesRegex(StopOrder, "仅支持 online"):
                Config.parse(ROOT / "config.yaml", data)

    def test_reject_invalid_config(self):
        for patch_values in ({"quantity": True}, {"quantity": 0}, {"auto_submit": "false"}, {"sku_options": {"颜色": 1}}):
            data = config_data()
            data["purchase"].update(patch_values)
            with self.subTest(values=patch_values), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)

    def test_cart_refresh_defaults_and_validation(self):
        cfg = Config.parse(ROOT / "config.yaml", config_data())
        self.assertEqual(cfg.timing["cart_action_timeout_ms"], 300)
        for value in (0, 99, 2001, True, "300"):
            data = config_data()
            data["timing"]["cart_action_timeout_ms"] = value
            with self.subTest(cart_action_timeout=value), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)
        self.assertEqual(cfg.timing["cart_preload_timeout_seconds"], 15)
        for value in (0, 61, True, "15"):
            data = config_data()
            data["timing"]["cart_preload_timeout_seconds"] = value
            with self.subTest(preload_timeout=value), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)
        self.assertTrue(cfg.timing["cart_refresh_enabled"])
        self.assertEqual(cfg.timing["cart_refresh_interval_ms"], 200)
        self.assertEqual(cfg.timing["cart_refresh_start_before_seconds"], 2)
        for invalid in (0, 199, 60001, True, "fast"):
            data = config_data()
            data.setdefault("timing", {})["cart_refresh_interval_ms"] = invalid
            with self.subTest(value=invalid), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)
        data = config_data()
        data.setdefault("timing", {})["cart_refresh_enabled"] = False
        self.assertFalse(Config.parse(ROOT / "config.yaml", data).timing["cart_refresh_enabled"])

    def test_cart_refresh_milliseconds_override_legacy_seconds(self):
        data = config_data()
        data.setdefault("timing", {})["cart_refresh_interval_seconds"] = 2
        self.assertEqual(Config.parse(ROOT / "config.yaml", data).timing["cart_refresh_interval_ms"], 2000)
        data["timing"]["cart_refresh_interval_ms"] = 200
        self.assertEqual(Config.parse(ROOT / "config.yaml", data).timing["cart_refresh_interval_ms"], 200)
        for invalid in (-1, 31, True, "2"):
            data["timing"]["cart_refresh_start_before_seconds"] = invalid
            with self.subTest(value=invalid), self.assertRaises(StopOrder):
                Config.parse(ROOT / "config.yaml", data)

    def test_money_and_timezone(self):
        self.assertEqual(str(money("￥ 1,299.50")), "1299.50")
        self.assertEqual(start_time("2026-11-11 20:00:00").utcoffset().total_seconds(), 28800)
        self.assertEqual(sku_id("https://item.jd.com/123.html?x=y"), "123")
        with self.assertRaises(StopOrder):
            money("页面未加载")

    def test_duplicate_claim_survives_restart_and_second_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite3"
            first, second = StateStore(path), StateStore(path)
            first.claim("key", "task", money(10))
            with self.assertRaises(StopOrder):
                second.claim("key", "task", money(10))
            first.close()
            second.close()
            reopened = StateStore(path)
            self.assertEqual(reopened.get("key")[0], "submitting")
            reopened.close()

    def test_retry_limits_and_backoff(self):
        budget = RetryBudget({"enabled": True, "interval_ms": 500, "max_attempts": 2, "timeout_seconds": 1})
        budget.begin()
        self.assertEqual(budget.delay(network=True), .5)
        budget.begin()
        self.assertEqual(budget.delay(network=True), 1)
        self.assertFalse(budget.available())
        budget.deadline = time.monotonic() - 1
        self.assertFalse(budget.available())

    def test_order_success_needs_order_and_success_status(self):
        self.assertEqual(success_result("订单提交成功 订单号：123456789"), "123456789")
        self.assertEqual(success_result("订单号：123456789 待付款"), "123456789")
        self.assertIsNone(success_result("订单提交成功"))
        self.assertIsNone(success_result("历史订单号：123456789 库存不足"))
        self.assertIsNone(success_result("历史订单号：123456789 待付款"))
        self.assertIsNone(success_result("收银台 订单号：123456789 订单号：987654321"))

    def test_stock_states(self):
        self.assertEqual(stock_state("无货 到货通知"), "out_of_stock")
        self.assertEqual(stock_state("立即抢购", False), "not_started")
        self.assertEqual(stock_state("有货 加入购物车"), "available")

    def test_notification_text_is_encoded(self):
        script = toast_script("测试'\"", "<&>$()")
        self.assertNotIn("<&>$()", script)
        self.assertIn("FromBase64String", script)

    def test_ntp_rejects_invalid_origin(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def connect(self, value): pass
            def send(self, value): pass
            def recv(self, value): return bytes(48)
        with patch("core.clock_sync.socket.socket", return_value=FakeSocket()), self.assertRaises(ValueError):
            query("local.test")


class AsyncCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_precision_local_trigger(self):
        scheduler = Scheduler(precision=True)
        try:
            target = scheduler.now_ns() + 80_000_000
            await scheduler.wait(target)
            actual, error = scheduler.record_trigger("local-test", target)
            self.assertGreaterEqual(actual, target)
            self.assertLess(error, 250)  # 调度测试容忍 CI/桌面负载，不作为京东端到端精度承诺。
        finally:
            scheduler.close()

    async def test_stop_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "stop.flag"
            flag.touch()
            scheduler = Scheduler(precision=False, stop_file=flag)
            with self.assertRaises(Cancelled):
                await scheduler.wait(scheduler.now_ns() + 1_000_000_000)

    async def test_time_source_selection_and_failure_fallback(self):
        with patch("core.clock_sync.query", side_effect=[Sample("a", 10, 20), Sample("b", 20, 3)]):
            self.assertEqual(await calibrate(["a", "b"]), (20, "b"))
        with patch("core.clock_sync.query", side_effect=socket.timeout):
            self.assertEqual(await calibrate(["a"]), (0, "local-unverified"))
