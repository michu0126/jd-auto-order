import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.config import CST, Config
from core.config_store import raw_config, upsert_link_task
from core.errors import StopOrder
from core.platforms import share_url, product_id, readiness, variant_id
from core.recurrence import next_target
from core.state_machine import StateStore
from main import execute

ROOT = Path(__file__).resolve().parents[1]


def external_task(platform, link):
    return {"id": platform + "-glasses", "platform": platform, "name": "模拟眼镜", "product_url": link,
            "target_time": "2026-10-26 10:00:00", "quantity": 1, "sku_options": {"尺码": "M"},
            "payment_method": "online", "recurrence": "daily", "enabled": True}


class PlatformConfigTests(unittest.TestCase):
    def test_tmall_product_and_variant_ids_are_distinct_without_assumed_size(self):
        link = "https://detail.tmall.com/item.htm?id=1094601672942&skuId=6313432066497"
        task = external_task("taobao", link) | {"sku_options": {}}
        parsed = Config.parse(ROOT / "config.yaml", {"tasks": [task]}).tasks[0]
        self.assertEqual(parsed.sku, "1094601672942")
        self.assertEqual(parsed.variant_id, "6313432066497")
        self.assertEqual(parsed.sku_options, {})
        changed = copy.copy(parsed)
        changed.variant_id = "6313432066498"
        self.assertNotEqual(parsed.key("data/session-taobao"), changed.key("data/session-taobao"))
        with self.assertRaises(StopOrder):
            variant_id(link, "6313432066498")

    def test_share_text_extracts_single_link_and_does_not_resolve(self):
        self.assertEqual(share_url("taobao", "【淘宝】https://c.tb.cn/h.test?tk=test CA381 商品"), "https://c.tb.cn/h.test?tk=test")
        self.assertEqual(share_url("douyin", "分享 https://v.douyin.com/test/ 打开查看"), "https://v.douyin.com/test/")
        self.assertTrue(product_id("douyin", "https://v.douyin.com/test/").startswith("link-"))
        self.assertEqual(product_id("douyin", "https://haohuo.jinritemai.com/detail?id=123456"), "123456")
        for invalid in ("https://evil.test/", "https://douyin.com.evil.test/", "https://a@v.douyin.com/test/"):
            with self.subTest(invalid=invalid), self.assertRaises(StopOrder):
                share_url("douyin", invalid)

    def test_platform_identity_profiles_and_existing_jd_key(self):
        task = external_task("taobao", "https://item.taobao.com/item.htm?id=123456789")
        cfg = Config.parse(ROOT / "config.yaml", {"tasks": [task]})
        self.assertEqual(cfg.tasks[0].url, task["product_url"])
        self.assertNotEqual(cfg.session_dir, cfg.for_platform("taobao").session_dir)
        self.assertFalse(readiness(cfg, "taobao")["ready"])
        with self.assertRaises(StopOrder):
            Config.parse(ROOT / "config.yaml", {"tasks": [task], "platforms": {"taobao": {"session_dir": "data/session"}}})
        old = Config.load(ROOT / "tests/fixtures/config.yaml").tasks[0]
        import hashlib
        expected = hashlib.sha256(f"{Path('data/session').resolve()}|{old.sku}|{old.quantity}|{old.target.isoformat()}".encode()).hexdigest()[:24]
        self.assertEqual(old.key('data/session'), expected)

    def test_daily_time_rolls_to_next_beijing_day_including_year_boundary(self):
        task = Config.parse(ROOT / "config.yaml", {"tasks": [external_task("douyin", "https://v.douyin.com/test/")]}).tasks[0]
        now = datetime(2026, 12, 31, 10, 0, 1, tzinfo=CST)
        self.assertEqual(next_target(task, round(now.timestamp() * 1e9)), datetime(2027, 1, 1, 10, tzinfo=CST))
        before = datetime(2026, 10, 26, 9, 59, tzinfo=CST)
        self.assertEqual(next_target(task, round(before.timestamp() * 1e9)), task.target)

    def test_link_form_preserves_jd_and_persists_actual_spec(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            before = [entry for entry in raw_config(path)["tasks"] if entry.get("platform", "jd") == "jd"]
            form = external_task("taobao", "https://c.tb.cn/h.test") | {"id": "", "enabled": False}
            saved = upsert_link_task(path, form)
            self.assertEqual(saved["platform"], "taobao")
            self.assertEqual(saved["sku_options"], {"尺码": "M"})
            after = [entry for entry in raw_config(path)["tasks"] if entry.get("platform", "jd") == "jd"]
            self.assertEqual(after, before)
            edited = upsert_link_task(path, form | {"id": saved["id"], "quantity": 2})
            self.assertEqual(edited["quantity"], 2)

    def test_daily_terminal_records_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite3"
            store = StateStore(path)
            store.claim("key", "daily", "100")
            store.finish("key", "unknown")
            store.close()
            store = StateStore(path)
            self.assertTrue(store.terminal_for_task("daily"))
            self.assertIsNone(store.terminal_for_task("other"))
            store.close()


class ParallelPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_platform_does_not_block_existing_jd_task(self):
        with tempfile.TemporaryDirectory() as folder:
            target = datetime.now(CST) + timedelta(minutes=5)
            cfg = Config.parse(Path(folder) / "config.yaml", {"tasks": [
                {"id": "jd", "sku_id": "111111111", "target_time": target.isoformat()},
                external_task("taobao", "https://c.tb.cn/h.test")], "timing": {"sync_clock": False},
                "notification": {"desktop": False}})
            context = SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(is_closed=lambda: False)), close=AsyncMock())
            class Playwright:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
            args = SimpleNamespace(command="run", task=None, platform=None, now=True, dry_run=False, noninteractive=True)
            with patch("main.async_playwright", return_value=Playwright()), \
                 patch("main.launch", new=AsyncMock(return_value=context)) as launch, \
                 patch("main.run_one", new=AsyncMock(return_value=True)) as run:
                self.assertEqual(await execute(cfg, args), 2)
                self.assertEqual(launch.await_count, 1)
                self.assertEqual(run.await_count, 1)
            store = StateStore(Path(folder) / "data/state.sqlite3")
            self.assertEqual(store.snapshot()["orders"], [])
            self.assertEqual(store.snapshot()["tasks"][0]["state"], "failed")
            store.close()

    async def test_three_platforms_enter_purchase_sections_together(self):
        with tempfile.TemporaryDirectory() as folder:
            target = datetime.now(CST) + timedelta(minutes=5)
            tasks = [{"id": "jd", "sku_id": "111111111", "target_time": target.isoformat()},
                     external_task("taobao", "https://item.taobao.com/item.htm?id=111111111"),
                     external_task("douyin", "https://haohuo.jinritemai.com/detail?id=111111111")]
            for task in tasks:
                task["recurrence"] = "once"
            cfg = Config.parse(Path(folder) / "config.yaml", {"tasks": tasks, "timing": {"sync_clock": False},
                "notification": {"desktop": False}})
            entered, locks, profiles = set(), [], []
            all_entered = asyncio.Event()

            async def operation(session, lock, *args):
                async with lock:
                    entered.add(session.task.platform)
                    locks.append(lock)
                    if len(entered) == 3:
                        all_entered.set()
                    await asyncio.wait_for(all_entered.wait(), 1)
                    return True

            class External:
                def __init__(self, context, page, cfg, task, store, scheduler):
                    self.task, self.page, self.submitted = task, page, False
                async def run(self, lock, *args):
                    return await operation(self, lock)

            async def launch(_p, local_cfg):
                profiles.append(local_cfg.session_dir)
                return SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(is_closed=lambda: False)), close=AsyncMock())

            class Playwright:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass

            args = SimpleNamespace(command="run", task=None, platform=None, now=True, dry_run=False, noninteractive=True)
            with patch("main.async_playwright", return_value=Playwright()), patch("main.launch", side_effect=launch), \
                 patch("main.WebPurchase", External), patch("main.run_one", side_effect=operation), \
                 patch("main.readiness", return_value={"ready": True}):
                self.assertEqual(await execute(cfg, args), 0)
            self.assertEqual(entered, {"jd", "taobao", "douyin"})
            self.assertEqual(len({id(lock) for lock in locks}), 3)
            self.assertEqual(len(set(profiles)), 3)
