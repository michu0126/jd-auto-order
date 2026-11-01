"""平台边界与分享链接解析；不联网解析短链，不伪造平台接口。"""
import hashlib
import re
from urllib.parse import parse_qs, urlparse

from core.errors import StopOrder

LABELS = {"jd": "京东", "taobao": "淘宝", "douyin": "抖音"}
DOMAINS = {"jd": ("jd.com",), "taobao": ("taobao.com", "tmall.com", "tb.cn"),
           "douyin": ("douyin.com", "jinritemai.com")}
HOMES = {"jd": "https://www.jd.com/", "taobao": "https://www.taobao.com/",
         "douyin": "https://www.douyin.com/"}
FLOW_SELECTORS = {"auth_user", "product_name", "buy_now", "checkout_root", "checkout_item",
                  "checkout_item_id", "checkout_quantity", "total", "address", "submit", "order_id", "order_status"}


def allowed_url(platform, url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (platform in DOMAINS and parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and any(host == root or host.endswith("." + root) for root in DOMAINS[platform]))


def share_url(platform, value):
    if not isinstance(value, str) or len(value) > 20000:
        raise StopOrder("商品链接必须是文本")
    urls = re.findall(r"https://[^\s<>\"'【】]+", value)
    matches = [url.rstrip("，。；,;") for url in urls if allowed_url(platform, url)]
    if len(matches) != 1:
        raise StopOrder("请粘贴一个所属平台的 HTTPS 商品链接或分享文案")
    return matches[0]


def product_id(platform, url, supplied=""):
    query = parse_qs(urlparse(url).query)
    detected = next((query[key][0] for key in ("id", "product_id", "item_id")
                     if key in query and re.fullmatch(r"[0-9]{1,30}", query[key][0])), "")
    if supplied and (not isinstance(supplied, str) or not re.fullmatch(r"[0-9]{1,30}", supplied)):
        raise StopOrder("商品 ID 应留空或填写数字")
    if supplied and detected and supplied != detected:
        raise StopOrder("商品 ID 与链接不一致")
    # 短链占位只用于任务去重，不作为结算商品 ID 的核对证据。
    return supplied or detected or "link-" + hashlib.sha256(url.encode()).hexdigest()[:16]


def variant_id(url, supplied=""):
    query = parse_qs(urlparse(url).query)
    detected = next((query[key][0] for key in ("skuId", "sku_id") if key in query), "")
    for value in (supplied, detected):
        if value and (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,30}", value)):
            raise StopOrder("所选 SKU ID 应为数字")
    if supplied and detected and supplied != detected:
        raise StopOrder("所选 SKU ID 与链接不一致")
    return supplied or detected


def readiness(cfg, platform):
    if platform == "jd":
        return {"ready": True, "status": "已接入", "detail": "已实现购物车、结算与订单提交；真实成功取决于页面和库存"}
    profile = cfg.platforms.get(platform, {})
    required = FLOW_SELECTORS | ({"checkout_variant_id"} if any(
        task.platform == platform and task.variant_id for task in cfg.tasks) else set())
    if (required <= profile.get("selectors", {}).keys()
            and profile.get("checkout_paths") and profile.get("result_paths")):
        return {"ready": True, "status": "自定义页面适配", "detail": "已配置定位器，仍需对真实商品演练验证"}
    detail = ("所给天猫商品页已可读取；淘宝购物车被浏览器安全策略拦截，结算仍待适配" if platform == "taobao" else
              "已检查该商品分享页：已抢光、完整价格需去 App；尚无已验证的电脑结算或 iPhone 执行端")
    return {"ready": False, "status": "待适配" if platform == "taobao" else "需要 App 执行端", "detail": detail}
