from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from core.browser_settings import validate_endpoint
from urllib.parse import urlparse

import yaml

from core.errors import StopOrder
from core.checkout_labels import SUBMIT_BUTTON_TEXTS
from core.platforms import LABELS, HOMES, share_url, product_id, allowed_url, variant_id

CST = timezone(timedelta(hours=8), "Asia/Shanghai")


def money(value, allow_zero=False):
    text = str(value).strip().replace(",", "").replace("，", "")
    match = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?", text)
    if not match:
        raise StopOrder(f"无法识别金额：{text!r}")
    result = Decimal(match[1])
    if result < 0 or (not allow_zero and result == 0):
        raise StopOrder("金额必须大于 0")
    return result


def sku_id(value):
    text = str(value).strip()
    if re.fullmatch(r"[0-9]+", text):
        return text
    url = urlparse(text)
    match = re.fullmatch(r"/(\d+)\.html", url.path)
    if url.scheme == "https" and url.hostname == "item.jd.com" and match:
        return match[1]
    raise StopOrder("商品应为数字 SKU 或 https://item.jd.com/数字.html")


def start_time(value):
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise StopOrder("target_time 示例：2026-11-11 20:00:00.000") from exc
    return result.replace(tzinfo=CST) if result.tzinfo is None else result.astimezone(CST)


def number(mapping, key, default, low, high, integer=False):
    value = mapping.get(key, default)
    allowed = (int,) if integer else (int, float)
    if type(value) not in allowed or not math.isfinite(value) or not low <= value <= high:
        raise StopOrder(f"{key} 必须在 {low} 到 {high} 之间" + ("且为整数" if integer else ""))
    return value


def boolean(mapping, key, default):
    value = mapping.get(key, default)
    if type(value) is not bool:
        raise StopOrder(f"{key} 必须为 true 或 false")
    return value


@dataclass
class Task:
    id: str
    name: str
    sku: str
    quantity: int
    target: datetime
    max_price: Decimal | None
    mode: str
    auto_submit: bool
    sku_options: dict
    address_keyword: str
    discounts: dict
    retry: dict
    require_breakdown: bool
    payment_method: str = "online"
    enabled: bool = True
    source: str = "manual"
    cart_spec: str = ""
    platform: str = "jd"
    product_link: str = ""
    recurrence: str = "once"
    variant_id: str = ""
    auto_pay: bool = False

    @property
    def url(self):
        return self.product_link if self.platform != "jd" else f"https://item.jd.com/{self.sku}.html"

    def key(self, profile):
        value = f"{Path(profile).resolve()}|{self.sku}|{self.quantity}|{self.target.isoformat()}"
        if self.platform != "jd":
            value = self.platform + "|" + value
            if self.variant_id:
                value += "|" + self.variant_id
        return hashlib.sha256(value.encode()).hexdigest()[:24]


@dataclass
class Config:
    path: Path
    jd: dict
    timing: dict
    notification: dict
    logging: dict
    tasks: list[Task]
    selectors: dict
    platforms: dict = field(default_factory=dict)

    @property
    def data_dir(self):
        return self.path.parent / "data"

    @property
    def session_dir(self):
        return (self.path.parent / self.jd["session_dir"]).resolve()

    def context_task(self, platform="jd"):
        matching = [task for task in self.tasks if task.platform == platform]
        if matching:
            return matching[0]
        return Task("session" if platform == "jd" else "session-" + platform, LABELS[platform] + "会话", "0", 1, datetime.now(CST), Decimal("1"),
            "direct", False, {}, "", {"use_existing_coupons": True, "coupon_text": "",
            "use_beans": False, "use_red_packets": False},
            {"enabled": True, "interval_ms": 500, "max_attempts": 1, "timeout_seconds": 30}, True,
            platform=platform, product_link=HOMES[platform])

    def for_platform(self, platform):
        from dataclasses import replace
        if platform == "jd":
            return self
        profile = self.platforms.get(platform, {})
        return replace(self, jd=self.jd | {"session_dir": profile.get("session_dir", "./data/session-" + platform),
                                          "browser_connection": "persistent"})

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            return cls.parse(path, data)
        except (OSError, yaml.YAMLError, TypeError, KeyError, ValueError) as exc:
            raise StopOrder(f"配置错误：{exc}") from exc

    @classmethod
    def parse(cls, path, data):
        if not isinstance(data, dict):
            raise StopOrder("YAML 顶层必须是对象")
        for name in ("jd", "timing", "retry", "notification", "logging", "purchase", "selectors", "platforms"):
            if name in data and not isinstance(data[name], dict):
                raise StopOrder(f"{name} 必须为对象")
        jd = {"login_mode": "qrcode", "browser": "chrome", "headless": False,
              "browser_connection": "persistent", "cdp_endpoint": "auto", "cdp_user_data_dir": "",
              "cdp_timeout_seconds": 60,
              "management_port": 8765,
              "session_dir": "./data/session", "manual_timeout_seconds": 300,
              "login_check_timeout_seconds": 15, "login_settle_seconds": 1.5,
              "action_timeout_ms": 5000, "result_timeout_seconds": 30, "payment_timeout_seconds": 60,
              "keep_browser_open": True, "block_media": True} | data.get("jd", {})
        if jd["browser"] not in ("chrome", "msedge", "chromium") or jd["login_mode"] != "qrcode":
            raise StopOrder("browser 支持 chrome/msedge/chromium；login_mode 支持 qrcode")
        if not isinstance(jd["session_dir"], str) or not jd["session_dir"]:
            raise StopOrder("session_dir 必须为非空目录")
        if jd["browser_connection"] not in ("persistent", "existing"):
            raise StopOrder("browser_connection 支持 persistent 或 existing")
        jd["cdp_endpoint"] = validate_endpoint(jd["cdp_endpoint"])
        if not isinstance(jd["cdp_user_data_dir"], str):
            raise StopOrder("cdp_user_data_dir 应留空或填写 Chrome 用户数据目录")
        if jd["browser_connection"] == "existing" and jd["browser"] != "chrome":
            raise StopOrder("复用现有浏览器模式请设置 browser: chrome")
        jd["cdp_timeout_seconds"] = number(jd, "cdp_timeout_seconds", 60, 5, 120)
        jd["management_port"] = number(jd, "management_port", 8765, 1, 65535, integer=True)
        extra_texts = jd.get("submit_button_texts", [])
        if not isinstance(extra_texts, list) or len(extra_texts) > 20 or any(
                not isinstance(text, str) or not text.strip() or len(text) > 40 for text in extra_texts):
            raise StopOrder("jd.submit_button_texts 必须为最多 20 个非空按钮文案的列表")
        jd["submit_button_texts"] = list(dict.fromkeys((*SUBMIT_BUTTON_TEXTS, *(re.sub(r"\s+", "", text) for text in extra_texts))))
        preference = jd.get("payment_preference", {})
        if not isinstance(preference, dict):
            raise StopOrder("jd.payment_preference 必须为对象")
        if preference.get("channel", "default") not in ("default", "auto", "jd_pay", "app"):
            raise StopOrder("payment_preference.channel 仅支持 default（沿用页面默认支付）")
        # 旧分享包保留任务升级时，也不能重新激活指定银行或分期的执行分支。
        jd["payment_preference"] = {"channel": "default"}
        run_budget = jd.get('run_budget', {})
        if not isinstance(run_budget, dict) or set(run_budget) - {'max_total', 'max_orders'}:
            raise StopOrder('jd.run_budget 只支持可选的 max_total 和 max_orders')
        if run_budget.get('max_total') is not None:
            money(run_budget['max_total'])
        if run_budget.get('max_orders') is not None:
            number(run_budget, 'max_orders', 1, 1, 1000, integer=True)
        for name in ("headless", "keep_browser_open", "block_media"):
            jd[name] = boolean(jd, name, jd[name])
        for name, default, low, high in (("manual_timeout_seconds", 300, 1, 3600),
                ("login_check_timeout_seconds", 15, 1, 60), ("login_settle_seconds", 1.5, 0.2, 10),
                ("action_timeout_ms", 5000, 500, 60000), ("result_timeout_seconds", 30, 1, 300),
                ("payment_timeout_seconds", 60, 1, 300)):
            jd[name] = number(jd, name, default, low, high)
        timing = {"prewarm_seconds": 60, "precision_mode": True, "sync_clock": True,
                  "cart_preload_timeout_seconds": 15,
                  "cart_action_timeout_ms": 300,
                  "cart_refresh_enabled": True, "cart_refresh_start_before_seconds": 2,
                  "cart_refresh_interval_ms": 200,
                  "clock_offset_ms": 0, "ntp_servers": ["ntp.aliyun.com", "time.cloudflare.com", "time.windows.com"]} | data.get("timing", {})
        timing["prewarm_seconds"] = number(timing, "prewarm_seconds", 60, 60, 3600)
        timing["cart_preload_timeout_seconds"] = number(timing, "cart_preload_timeout_seconds", 15, 1, 60)
        timing["cart_action_timeout_ms"] = number(timing, "cart_action_timeout_ms", 300, 100, 2000)
        timing["clock_offset_ms"] = number(timing, "clock_offset_ms", 0, -60000, 60000)
        timing["cart_refresh_start_before_seconds"] = number(timing, "cart_refresh_start_before_seconds", 2, 0, 30)
        # 旧秒级字段仍可读取；显式毫秒字段优先，方便旧管理服务保留兼容配置。
        legacy_interval = number(timing, "cart_refresh_interval_seconds", .2, .2, 60)
        if "cart_refresh_interval_ms" not in data.get("timing", {}) and "cart_refresh_interval_seconds" in data.get("timing", {}):
            timing["cart_refresh_interval_ms"] = legacy_interval * 1000
        timing["cart_refresh_interval_ms"] = number(timing, "cart_refresh_interval_ms", 200, 200, 60000)
        timing["cart_refresh_interval_seconds"] = timing["cart_refresh_interval_ms"] / 1000
        for name in ("precision_mode", "sync_clock", "cart_refresh_enabled"):
            timing[name] = boolean(timing, name, True)
        if not isinstance(timing["ntp_servers"], list) or not 1 <= len(timing["ntp_servers"]) <= 5 or any(
                not isinstance(v, str) or not v.strip() for v in timing["ntp_servers"]):
            raise StopOrder("ntp_servers 必须为 1 到 5 个时间服务器名称")
        raw_tasks = data.get("tasks", [data.get("purchase", {})])
        if not isinstance(raw_tasks, list) or len(raw_tasks) > 20:
            raise StopOrder("tasks 最多允许 20 个任务")
        tasks = []
        for index, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise StopOrder("每个任务必须为对象")
            value = data.get("purchase", {}) | raw
            platform = value.get("platform", "jd")
            if platform not in LABELS:
                raise StopOrder("platform 支持 jd、taobao、douyin")
            link = ""
            if platform == "jd":
                if not value.get("sku_id") and not value.get("product_url"):
                    raise StopOrder("请配置 sku_id 或 product_url")
                sku = sku_id(value.get("sku_id") or value["product_url"])
                if value.get("product_url") and sku_id(value["product_url"]) != sku:
                    raise StopOrder("sku_id 和 product_url 对应的 SKU 不一致")
            else:
                link = share_url(platform, raw.get("product_url", ""))
                sku = product_id(platform, link, raw.get("product_id", ""))
            if value.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
                raise StopOrder("timezone 必须为 Asia/Shanghai")
            mode = value.get("purchase_mode", "direct") if platform == "jd" else "direct"
            if mode not in ("direct", "cart"):
                raise StopOrder("purchase_mode 必须为 direct 或 cart")
            options = value.get("sku_options", {})
            if not isinstance(options, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in options.items()):
                raise StopOrder("sku_options 必须为规格名与规格值的文本映射")
            retry_value = data.get("retry", {}) | value.get("retry", {})
            retry = {"enabled": boolean(retry_value, "enabled", True),
                "interval_ms": number(retry_value, "interval_ms", 500, 500, 60000),
                "max_attempts": number(retry_value, "max_attempts", 10, 1, 100, integer=True),
                "timeout_seconds": number(retry_value, "timeout_seconds", 30, 1, 1800)}
            discounts = {"use_existing_coupons": True, "coupon_text": "", "use_beans": False,
                         "use_red_packets": False} | value.get("discounts", {})
            for name in ("use_existing_coupons", "use_beans", "use_red_packets"):
                discounts[name] = boolean(discounts, name, discounts[name])
            if not isinstance(discounts["coupon_text"], str):
                raise StopOrder("coupon_text 必须为文本")
            taskid = str(value.get("id", f"task-{index + 1}"))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", taskid):
                raise StopOrder("任务 id 只允许字母数字下划线和短横线")
            name = value.get("name", sku)
            address = value.get("address_keyword", "")
            if not isinstance(name, str) or not isinstance(address, str):
                raise StopOrder("name 与 address_keyword 必须为文本")
            payment = value.get("payment_method", "online")
            if payment != "online":
                raise StopOrder("payment_method 仅支持 online（在线支付），请更新旧任务的支付方式")
            source = value.get("source", "manual")
            cart_spec = value.get("cart_spec", "")
            if source not in ("manual", "cart") or not isinstance(cart_spec, str):
                raise StopOrder("source 或 cart_spec 无效")
            recurrence = value.get("recurrence", "once")
            if recurrence not in ("once", "daily"):
                raise StopOrder("recurrence 支持 once 或 daily")
            auto_pay = boolean(value, "auto_pay", False)
            if auto_pay and (platform != "jd" or not boolean(value, "auto_submit", True)):
                raise StopOrder("自动付款仅支持已启用自动提交的京东任务")
            tasks.append(Task(taskid, name, sku, number(value, "quantity", 1, 1, 99, integer=True),
                start_time(value["target_time"]), money(value["max_price"]) if value.get("max_price") not in (None, "") else None, mode,
                boolean(value, "auto_submit", True), options, address, discounts, retry,
                boolean(value, "require_breakdown", True), payment,
                boolean(value, "enabled", True), source, cart_spec, platform, link, recurrence,
                variant_id(link, raw.get("variant_id", "")) if platform != "jd" else "", auto_pay))
        if len({task.id for task in tasks}) != len(tasks):
            raise StopOrder("任务 id 不可重复")
        selectors = data.get("selectors", {})
        if any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in selectors.items()):
            raise StopOrder("selectors 必须为名称与非空 CSS 字符串的映射")
        notification = {"desktop": True} | data.get("notification", {})
        notification["desktop"] = boolean(notification, "desktop", True)
        log = {"level": "INFO", "save_screenshots": True} | data.get("logging", {})
        if log["level"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise StopOrder("logging.level 无效")
        log["save_screenshots"] = boolean(log, "save_screenshots", True)
        platforms = data.get("platforms", {})
        if platforms.keys() - {"taobao", "douyin"}:
            raise StopOrder("platforms 仅用于 taobao / douyin 的独立会话和页面适配")
        profiles = [str((Path(path).parent / jd["session_dir"]).resolve()).casefold()]
        for platform, settings in platforms.items():
            if not isinstance(settings, dict):
                raise StopOrder("平台配置必须为对象")
            directory = settings.get("session_dir", "./data/session-" + platform)
            if not isinstance(directory, str) or not directory:
                raise StopOrder("平台 session_dir 必须为非空目录")
            profiles.append(str((Path(path).parent / directory).resolve()).casefold())
            bindings = settings.get("selectors", {})
            if not isinstance(bindings, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                                      for k, v in bindings.items()):
                raise StopOrder("平台 selectors 必须为非空 CSS 字符串的映射")
            if settings.get("login_url") and not allowed_url(platform, settings["login_url"]):
                raise StopOrder("平台登录地址必须属于该平台")
            for key in ("checkout_paths", "result_paths"):
                paths = settings.get(key, [])
                if not isinstance(paths, list) or any(not isinstance(v, str) or not v.startswith("/") or v == "/" for v in paths):
                    raise StopOrder(key + " 必须为具体页面路径前缀列表")
        # 未显式配置的平台也使用不同的默认目录。
        for platform in ({task.platform for task in tasks} - {"jd"} - platforms.keys()):
            profiles.append(str((Path(path).parent / ("./data/session-" + platform)).resolve()).casefold())
        if len(set(profiles)) != len(profiles):
            raise StopOrder("不同平台必须使用不同的 session_dir")
        return cls(Path(path), jd, timing, notification, log, tasks, selectors, platforms)
