# -*- coding: utf-8 -*-


class TradeError(IOError):
    pass


class TradeVerificationError(TradeError):
    def __init__(self, message=None, result=None):
        super(TradeVerificationError, self).__init__(message)
        self.result = result


class NotLoginError(Exception):
    def __init__(self, result=None):
        super(NotLoginError, self).__init__()
        self.result = result


class SchemaValidationError(ValueError):
    pass


class CircuitBreakerOpenError(IOError):
    pass


class HumanInterventionRequiredError(TradeError):
    """当识别到图形验证码 (CAPTCHA) 或需要人工介入的阻断弹窗时触发熔断报警"""
    pass


class VisualArbitrationError(TradeError):
    """视觉仲裁置信度不足或判定异常"""
    def __init__(self, message=None, result=None):
        super(VisualArbitrationError, self).__init__(message)
        self.result = result

