# 在 Windows 下若安装了 onnxruntime，需尽早导入以防止与 Win32/ctypes 模块发生 C++ 运行时 DLL 加载冲突
try:
    import onnxruntime
except ImportError:
    pass

import urllib3

from easytrader import exceptions
from easytrader.api import use, follower
from easytrader.exceptions import (
    CircuitBreakerOpenError,
    HumanInterventionRequiredError,
    SchemaValidationError,
    TradeError,
    TradeVerificationError,
    VisualArbitrationError,
)
from easytrader.log import logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__version__ = "0.23.7"
__author__ = "shidenggui"
