class StopOrder(Exception):
    pass


class ParallelSubmission(StopOrder):
    """另一路已有提交意图；本路正常停止防重，不视为新的购买失败。"""
    pass


class Retryable(Exception):
    pass


class LoginRequired(Exception):
    pass


class VerificationRequired(Exception):
    pass


class Cancelled(StopOrder):
    pass
