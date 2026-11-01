"""按白名单打包干净源码；不读取本机真实配置、会话或开发验证记录。"""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("main.py", "webapp.py", "requirements.txt", "setup.bat", "start.bat",
              "setup.ps1", "start.ps1", ".gitignore", "SHARE_README.md")
SOURCE_DIRS = ("core", "jd", "marketplaces", "utils", "tests", "ui")
SOURCE_SUFFIXES = {".py", ".yaml", ".html", ".css", ".js"}


def public_payload(root):
    root = Path(root)
    payload = {name: (root / name).read_bytes() for name in ROOT_FILES}
    payload["README.md"] = payload["SHARE_README.md"]
    for folder in SOURCE_DIRS:
        for path in sorted((root / folder).rglob("*")):
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                    and path.suffix in SOURCE_SUFFIXES):
                payload[path.relative_to(root).as_posix()] = path.read_bytes()
    # 只携带打包器；本机演练、诊断和探测脚本不在分享清单内。
    payload["tools/package_source.py"] = (root / "tools/package_source.py").read_bytes()
    template = (root / "config.example.yaml").read_bytes()
    example = yaml.safe_load(template.decode("utf-8-sig"))
    if example.get("tasks") != [] or any(key in example.get("purchase", {})
            for key in ("sku_id", "product_url", "target_time", "name", "address_keyword")):
        raise ValueError("config.example.yaml 必须为空任务模板，不能包含个人商品或地址")
    if example.get("purchase", {}).get("payment_method", "online") != "online":
        raise ValueError("分享模板只支持在线支付")
    preference = example.get("jd", {}).get("payment_preference", {})
    if (not isinstance(preference, dict) or preference.get("channel", "default") != "default"
            or any(preference.get(key) for key in ("bank_name", "card_type", "installment", "card_last4"))
            or set(preference) - {"channel", "bank_name", "card_type", "installment", "card_last4"}):
        raise ValueError("分享模板必须使用 default，不能包含银行、卡种、分期或卡号偏好")
    browser_settings = example.get("jd", {})
    if browser_settings.get("cdp_user_data_dir", "") or browser_settings.get("cdp_endpoint", "auto") != "auto":
        raise ValueError("分享模板不能包含本机 Chrome 用户目录或具体调试端点")
    payload["config.example.yaml"] = template
    payload["config.yaml"] = template
    return payload


def build_archive(root, target):
    payload = public_payload(root)
    manifest = {"format": 1, "kind": "clean-source-sharing-package",
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                  for name, data in sorted(payload.items())}}
    payload["MANIFEST.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            archive.writestr("jd-auto-order/" + name, data)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert "jd-auto-order/ui/app.js" in names
        assert not any("/data/" in name or "/.venv/" in name or name.endswith("/VERIFICATION.md") for name in names)
        assert archive.read("jd-auto-order/config.yaml") == archive.read("jd-auto-order/config.example.yaml")
    return {"path": str(target.resolve()), "files": len(payload), "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "jd-auto-order-source.zip")
    args = parser.parse_args()
    print(json.dumps(build_archive(ROOT, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
