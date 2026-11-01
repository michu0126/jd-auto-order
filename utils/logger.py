import logging
import time
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler

from core.config import CST


def configure(cfg):
    folder = cfg.data_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    formatter.converter = lambda *args: datetime.now(CST).timetuple()
    handlers = [logging.StreamHandler(), RotatingFileHandler(folder / "run.log", maxBytes=4_000_000,
                backupCount=3, encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=cfg.logging["level"], handlers=handlers, force=True)


@contextmanager
def measure(taskid, operation):
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        logging.getLogger("timing").info("[%s] %s 耗时 %.2f ms", taskid, operation,
            (time.perf_counter_ns() - start) / 1e6)
