class StopOrder(Exception):
    pass


class ParallelSubmission(StopOrder):
    """另一路已有提交意图；本路正常停止防重，不视为新的购买失败。"""
    pass


class Retryable(Exception):
    pass


class SubmissionRejected(StopOrder):
    """本次提交出现已识别且已关闭的明确拒绝提示；仅原任务可有限重试。"""
    def __init__(self, feedback):
        self.feedback = dict(feedback)
        super().__init__(feedback['message'])


class LoginRequired(Exception):
    pass


class VerificationRequired(Exception):
    pass


class Cancelled(StopOrder):
    pass
