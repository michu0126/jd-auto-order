"""本机购物车扫描、任务管理及运行界面。"""
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.config import Config
from core.config_store import catalog_failure, catalog_read, catalog_write, delete_task, is_example, raw_config, save_config, task_view, upsert_task, upsert_link_task
from core.errors import StopOrder
from core.state_machine import StateStore
from core.payment_secret import PaymentSecret
from core.purchase_lock import validate_task_concurrency
from core.worker import launch_worker
from core.platforms import LABELS, readiness

ROOT = Path(__file__).resolve().parent


def serve(config_path, port=8765, open_browser=False):
    config_path = Path(config_path).resolve()
    data_dir = config_path.parent / "data"
    data_dir.mkdir(exist_ok=True)
    token = secrets.token_urlsafe(32)
    process = None
    current_action = None
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass

        def reply(self, status, body, content_type="application/json; charset=utf-8"):
            payload = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

        def local(self):
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def running(self):
            return process is not None and process.poll() is None

        def do_GET(self):
            if not self.local():
                return self.reply(403, {"error": "仅允许本机访问"})
            try:
                if self.path == "/":
                    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
                    return self.reply(200, html.replace("__TOKEN__", json.dumps(token)), "text/html; charset=utf-8")
                if self.path in ("/app.js", "/style.css"):
                    content_type = "text/javascript; charset=utf-8" if self.path.endswith(".js") else "text/css; charset=utf-8"
                    return self.reply(200, (ROOT / "ui" / self.path[1:]).read_text(encoding="utf-8"), content_type)
                if self.path == "/api/config":
                    try:
                        cfg = Config.load(config_path)
                        tasks = [task_view(task) | {"adapter": readiness(cfg, task.platform)} for task in cfg.tasks]
                        platforms = [{"id": key, "name": name} | readiness(cfg, key) for key, name in LABELS.items()]
                        browser_settings = {key: cfg.jd[key] for key in ("browser_connection", "cdp_endpoint")}
                    except StopOrder:
                        tasks = []
                        platforms = []
                        browser_settings = {}
                    return self.reply(200, {"yaml": config_path.read_text(encoding="utf-8"),
                        "tasks": tasks, "platforms": platforms, "browser": browser_settings,
                        "task_sources": ["cart", "manual"], "config_file": str(config_path)})
                if self.path == "/api/cart":
                    with lock:
                        catalog = catalog_read(data_dir)
                        if catalog.get("status") == "scanning" and not self.running():
                            catalog = catalog_failure(data_dir, "扫描进程已结束，未返回成功结果，请查看日志后重试")
                    return self.reply(200, catalog)
                if self.path == "/api/status":
                    store = StateStore(data_dir / "state.sqlite3")
                    try:
                        status = store.snapshot()
                    finally:
                        store.close()
                    log_file = data_dir / "logs" / "run.log"
                    log = ""
                    if log_file.exists():
                        with log_file.open("rb") as handle:
                            handle.seek(max(0, log_file.stat().st_size - 24000))
                            log = handle.read().decode("utf-8", errors="replace")
                    return self.reply(200, status | {"running": self.running(), "action": current_action, "log": log})
                return self.reply(404, {"error": "未找到"})
            except Exception as exc:
                return self.reply(400, {"error": str(exc)})

        def do_POST(self):
            nonlocal process, current_action
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 100_000:
                    self.close_connection = True
                    return self.reply(400, {"error": "请求长度无效"})
                payload = self.rfile.read(length)
                if not self.local() or not secrets.compare_digest(self.headers.get("X-Local-Token", ""), token):
                    return self.reply(403, {"error": "无效的本地操作令牌"})
                data = json.loads(payload)
                with lock:
                    if self.path == "/api/browser":
                        if self.running():
                            raise StopOrder("任务或扫描运行中，请停止后切换浏览器")
                        mode = data.get("browser_connection")
                        if mode not in ("persistent", "existing"):
                            raise StopOrder("请选择独立浏览器或复用当前 Chrome")
                        raw = raw_config(config_path)
                        raw.setdefault("jd", {}).update(browser_connection=mode, management_port=port)
                        if mode == "existing":
                            raw["jd"]["browser"] = "chrome"
                        save_config(config_path, raw)
                        return self.reply(200, {"message": "浏览器方式已保存。复用模式请先在 Chrome 开启远程调试，再检测连接"})
                    if self.path == "/api/config":
                        if self.running():
                            raise StopOrder("任务或扫描运行中，请结束后修改配置")
                        save_config(config_path, data["yaml"])
                        return self.reply(200, {"message": "配置校验通过并已保存"})
                    if self.path in ("/api/tasks", "/api/tasks/link", "/api/tasks/delete"):
                        if self.running():
                            raise StopOrder("任务或扫描运行中，请结束后修改任务")
                        if self.path == "/api/tasks/delete":
                            delete_task(config_path, data["id"])
                            return self.reply(200, {"message": "任务已删除"})
                        task = upsert_link_task(config_path, data) if self.path == "/api/tasks/link" else upsert_task(config_path, data)
                        return self.reply(200, {"message": "抢购任务已保存，点击启动后才会执行", "task": task})
                    if self.path != "/api/action":
                        return self.reply(404, {"error": "未找到"})
                    action = data.get("action")
                    payment_secret = PaymentSecret(data.pop("payment_password", ""))
                    if payment_secret and action != "run":
                        raise StopOrder("支付密码仅用于启动定时任务，演练不会填写密码")
                    if action == "stop":
                        (data_dir / "stop.flag").touch()
                        return self.reply(200, {"message": "已请求停止，将在当前页面操作结束后停止；已提交的订单请到订单中心核实"})
                    if action not in ("browser-check", "login", "scan-cart", "prepare", "dry-run", "run"):
                        raise StopOrder("无效操作")
                    if self.running():
                        raise StopOrder("已有任务或扫描运行，请等待结束或停止")
                    cfg = Config.load(config_path)
                    platform = data.get("platform")
                    if platform is not None and platform not in LABELS:
                        raise StopOrder("未知平台")
                    if action == "scan-cart" and platform not in (None, "jd"):
                        raise StopOrder("购物车扫描目前只支持京东；淘宝/抖音使用商品链接")
                    selected = data.get("task", "") if action not in ("browser-check", "login", "scan-cart") else ""
                    if selected and selected not in {task.id for task in cfg.tasks}:
                        raise StopOrder("未知任务 id")
                    if action not in ("browser-check", "login", "scan-cart"):
                        active = [task for task in cfg.tasks if task.enabled and not is_example(task)
                            and (not selected or task.id == selected) and (not platform or task.platform == platform)]
                        if not active:
                            raise StopOrder("请先添加并启用抢购任务")
                        if selected and not readiness(cfg, active[0].platform)["ready"]:
                            raise StopOrder(readiness(cfg, active[0].platform)["detail"])
                        if action in ('run', 'dry-run'):
                            validate_task_concurrency(active, immediate=action == 'dry-run',
                                prewarm_seconds=cfg.timing['prewarm_seconds'])
                    command = [sys.executable, str(ROOT / "main.py"), "--config", str(config_path),
                        "run" if action == "dry-run" else action, "--noninteractive"]
                    if action == "dry-run":
                        command.append("--dry-run")
                    if selected:
                        command.extend(["--task", selected])
                    if platform:
                        command.extend(["--platform", platform])
                    (data_dir / "stop.flag").unlink(missing_ok=True)
                    if action == "scan-cart":
                        catalog_write(data_dir, catalog_read(data_dir) | {"status": "scanning", "complete": False,
                            "message": "正在读取京东购物车，需要验证时请在 Chrome 完成"})
                    log_dir = data_dir / "logs"
                    log_dir.mkdir(exist_ok=True)
                    env = dict(os.environ, PYTHONIOENCODING="utf-8")
                    try:
                        with (log_dir / "console.log").open("ab") as output:
                            process = launch_worker(command, ROOT, env, output, payment_secret)
                    except OSError:
                        if action == "scan-cart":
                            catalog_failure(data_dir, "无法启动扫描进程")
                        raise
                    finally:
                        payment_secret.clear()
                    current_action = action
                    return self.reply(200, {"message": "浏览器连接检测已启动，请在 Chrome 确认连接请求；不会操作商品" if action == "browser-check" else "购物车扫描已启动" if action == "scan-cart" else
                        "任务已启动，需要扫码或人工验证时请在 Chrome 完成"})
            except Exception as exc:
                return self.reply(400, {"error": str(exc)})

    class LocalServer(ThreadingHTTPServer):
        allow_reuse_address = False
        allow_reuse_port = False

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        if not open_browser:
            raise
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/api/config")
            existing = json.loads(connection.getresponse().read())
            if existing.get("config_file") != str(config_path):
                raise StopOrder(f"端口 {port} 已占用，请用 --port 选择其他端口") from exc
        except (OSError, ValueError) as error:
            raise StopOrder(f"端口 {port} 已占用，请用 --port 选择其他端口") from error
        finally:
            connection.close()
        webbrowser.open(f"http://127.0.0.1:{port}")
        print("已打开现有的本地管理页")
        return
    print(f"本地管理页面：http://127.0.0.1:{port}，Ctrl+C 关闭")
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        (data_dir / "stop.flag").touch()
        server.server_close()
