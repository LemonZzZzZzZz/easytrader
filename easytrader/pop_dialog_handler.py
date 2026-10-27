# coding:utf-8
import datetime
import logging
import os
import re
import time
from typing import Optional

from easytrader import exceptions
from easytrader.utils.perf import perf_clock
from easytrader.utils.win_gui import SetForegroundWindow, ShowWindow, win32defines

logger = logging.getLogger(__name__)

# 弹窗存证截图目录（可用环境变量覆盖，缺省 logs/ths_dialogs）
DIALOG_SHOT_DIR_ENV = "EASYTRADER_DIALOG_SHOT_DIR"
DIALOG_SHOT_DIR_DEFAULT = os.path.join("logs", "ths_dialogs")


class PopDialogHandler:
    def __init__(self, app):
        self._app = app

    @staticmethod
    def _set_foreground(window):
        if window.has_style(win32defines.WS_MINIMIZE):  # if minimized
            ShowWindow(window.wrapper_object(), 9)  # restore window state
        else:
            SetForegroundWindow(window.wrapper_object())  # bring to front

    @perf_clock
    def handle(self, title):
        if any(s in title for s in {"提示信息", "委托确认", "网上交易用户协议", "撤单确认"}):
            self._submit_by_shortcut()
            return None

        if "提示" in title:
            content = self._extract_content()
            self._submit_by_click()
            return {"message": content}

        content = self._extract_content()
        self._close()
        return {"message": "unknown message: {}".format(content)}

    def _extract_content(self):
        return self._app.top_window().Static.window_text()

    @staticmethod
    def _extract_entrust_id(content):
        return re.search(r"[\da-zA-Z]+", content).group()

    def _submit_by_click(self):
        try:
            self._app.top_window()["确定"].click()
        except Exception as ex:
            self._app.Window_(best_match="Dialog", top_level_only=True).ChildWindow(
                best_match="确定"
            ).click()

    def _submit_by_shortcut(self):
        self._set_foreground(self._app.top_window())
        self._app.top_window().type_keys("%Y", set_foreground=False)

    def _close(self):
        self._app.top_window().close()

    def _capture_dialog_screenshot(self, title=""):
        """把当前弹窗截图落盘存证，返回路径。

        ⛔ 尽力而为：截图失败只记 debug，绝不阻断交易主流程。
        目录取 env ``EASYTRADER_DIALOG_SHOT_DIR``，缺省 ``logs/ths_dialogs``。
        """
        shot_dir = os.environ.get(DIALOG_SHOT_DIR_ENV) or DIALOG_SHOT_DIR_DEFAULT
        try:
            os.makedirs(shot_dir, exist_ok=True)
            safe_title = re.sub(
                r"[^0-9A-Za-z\u4e00-\u9fa5]+", "_", str(title or "dialog")
            ).strip("_")[:24] or "dialog"
            filename = "{}_{}.png".format(
                safe_title,
                datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3],
            )
            path = os.path.join(shot_dir, filename)
            self._app.top_window().capture_as_image().save(path)
            return path
        except Exception as ex:  # noqa: BLE001 存证失败不得影响交易
            logger.debug("弹窗存证截图失败: %s", ex)
            return ""


class TradePopDialogHandler(PopDialogHandler):
    # 命中即按「是(Y)」快捷键关窗的历史白名单（行为保持原样，不得随意增删）
    _SHORTCUT_KEYWORDS = (
        "超出涨跌停",
        "委托价格的小数价格应为",
        "逆回购",
        "正回购",
    )

    # 验证码 / 人机校验类：⛔ 绝不自动绕过，必须人工介入
    _CAPTCHA_KEYWORDS = (
        "验证码",
        "校验码",
        "图形码",
        "人机验证",
        "人机校验",
        "动态口令",
        "短信验证",
        "手机验证",
        "操作过于频繁",
        "请输入图片",
    )

    @perf_clock
    def handle(self, title) -> Optional[dict]:
        if title == "委托确认":
            self._submit_by_shortcut()
            return None

        if title == "提示信息":
            content = self._extract_content()

            for keyword in self._SHORTCUT_KEYWORDS:
                if keyword in content:
                    self._submit_by_shortcut()
                    return None

            # ── 2026-10-27 P0 修复 ──────────────────────────────────────
            # 原实现在此直接 `return None`：既不点按钮也不关窗。
            # ClientTrader._handle_pop_dialogs 在 `result is None` 且
            # `visual_oracle` 未接线时循环体等于空操作 ⇒ while 原地空转满
            # 10 轮才抛 dialog_dismiss_timeout。
            # 真机实测（sh.603466 200 股）：空转 18.3s（正常基线 6.4s）后委托
            # 被丢弃，从未到达券商，且无任何告警。
            screenshot = self._capture_dialog_screenshot(title)

            if any(keyword in content for keyword in self._CAPTCHA_KEYWORDS):
                # 验证码/人机校验：点任何按钮都可能被计为一次失败尝试，
                # ⛔ 不做任何自动操作，立即熔断等待人工处置。
                raise exceptions.HumanInterventionRequiredError(
                    "检测到验证码/人机校验弹窗，已熔断等待人工处置: "
                    "{!r}（截图 {}）".format(content, screenshot or "未落盘"),
                    result={
                        "message": "captcha_dialog",
                        "dialog_title": title,
                        "dialog_content": content,
                        "screenshot": screenshot,
                    },
                )

            # 其它未收录内容：留痕 + 尝试点「确定」关闭，把失败语义从
            # 「弹窗无限循环」拉回「未取得合同号」。
            logger.warning(
                "未收录的提示信息弹窗: title=%r content=%r 截图=%s —— 尝试点击确定关闭",
                title,
                content,
                screenshot or "未落盘",
            )
            try:
                self._submit_by_click()
            except Exception as ex:  # noqa: BLE001 关不掉交由上层超时兜底
                logger.warning("未收录弹窗点击确定失败: %s", ex)
            return {
                # ⚠️ 文案里不得出现 "unknown message" 子串：ClientTrader 会据此
                # 判定为「未识别」而继续循环，而不是返回本结果。
                "message": "已关闭未收录提示框: {}".format(content),
                "dialog_title": title,
                "dialog_content": content,
                "screenshot": screenshot,
                "unknown_dialog": True,
            }

        if title == "提示":
            content = self._extract_content()
            if "成功" in content:
                entrust_no = self._extract_entrust_id(content)
                self._submit_by_click()
                return {"entrust_no": entrust_no}

            self._submit_by_click()
            time.sleep(0.05)
            raise exceptions.TradeError(content)
        self._close()
        return None
