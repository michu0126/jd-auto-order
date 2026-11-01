import asyncio
import ctypes
import logging
import os
import time
from datetime import datetime

from core.config import CST
from core.errors import Cancelled

LOG = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, offset_ns=0, precision=True, stop_file=None):
        self.base_perf = time.perf_counter_ns()
        self.base_epoch = time.time_ns() + offset_ns
        self.precision = precision
        self.stop_file = stop_file
        self.timer_enabled = False
        if precision and os.name == "nt":
            self.timer_enabled = ctypes.windll.winmm.timeBeginPeriod(1) == 0

    def close(self):
        if self.timer_enabled:
            ctypes.windll.winmm.timeEndPeriod(1)
            self.timer_enabled = False

    def now_ns(self):
        return self.base_epoch + time.perf_counter_ns() - self.base_perf

    def check_stop(self):
        if self.stop_file and self.stop_file.exists():
            raise Cancelled("用户已停止任务")

    async def wait(self, epoch_ns):
        while True:
            self.check_stop()
            remaining = (epoch_ns - self.now_ns()) / 1e9
            if remaining <= 0:
                return
            if self.precision and remaining <= 0.003:
                await asyncio.sleep(0)
            elif self.precision and remaining <= 3:
                await asyncio.sleep(min(0.01, max(0, remaining - 0.002)))
            else:
                await asyncio.sleep(min(1, remaining))

    def record_trigger(self, taskid, target_ns):
        actual_ns = self.now_ns()
        error = (actual_ns - target_ns) / 1e6
        LOG.info("[%s] 预定触发=%s 实际触发=%s 触发误差=%+.3f ms", taskid,
            datetime.fromtimestamp(target_ns / 1e9, CST).isoformat(timespec="milliseconds"),
            datetime.fromtimestamp(actual_ns / 1e9, CST).isoformat(timespec="milliseconds"), error)
        return actual_ns, error
