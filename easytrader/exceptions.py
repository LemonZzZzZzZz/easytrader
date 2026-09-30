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

