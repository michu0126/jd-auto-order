"""每日尝试窗口的日期推进；按北京时间计算，成功后不重复购买。"""
from datetime import datetime, timedelta


def next_target(task, now_ns):
    target = task.target
    if task.recurrence != "daily":
        return target
    now = datetime.fromtimestamp(now_ns / 1e9, target.tzinfo)
    if target < now:
        target += timedelta(days=max(0, (now.date() - target.date()).days))
        if target < now:
            target += timedelta(days=1)
    return target
