"""一次购买任务持有的账号锁，可从勾选阶段连续交接到结算尝试。"""
import asyncio
from decimal import Decimal
import math

from core.errors import ParallelSubmission, StopOrder


def validate_task_concurrency(tasks, *, immediate=False, prewarm_seconds=60):
    """同账号每条购买路径只允许一个活动任务；不合并、不静默排队。"""
    jd_tasks = [task for task in tasks if task.platform == 'jd']
    for index, left in enumerate(jd_tasks):
        for right in jd_tasks[index + 1:]:
            if left.mode != right.mode:
                continue
            anchor = max(left.target.timestamp(), right.target.timestamp())
            def intervals(task):
                target = task.target.timestamp()
                shifts = [0]
                if task.recurrence == 'daily':
                    nearest = math.floor((anchor - target) / 86400)
                    shifts = [shift for shift in (nearest - 1, nearest, nearest + 1) if shift >= 0]
                return [(target + shift * 86400 - prewarm_seconds,
                    target + shift * 86400 + task.retry['timeout_seconds']) for shift in shifts]
            overlap = immediate or any(a <= d and c <= b for a, b in intervals(left) for c, d in intervals(right))
            if overlap:
                lane = '购物车' if left.mode == 'cart' else '详情页'
                raise StopOrder(f'同一时段最多一个购物车任务和一个详情页任务；{lane}任务 {left.id} 与 {right.id} '
                    '的预热或购买窗口重叠。请停用其中一个或错开时间；不会合并商品或自动排队')


class PurchaseLanes:
    """购物车共用一条通道；直接购买各用一条，互不阻塞详情页入口。"""
    def __init__(self):
        self.locks = {}

    def for_task(self, task):
        key = (task.platform, "direct", task.id) if task.platform == "jd" and task.mode == "direct" else (task.platform, "shared")
        return self.locks.setdefault(key, asyncio.Lock())


class SubmissionGuard:
    """同一运行内重叠抢购窗口中的同 SKU 只允许一次提交意图。"""
    def __init__(self, max_total=None, max_orders=None):
        self.claims = []
        self.max_total = Decimal(str(max_total)) if max_total is not None else None
        self.max_orders = max_orders
        self.reserved_total = Decimal('0')
        self.reserved_orders = 0

    @staticmethod
    def window(task, target_ns=None):
        start = task.target.timestamp() if target_ns is None else target_ns / 1e9
        return start, start + task.retry["timeout_seconds"]

    def check(self, task, target_ns=None):
        start, end = self.window(task, target_ns)
        for platform, sku, left, right, owner in self.claims:
            if platform == task.platform and sku == task.sku and start <= right and left <= end:
                raise ParallelSubmission(f"同一商品的并行任务 {owner} 已进入提交，停止本路提交；请核实订单中心")

    def claim(self, task, target_ns, persist, amount=None):
        # 单个事件循环内无 await：落盘与登记之间不能插入另一条购买路径。
        self.check(task, target_ns)
        if self.max_total is not None and (amount is None or self.reserved_total + amount > self.max_total):
            raise StopOrder('本次运行已达到批准的合计金额上限，停止新增订单')
        if self.max_orders is not None and self.reserved_orders + 1 > self.max_orders:
            raise StopOrder('本次运行已达到批准的最多订单数，停止新增订单')
        persist()
        self.reserved_total += amount if amount is not None else Decimal('0')
        self.reserved_orders += 1
        start, end = self.window(task, target_ns)
        self.claims.append((task.platform, task.sku, start, end, task.id))


class PurchaseLease:
    def __init__(self, lock):
        self.lock = lock
        self.held = False
        self.owner = None

    async def acquire(self):
        if self.held:
            if self.owner is not asyncio.current_task():
                raise RuntimeError("不能跨任务复用账号锁凭据")
        else:
            await self.lock.acquire()
            self.held = True
            self.owner = asyncio.current_task()

    def release(self):
        # 只释放本任务成功取得的锁，取消排队时不能释放其他任务的锁。
        if self.held:
            if self.owner is not asyncio.current_task():
                raise RuntimeError("不能释放其他任务的账号锁")
            self.held = False
            self.owner = None
            self.lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.release()
