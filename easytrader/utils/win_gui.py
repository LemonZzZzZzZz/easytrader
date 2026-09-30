# coding:utf-8
import ctypes
from pywinauto import win32defines

try:
    from pywinauto.win32functions import ShowWindow
except ImportError:
    def ShowWindow(hwnd, cmd_show):
        if hasattr(hwnd, "handle"):
            hwnd = hwnd.handle
        if not isinstance(hwnd, int):
            return 0
        return ctypes.windll.user32.ShowWindow(hwnd, cmd_show)

try:
    from pywinauto.win32functions import SetForegroundWindow
except ImportError:
    def SetForegroundWindow(hwnd):
        if hasattr(hwnd, "handle"):
            hwnd = hwnd.handle
        if not isinstance(hwnd, int):
            return 0
        return ctypes.windll.user32.SetForegroundWindow(hwnd)


def get_window_dpi_scale(hwnd=None) -> float:
    """获取指定窗口的 DPI 缩放比例因子，默认 1.0 (96 DPI)"""
    try:
        if hwnd is not None and hasattr(hwnd, "handle"):
            hwnd = hwnd.handle
        if not isinstance(hwnd, int):
            hwnd = None
        u32 = ctypes.windll.user32
        if hwnd and hwnd > 0 and hasattr(u32, "GetDpiForWindow"):
            dpi = u32.GetDpiForWindow(hwnd)
            if dpi > 0:
                return float(dpi) / 96.0
        if hasattr(u32, "GetDpiForSystem"):
            dpi = u32.GetDpiForSystem()
            if dpi > 0:
                return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0
