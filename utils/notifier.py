import asyncio
import base64
import html
import logging
import os
import subprocess

LOG = logging.getLogger(__name__)


def toast_script(title, message):
    xml = '<toast><visual><binding template="ToastGeneric"><text>' + html.escape(title) + '</text><text>' + html.escape(message) + '</text></binding></visual></toast>'
    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return f"""$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')))
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell').Show($toast)
"""


async def notify(enabled, title, message):
    LOG.info("通知：%s — %s", title, message)
    if not enabled or os.name != "nt":
        return False
    encoded = base64.b64encode(toast_script(title, message).encode("utf-16-le")).decode("ascii")
    try:
        result = await asyncio.to_thread(subprocess.run,
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            LOG.warning("Windows 未接受桌面通知，请查看管理页和日志")
            return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        LOG.warning("桌面通知调用失败，请查看管理页和日志")
        return False
