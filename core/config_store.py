"""管理页任务编辑：核对购物车选品或京东详情链接，验证后原子保存。"""
import copy
import json
from datetime import datetime
from pathlib import Path

import yaml

from core.config import CST, Config, sku_id, start_time
from core.errors import StopOrder
from core.platforms import share_url, product_id


def raw_config(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))


def save_config(path, data):
    path = Path(path)
    text = data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    temp = path.with_suffix(".pending.yaml")
    try:
        temp.write_text(text, encoding="utf-8")
        cfg = Config.load(temp)
        path.with_suffix(".backup.yaml").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        temp.replace(path)
        return cfg
    finally:
        temp.unlink(missing_ok=True)


def is_example(task):
    return task.platform == "jd" and task.sku == "123456789" and task.name == "我的抢购商品" and task.id == "task-1"


def task_view(task):
    return {"id": task.id, "name": task.name, "sku_id": task.sku, "product_url": task.url,
        "quantity": task.quantity, "target_time": task.target.replace(tzinfo=None).isoformat(timespec="milliseconds"),
        "max_price": str(task.max_price) if task.max_price is not None else None, "purchase_mode": task.mode, "auto_submit": task.auto_submit,
        "payment_method": task.payment_method, "enabled": task.enabled, "source": task.source,
        "cart_spec": task.cart_spec, "sku_options": task.sku_options, "address_keyword": task.address_keyword,
        "retry": task.retry, "discounts": task.discounts, "example": is_example(task),
        "platform": task.platform, "recurrence": task.recurrence,
        "product_id": "" if task.sku.startswith("link-") else task.sku, "variant_id": task.variant_id}


def catalog_read(data_dir):
    path = Path(data_dir) / "cart.json"
    if not path.exists():
        return {"status": "idle", "items": [], "message": "尚未扫描购物车"}
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_write(data_dir, data):
    path = Path(data_dir) / "cart.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".pending.json")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def catalog_failure(data_dir, message):
    # scanned_at 仅代表上次成功结果，失败不能改写或冒充新的商品快照。
    catalog = catalog_read(data_dir) | {"status": "error", "complete": False,
        "failed_at": datetime.now(CST).isoformat(timespec="seconds"), "message": message}
    catalog_write(data_dir, catalog)
    return catalog


def materialized_tasks(cfg, raw):
    if "tasks" in raw:
        return [copy.deepcopy(raw.get("purchase", {}) | entry | {"id": task.id})
                for task, entry in zip(cfg.tasks, raw["tasks"])]
    if cfg.tasks and is_example(cfg.tasks[0]):
        return []
    return [copy.deepcopy(raw.get("purchase", {}))] if cfg.tasks else []


def upsert_task(path, form):
    cfg = Config.load(path)
    raw = raw_config(path)
    tasks = materialized_tasks(cfg, raw)
    taskid = form.get("id", "")
    existing = next((task for task in cfg.tasks if task.id == taskid), None) if taskid else None
    if taskid and existing is None:
        raise StopOrder("找不到要编辑的任务")
    if existing and existing.platform != "jd":
        raise StopOrder("无法跨平台修改已有任务")
    source = form.get("source", existing.source if existing else "cart")
    if source not in ("cart", "manual"):
        raise StopOrder("请选择购物车或商品详情页入口")
    item = None
    if source == "manual":
        link = form.get("product_url", existing.url if existing else "")
        sku = sku_id(link)
        if form.get("sku_id") and sku_id(form["sku_id"]) != sku:
            raise StopOrder("SKU 与商品详情链接不一致")
        name = form.get("name", "").strip() if isinstance(form.get("name", ""), str) else None
        if name is None or len(name) > 300:
            raise StopOrder("商品名称应为不超过300字的文本")
        name = name or (existing.name if existing and existing.sku == sku else "京东商品 " + sku)
        cart_spec = ""
    else:
        sku = str(form.get("sku_id", existing.sku if existing else ""))
        catalog = catalog_read(cfg.data_dir)
        item = next((item for item in catalog.get("items", []) if item["sku_id"] == sku), None)
        if existing is None or sku != existing.sku or existing.source != "cart":
            if catalog.get("status") != "ready" or item is None:
                raise StopOrder("请先扫描购物车，再选择其中的商品添加任务")
        name = item["name"] if item else existing.name
        cart_spec = item.get("spec", "") if item else existing.cart_spec
    if not taskid:
        base = ("product-" if source == "manual" else "cart-") + sku
        taskid = base
        suffix = 2
        used = {task.id for task in cfg.tasks}
        while taskid in used:
            taskid = f"{base}-{suffix}"
            suffix += 1
    target = start_time(form.get("target_time", ""))
    if target.timestamp() <= datetime.now(CST).timestamp():
        raise StopOrder("抢购时间必须晚于当前北京时间")
    # 不继承另一个商品的规格、地址或优惠选项。
    values = {
        "id": taskid, "platform": "jd", "name": name,
        "sku_id": sku, "product_url": f"https://item.jd.com/{sku}.html",
        "source": source, "cart_spec": cart_spec,
        "quantity": form.get("quantity", 1), "target_time": target.replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds"),
        "timezone": "Asia/Shanghai", "max_price": form.get("max_price"),
        "purchase_mode": form.get("purchase_mode", "direct" if source == "manual" else "cart"),
        "payment_method": form.get("payment_method", "online"),
        "auto_submit": form.get("auto_submit", True), "enabled": form.get("enabled", True),
        "sku_options": existing.sku_options if existing and sku == existing.sku else {},
        "recurrence": form.get("recurrence", existing.recurrence if existing else "once"),
        "address_keyword": form.get("address_keyword", ""), "require_breakdown": True,
        "retry": form.get("retry", {"enabled": True, "interval_ms": 500, "max_attempts": 10, "timeout_seconds": 30}),
        "discounts": form.get("discounts", {"use_existing_coupons": True, "coupon_text": "", "use_beans": False, "use_red_packets": False}),
    }
    tasks = [task for task in tasks if str(task.get("id", "")) != taskid]
    tasks.append(values)
    raw["tasks"] = tasks
    # purchase 仅保留通用默认值，防止空任务仍执行旧示例。
    if cfg.tasks and is_example(cfg.tasks[0]):
        raw["purchase"] = {"timezone": "Asia/Shanghai", "purchase_mode": "cart", "auto_submit": True}
    saved = save_config(path, raw)
    return task_view(next(task for task in saved.tasks if task.id == taskid))


def upsert_link_task(path, form):
    cfg = Config.load(path)
    raw = raw_config(path)
    platform = form.get("platform")
    if platform not in ("taobao", "douyin"):
        raise StopOrder("链接任务请选择淘宝或抖音")
    link = share_url(platform, form.get("product_url", ""))
    identifier = product_id(platform, link, form.get("product_id", ""))
    taskid = form.get("id", "")
    existing = next((task for task in cfg.tasks if task.id == taskid), None) if taskid else None
    if taskid and (existing is None or existing.platform != platform):
        raise StopOrder("无法跨平台修改已有任务")
    name = form.get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 300:
        raise StopOrder("请填写真实商品名称")
    target = start_time(form.get("target_time", ""))
    recurrence = form.get("recurrence", "daily")
    if recurrence == "once" and target <= datetime.now(CST):
        raise StopOrder("单次任务时间必须晚于当前北京时间")
    tasks = materialized_tasks(cfg, raw)
    if not taskid:
        base = platform + "-" + identifier
        taskid = base
        suffix = 2
        while taskid in {task.id for task in cfg.tasks}:
            taskid = base + "-" + str(suffix)
            suffix += 1
    values = {"id": taskid, "platform": platform, "name": name.strip(), "product_url": link,
        "product_id": form.get("product_id", ""), "quantity": form.get("quantity", 1),
        "variant_id": form.get("variant_id", ""),
        "sku_options": form.get("sku_options", {}), "target_time": target.isoformat(),
        "timezone": "Asia/Shanghai", "recurrence": recurrence, "purchase_mode": "direct",
        "source": "manual", "cart_spec": "", "payment_method": "online", "max_price": form.get("max_price"),
        "address_keyword": form.get("address_keyword", ""), "auto_submit": form.get("auto_submit", True),
        "enabled": form.get("enabled", False), "require_breakdown": False,
        "retry": form.get("retry", {"enabled": True, "interval_ms": 1000, "max_attempts": 10, "timeout_seconds": 30})}
    raw["tasks"] = [task for task in tasks if task.get("id") != taskid] + [values]
    saved = save_config(path, raw)
    return task_view(next(task for task in saved.tasks if task.id == taskid))


def delete_task(path, taskid):
    cfg = Config.load(path)
    if taskid not in {task.id for task in cfg.tasks}:
        raise StopOrder("任务不存在")
    raw = raw_config(path)
    raw["tasks"] = [task for task in materialized_tasks(cfg, raw) if str(task.get("id")) != taskid]
    save_config(path, raw)
