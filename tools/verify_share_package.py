"""离线校验分享包，并在独立目录/随机本机端口验证空配置启动。"""
import compileall
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4
from zipfile import ZipFile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = Path(sys.argv[1]).resolve()
    stage = (ROOT / "data" / "packaging" / ("check-" + uuid4().hex[:8])).resolve()
    assert stage.is_relative_to((ROOT / "data").resolve())
    stage.mkdir(parents=True)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        entries = archive.namelist()
        manifest = json.loads(archive.read("jd-auto-order/MANIFEST.json"))
        assert set(entries) == {"jd-auto-order/" + name for name in manifest["files"]} | {"jd-auto-order/MANIFEST.json"}
        for name, info in manifest["files"].items():
            data = archive.read("jd-auto-order/" + name)
            assert hashlib.sha256(data).hexdigest() == info["sha256"]
            assert len(data) == info["bytes"]
        for name in entries:
            assert (stage / name).resolve().is_relative_to(stage)
            assert not any(part in ("data", ".venv", "__pycache__") for part in Path(name).parts)
            assert not name.endswith((".log", ".sqlite3", ".png", ".jpg", "VERIFICATION.md", "config.backup.yaml"))
        template = archive.read("jd-auto-order/config.example.yaml")
        assert archive.read("jd-auto-order/config.yaml") == template
        assert yaml.safe_load(template)["tasks"] == []
        assert yaml.safe_load(template).get("purchase", {}).get("payment_method", "online") == "online"
        pref = yaml.safe_load(template).get("jd", {}).get("payment_preference", {})
        assert pref.get("channel", "default") == "default"
        assert not any(pref.get(key) for key in ("bank_name", "card_type", "installment", "card_last4"))
        archive.extractall(stage)
    project = stage / "jd-auto-order"
    assert compileall.compile_dir(str(project), quiet=1)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT / ".venv" / "Lib" / "site-packages")
    # 使用真实解释器而非 Windows venv 启动器，以便只终止本次检查创建的服务。
    python = getattr(sys, "_base_executable", sys.executable)
    check = subprocess.run([python, str(project / "main.py"), "check"], cwd=project,
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert check.returncode == 0, check.stderr
    defaults = subprocess.run([python, "-c",
        "from core.config import Config; from pathlib import Path; "
        "p=Config.parse(Path('config.yaml'), {'tasks':[]}).jd['payment_preference']; "
        "assert p=={'channel':'default'}; "
        "assert all(Config.parse(Path('config.yaml'), {'tasks':[], 'jd':{'payment_preference':{'channel':c,'bank_name':'LEGACY_BANK','card_last4':'1234'}}}).jd['payment_preference']==p for c in ('auto','jd_pay','app'))"],
        cwd=project, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert defaults.returncode == 0, defaults.stderr
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    def read_local(path):
        for attempt in range(3):
            try:
                with opener.open(f"http://127.0.0.1:{port}" + path, timeout=3) as response:
                    assert response.status == 200
                    return response.read()
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(.15 * (attempt + 1))
    with (stage / "server.log").open("wb") as output:
        process = subprocess.Popen([python, str(project / "main.py"), "web", "--port", str(port)],
            cwd=project, env=env, stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            cfg = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("独立检查服务提前退出")
                try:
                    cfg = json.loads(read_local("/api/config"))
                    break
                except OSError:
                    time.sleep(.1)
            assert cfg is not None and cfg["tasks"] == []
            assert Path(cfg["config_file"]).resolve() == (project / "config.yaml").resolve()
            status = json.loads(read_local("/api/status"))
            assert not status["running"] and not status["orders"]
            html = read_local("/").decode("utf-8")
            assert 'id="task-form"' in html and 'id="link-task-form"' in html and "__TOKEN__" not in html
            assert 'value="cod"' not in html and "货到付款" not in html
            assert read_local("/app.js")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    canary = "PRIVATE_RUNTIME_CANARY_" + uuid4().hex
    (project / "config.yaml").write_text(canary, encoding="utf-8")
    (project / "data" / "session").mkdir(parents=True, exist_ok=True)
    (project / "data" / "session" / "cookie.txt").write_text(canary, encoding="utf-8")
    (project / "VERIFICATION.md").write_text(canary, encoding="utf-8")
    repacked = stage / "repacked.zip"
    repack = subprocess.run([python, str(project / "tools/package_source.py"), "--output", str(repacked)],
        cwd=project, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert repack.returncode == 0, repack.stderr
    with ZipFile(repacked) as archive:
        assert all(canary.encode() not in archive.read(name) for name in archive.namelist())
        assert archive.read("jd-auto-order/config.yaml") == template
    dirty_template = yaml.safe_load(template)
    dirty_template["tasks"] = [{"id": "private-task"}]
    (project / "config.example.yaml").write_text(yaml.safe_dump(dirty_template), encoding="utf-8")
    refused = stage / "must-not-exist.zip"
    rejection = subprocess.run([python, str(project / "tools/package_source.py"), "--output", str(refused)],
        cwd=project, env=env, capture_output=True, timeout=20)
    assert rejection.returncode != 0 and not refused.exists()
    for key, value in (("cdp_user_data_dir", canary), ("cdp_endpoint", "ws://127.0.0.1:9222/devtools/browser/private")):
        browser_template = yaml.safe_load(template)
        browser_template.setdefault("jd", {})[key] = value
        (project / "config.example.yaml").write_text(yaml.safe_dump(browser_template), encoding="utf-8")
        rejection = subprocess.run([python, str(project / "tools/package_source.py"), "--output", str(refused)],
            cwd=project, env=env, capture_output=True, timeout=20)
        assert rejection.returncode != 0 and not refused.exists()
    for patch in ({"bank_name": canary}, {"card_type": "信用卡"}, {"installment": "不分期"},
                  {"card_last4": "1234"}, {"channel": "auto"}, {"channel": "jd_pay"}, {"channel": "app"}, {"private_key": canary}):
        payment_template = yaml.safe_load(template)
        payment_template.setdefault("jd", {}).setdefault("payment_preference", {}).update(patch)
        (project / "config.example.yaml").write_text(yaml.safe_dump(payment_template), encoding="utf-8")
        rejection = subprocess.run([python, str(project / "tools/package_source.py"), "--output", str(refused)],
            cwd=project, env=env, capture_output=True, timeout=20)
        assert rejection.returncode != 0 and not refused.exists(), patch
    offline_template = yaml.safe_load(template)
    offline_template.setdefault("purchase", {})["payment_method"] = "cod"
    (project / "config.example.yaml").write_text(yaml.safe_dump(offline_template), encoding="utf-8")
    rejection = subprocess.run([python, str(project / "tools/package_source.py"), "--output", str(refused)],
        cwd=project, env=env, capture_output=True, timeout=20)
    assert rejection.returncode != 0 and not refused.exists()
    summary = {"archive": str(target), "files": len(entries), "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "manifest_verified": True,
        "empty_config_verified": True, "isolated_startup_verified": True, "compiled": True,
        "runtime_data_exclusion_verified": True, "private_template_rejected": True,
        "private_browser_settings_rejected": True,
        "generic_payment_defaults_verified": True, "personal_payment_template_rejected": True,
        "online_only_template_and_ui_verified": True,
        "legacy_payment_config_uses_page_default_verified": True,
        "report_directory": str(stage)}
    (stage / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
