import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "config.yaml"
        shutil.copy2(ROOT / "tests" / "fixtures" / "config.yaml", cls.path)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--config", str(cls.path), "web", "--port", str(cls.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONUTF8="1"))
        for _ in range(100):
            cls.http = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
            try:
                cls.http.request("GET", "/")
                response = cls.http.getresponse()
                cls.html = response.read().decode("utf-8")
                cls.token = json.loads(re.search(r"const token=(\"[^\"]+\");", cls.html)[1])
                break
            except OSError:
                cls.http.close()
                if cls.process.poll() is not None:
                    raise RuntimeError("Web service failed to start")
                time.sleep(.05)
        else:
            raise RuntimeError("Web service startup timed out")

    @classmethod
    def tearDownClass(cls):
        cls.http.close()
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temp.cleanup()

    def request(self, path, data=None, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Local-Token"] = self.token
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        self.http.request("POST" if data is not None else "GET", path, body=payload, headers=headers)
        response = self.http.getresponse()
        result = json.loads(response.read())
        return response.status, result

    def test_management_page_and_status(self):
        self.assertIn("扫码登录", self.html)
        self.assertIn("演练到结算页", self.html)
        status, result = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(result["running"])

    def test_cart_and_static_assets(self):
        status, result = self.request("/api/cart")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "idle")
        for path in ("/app.js", "/style.css"):
            self.http.request("GET", path)
            response = self.http.getresponse()
            self.assertEqual(response.status, 200)
            self.assertGreater(len(response.read()), 100)

    def test_product_link_api_without_scan_and_capability_flag(self):
        from datetime import datetime, timedelta
        from core.config import CST
        original = self.path.read_text(encoding='utf-8')
        try:
            _, config = self.request('/api/config')
            self.assertIn('manual', config['task_sources'])
            form = {'source': 'manual', 'product_url': 'https://item.jd.com/333333333.html',
                'target_time': (datetime.now(CST) + timedelta(days=1)).isoformat(), 'quantity': 1}
            status, _ = self.request('/api/tasks', form, token=False)
            self.assertEqual(status, 403)
            status, result = self.request('/api/tasks', form)
            self.assertEqual(status, 200)
            self.assertEqual(result['task']['purchase_mode'], 'direct')
            self.assertEqual(result['task']['source'], 'manual')
            self.assertEqual(result['task']['sku_id'], '333333333')
            _, state = self.request('/api/status')
            self.assertFalse(state['running'])
            self.assertEqual(state['orders'], [])
        finally:
            self.request('/api/config', {'yaml': original})

    def test_template_cannot_start_as_purchase(self):
        status, result = self.request("/api/action", {"action": "run"})
        self.assertEqual(status, 400)
        self.assertIn("添加", result["error"])

    def test_write_requires_local_token(self):
        status, _ = self.request("/api/action", {"action": "stop"}, token=False)
        self.assertEqual(status, 403)

    def test_browser_mode_save_preserves_tasks_and_requires_token(self):
        original = self.path.read_text(encoding='utf-8')
        try:
            status, _ = self.request('/api/browser', {'browser_connection': 'existing'}, token=False)
            self.assertEqual(status, 403)
            self.assertEqual(self.path.read_text(encoding='utf-8'), original)
            status, _ = self.request('/api/browser', {'browser_connection': 'existing'})
            self.assertEqual(status, 200)
            _, result = self.request('/api/config')
            self.assertEqual(result['browser']['browser_connection'], 'existing')
            import yaml
            before, after = yaml.safe_load(original), yaml.safe_load(result['yaml'])
            self.assertEqual(before['purchase'], after['purchase'])
            self.assertEqual(before['timing'], after['timing'])
            self.assertEqual(after['jd']['management_port'], self.port)
            status, _ = self.request('/api/browser', {'browser_connection': 'invalid'})
            self.assertEqual(status, 400)
        finally:
            self.request('/api/config', {'yaml': original})

    def test_payment_password_validation_does_not_echo_secret(self):
        status, result = self.request("/api/action", {"action": "run", "payment_password": "654321invalid"})
        self.assertEqual(status, 400)
        self.assertNotIn("654321", str(result))
        status, result = self.request("/api/action", {"action": "dry-run", "payment_password": "654321"})
        self.assertEqual(status, 400)
        self.assertIn("演练不会填写", result["error"])
        for endpoint in ("/api/config", "/api/status"):
            _, result = self.request(endpoint)
            self.assertNotIn("654321", str(result))

    def test_only_one_server_can_bind_port(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                sock.bind(("127.0.0.1", self.port))

    def test_valid_config_save_and_invalid_config_rejection(self):
        original = self.path.read_text(encoding="utf-8")
        status, _ = self.request("/api/config", {"yaml": original})
        self.assertEqual(status, 200)
        self.assertTrue(self.path.with_suffix(".backup.yaml").exists())
        status, _ = self.request("/api/config", {"yaml": "jd: []"})
        self.assertEqual(status, 400)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_stop_flag_and_invalid_task(self):
        status, _ = self.request("/api/action", {"action": "stop"})
        self.assertEqual(status, 200)
        self.assertTrue((self.path.parent / "data" / "stop.flag").exists())
        status, _ = self.request("/api/action", {"action": "run", "task": "does-not-exist"})
        self.assertEqual(status, 400)

    def test_overlapping_same_path_tasks_are_rejected_without_starting_worker(self):
        import yaml
        from datetime import datetime, timedelta
        from core.config import CST
        original = self.path.read_text(encoding='utf-8')
        try:
            target = (datetime.now(CST) + timedelta(hours=1)).isoformat()
            value = {'tasks': [{'id': name, 'sku_id': sku, 'name': name, 'source': 'cart',
                'purchase_mode': 'cart', 'quantity': 1, 'target_time': target}
                for name, sku in [('cart-a', '111111111'), ('cart-b', '222222222')]]}
            status, _ = self.request('/api/config', {'yaml': yaml.safe_dump(value)})
            self.assertEqual(status, 200)
            for action in ('run', 'dry-run'):
                status, result = self.request('/api/action', {'action': action})
                self.assertEqual(status, 400)
                self.assertIn('不会合并商品或自动排队', result['error'])
                _, state = self.request('/api/status')
                self.assertFalse(state['running'])
                self.assertEqual(state['orders'], [])
        finally:
            self.request('/api/config', {'yaml': original})
