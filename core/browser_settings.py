"""现有浏览器连接参数；自动发现仅读取 Chrome 的调试端口标记。"""
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from core.errors import StopOrder


def validate_endpoint(value):
    if value == "auto":
        return value
    try:
        url = urlsplit(value)
        valid = (url.scheme in ("http", "https", "ws", "wss")
                 and url.hostname in ("127.0.0.1", "localhost", "::1")
                 and url.port is not None and 1 <= url.port <= 65535
                 and not (url.username or url.password or url.query or url.fragment))
        if valid and ((url.scheme.startswith("http") and url.path in ("", "/"))
                      or (url.scheme.startswith("ws") and re.fullmatch(r"/devtools/browser(?:/[A-Za-z0-9-]+)?", url.path))):
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise StopOrder("cdp_endpoint 应为 auto 或本机 Chrome 调试地址（仅允许 localhost/127.0.0.1/::1）")


def resolve_endpoint(settings):
    endpoint = validate_endpoint(settings["cdp_endpoint"])
    if endpoint != "auto":
        return endpoint
    directory = settings["cdp_user_data_dir"]
    if not directory:
        local = os.environ.get("LOCALAPPDATA")
        if os.name != "nt" or not local:
            raise StopOrder("自动连接当前支持 Windows Chrome；其他环境请填写本机 cdp_endpoint")
        directory = str(Path(local) / "Google/Chrome/User Data")
    marker = Path(directory) / "DevToolsActivePort"
    try:
        if marker.stat().st_size > 4096:
            raise ValueError
        lines = marker.read_text(encoding="utf-8").strip().splitlines()
        port = int(lines[0])
        if not 1 <= port <= 65535:
            raise ValueError
        path = lines[1] if len(lines) > 1 else "/devtools/browser"
        if not re.fullmatch(r"/devtools/browser(?:/[A-Za-z0-9-]+)?", path):
            raise ValueError
    except (OSError, UnicodeError, ValueError, IndexError):
        raise StopOrder("尚未发现可接管的 Chrome。请在管理页面所在 Chrome 打开 chrome://inspect/#remote-debugging，开启允许远程调试，再检测连接") from None
    return f"ws://127.0.0.1:{port}{path}"
