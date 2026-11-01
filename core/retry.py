import time


class RetryBudget:
    def __init__(self, config):
        self.config = config
        self.count = 0
        self.deadline = time.monotonic() + config["timeout_seconds"]

    def available(self):
        limit = self.config["max_attempts"] if self.config["enabled"] else 1
        return self.count < limit and time.monotonic() < self.deadline

    def begin(self):
        self.count += 1

    def delay(self, network=False):
        seconds = self.config["interval_ms"] / 1000
        return min(10, seconds * 2 ** min(self.count - 1, 5)) if network else seconds
