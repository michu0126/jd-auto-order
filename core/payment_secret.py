"""本次进程使用的支付密码；不进入 YAML、命令行、环境变量或运行记录。"""
import json
import re

from core.errors import StopOrder


class PaymentSecret:
    def __init__(self, value=""):
        if not isinstance(value, str) or (value and not re.fullmatch(r"[0-9]{6}", value)):
            raise StopOrder("支付密码应为 6 位数字，或留空手动处理")
        self._value = value

    def __repr__(self):
        return "PaymentSecret(<redacted>)"

    def __bool__(self):
        return bool(self._value)

    def value(self):
        return self._value

    def clear(self):
        self._value = ""

    def pipe_payload(self):
        return (json.dumps({"payment_password": self._value}) + "\n").encode("utf-8")


def read_payment_secret(stream):
    try:
        payload = json.loads(stream.readline(256))
        if not isinstance(payload, dict) or set(payload) != {"payment_password"}:
            raise ValueError
        return PaymentSecret(payload["payment_password"])
    except Exception:
        raise StopOrder("无法读取本次运行的支付密码，请在管理页重新启动任务") from None
