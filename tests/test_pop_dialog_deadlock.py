# coding:utf-8
"""弹窗未知内容死循环 + 验证码熔断 守卫测试。

对应 2026-10-27 真机事故（sh.603466，09:15:33~09:15:52）：
「提示信息」弹窗内容不在白名单内 ⇒ TradePopDialogHandler 原实现直接 return None，
既不点按钮也不关窗 ⇒ ClientTrader._handle_pop_dialogs 在没有 visual_oracle 时
循环体等于空操作 ⇒ 空转 18.3s（正常基线 6.4s）后抛 dialog_dismiss_timeout，委托丢失。

⛔ 本文件所有用例都断言「是否真的点了按钮 / 循环了几轮」，
   不允许只断言 raises —— 那样等于没测到提前熔断这个改进。
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from easytrader import exceptions
from easytrader import pop_dialog_handler
from easytrader.clienttrader import (
    ClientTrader,
    DEFAULT_MAX_STAGNANT_DIALOG_ROUNDS,
    MAX_STAGNANT_DIALOG_ROUNDS_ENV,
    _resolve_max_stagnant,
)
from easytrader.pop_dialog_handler import TradePopDialogHandler


def _make_handler(content, title="提示信息"):
    """构造一个真实 TradePopDialogHandler，弹窗正文由 mock 提供。"""
    app = MagicMock(name="app")
    app.top_window.return_value.Static.window_text.return_value = content
    handler = TradePopDialogHandler(app)
    # 隔离真实 GUI 动作，只观察「有没有被调用」
    handler._submit_by_shortcut = MagicMock(name="_submit_by_shortcut")
    handler._submit_by_click = MagicMock(name="_submit_by_click")
    handler._close = MagicMock(name="_close")
    handler._capture_dialog_screenshot = MagicMock(return_value="shot.png")
    return handler


def _make_trader(always_true=True):
    trader = ClientTrader()
    trader._main = MagicMock(name="_main")
    trader._app = MagicMock(name="_app")
    return trader


class _NoProgressHandler:
    """模拟永远返回 None 的 handler（即「弹窗关不掉」现场）。"""

    calls = 0

    def __init__(self, app):
        pass

    def handle(self, title):
        type(self).calls += 1
        return None


class TestCaptchaBrake(unittest.TestCase):
    """验证码/人机校验：必须熔断，且绝不对弹窗做任何点击。"""

    def test_captcha_plain_raises_with_content(self):
        handler = _make_handler("请输入验证码后继续操作")
        with self.assertRaises(exceptions.HumanInterventionRequiredError) as ctx:
            handler.handle("提示信息")

        result = ctx.exception.result
        self.assertEqual(result["message"], "captcha_dialog")
        self.assertEqual(result["dialog_title"], "提示信息")
        self.assertIn("验证码", result["dialog_content"])
        self.assertEqual(result["screenshot"], "shot.png")

    def test_captcha_never_clicks_any_button(self):
        handler = _make_handler("请输入验证码后继续操作")
        with self.assertRaises(exceptions.HumanInterventionRequiredError):
            handler.handle("提示信息")

        handler._submit_by_click.assert_not_called()
        handler._submit_by_shortcut.assert_not_called()
        handler._close.assert_not_called()

    def test_captcha_wrong_code_variant_also_brakes(self):
        handler = _make_handler("验证码错误，请重新输入")
        with self.assertRaises(exceptions.HumanInterventionRequiredError) as ctx:
            handler.handle("提示信息")
        self.assertEqual(ctx.exception.result["message"], "captcha_dialog")

    def test_captcha_human_verification_variant(self):
        handler = _make_handler("请完成人机验证")
        with self.assertRaises(exceptions.HumanInterventionRequiredError):
            handler.handle("提示信息")


class TestUnknownDialog(unittest.TestCase):
    """未收录内容：必须留痕 + 尝试点确定 + 返回可终止循环的结果。"""

    def test_unknown_content_is_dismissed_and_returns(self):
        handler = _make_handler("该证券今日停牌")
        result = handler.handle("提示信息")

        self.assertIsInstance(result, dict)
        self.assertTrue(result["unknown_dialog"])
        self.assertIn("停牌", result["dialog_content"])
        # 必须真的点了「确定」，否则弹窗仍在，上层还会继续循环
        handler._submit_by_click.assert_called_once()

    def test_unknown_content_message_not_filtered_by_caller(self):
        """返回结果的 message 不得含 "unknown message"，否则会被上层过滤继续循环。"""
        handler = _make_handler("该证券今日停牌")
        result = handler.handle("提示信息")
        self.assertNotIn("unknown message", result["message"])

    def test_unknown_content_click_failure_does_not_raise(self):
        """点确定失败也必须返回结果，交由上层超时兜底，不能在 handler 里炸掉。"""
        handler = _make_handler("该证券今日停牌")
        handler._submit_by_click = MagicMock(side_effect=RuntimeError("no button"))
        result = handler.handle("提示信息")
        self.assertTrue(result["unknown_dialog"])


class TestLegacyBranchesUnchanged(unittest.TestCase):
    """原有分支行为必须一字不变（回归）。"""

    def test_shortcut_whitelist_unchanged(self):
        for keyword in (
            "超出涨跌停",
            "委托价格的小数价格应为",
            "逆回购",
            "正回购",
        ):
            with self.subTest(keyword=keyword):
                handler = _make_handler("{} 相关提示".format(keyword))
                result = handler.handle("提示信息")
                self.assertIsNone(result)
                handler._submit_by_shortcut.assert_called_once()
                handler._submit_by_click.assert_not_called()

    def test_tip_success_extracts_entrust_no(self):
        handler = _make_handler("委托成功，合同号 12345")
        result = handler.handle("提示")
        self.assertEqual(result["entrust_no"], "12345")
        handler._submit_by_click.assert_called_once()

    def test_tip_failure_raises_trade_error(self):
        handler = _make_handler("资金不足")
        with self.assertRaises(exceptions.TradeError):
            handler.handle("提示")
        handler._submit_by_click.assert_called_once()

    def test_other_title_closes_window(self):
        handler = _make_handler("随便什么内容")
        result = handler.handle("警告")
        self.assertIsNone(result)
        handler._close.assert_called_once()


class TestStagnantEarlyAbort(unittest.TestCase):
    """同标题连续无进展必须提前熔断，而不是空转满 10 轮。"""

    def setUp(self):
        _NoProgressHandler.calls = 0

    def test_stagnant_aborts_before_max_attempts(self):
        trader = _make_trader()
        with patch.dict(os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: ""}):
            with patch.object(trader, "is_exist_pop_dialog", return_value=True):
                with patch.object(
                    trader, "_get_pop_dialog_title", return_value="提示信息"
                ):
                    with patch.object(trader, "_capture_dialog_evidence", return_value=""):
                        with self.assertRaises(exceptions.TradeVerificationError) as ctx:
                            trader._handle_pop_dialogs(
                                handler_class=_NoProgressHandler
                            )

        result = ctx.exception.result
        self.assertEqual(result["message"], "dialog_stagnant")
        self.assertEqual(result["dialog_title"], "提示信息")
        self.assertTrue(result["dialog_trace"])
        # 关键：必须远早于 11 轮
        self.assertEqual(
            _NoProgressHandler.calls, DEFAULT_MAX_STAGNANT_DIALOG_ROUNDS + 1
        )
        self.assertLess(_NoProgressHandler.calls, 11)

    def test_timeout_branch_carries_trace_and_title(self):
        """把停滞阈值放大到 max_attempts，逼出原始超时分支并校验其携带上下文。"""
        trader = _make_trader()
        with patch.dict(
            os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: "10"}
        ):
            with patch.object(trader, "is_exist_pop_dialog", return_value=True):
                with patch.object(
                    trader, "_get_pop_dialog_title", return_value="提示信息"
                ):
                    with patch.object(trader, "_capture_dialog_evidence", return_value=""):
                        with self.assertRaises(exceptions.TradeVerificationError) as ctx:
                            trader._handle_pop_dialogs(
                                handler_class=_NoProgressHandler
                            )

        result = ctx.exception.result
        self.assertEqual(result["message"], "dialog_dismiss_timeout")
        self.assertEqual(result["dialog_title"], "提示信息")
        self.assertEqual(len(result["dialog_trace"]), 10)
        self.assertEqual(_NoProgressHandler.calls, 10)


class TestStagnantThresholdResolver(unittest.TestCase):
    def test_default_when_unset(self):
        with patch.dict(os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: ""}):
            self.assertEqual(
                _resolve_max_stagnant(10), DEFAULT_MAX_STAGNANT_DIALOG_ROUNDS
            )

    def test_env_override_and_clamp(self):
        with patch.dict(os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: "7"}):
            self.assertEqual(_resolve_max_stagnant(10), 7)
        with patch.dict(os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: "999"}):
            self.assertEqual(_resolve_max_stagnant(10), 10)
        with patch.dict(os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: "0"}):
            self.assertEqual(_resolve_max_stagnant(10), 1)
        with patch.dict(os.environ, {MAX_STAGNANT_DIALOG_ROUNDS_ENV: "abc"}):
            self.assertEqual(
                _resolve_max_stagnant(10), DEFAULT_MAX_STAGNANT_DIALOG_ROUNDS
            )


class TestDialogEvidenceCapture(unittest.TestCase):
    def test_capture_returns_empty_on_error(self):
        trader = _make_trader()
        trader._capture_window_image = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(trader._capture_dialog_evidence("提示信息"), "")

    def test_capture_writes_file_into_env_dir(self):
        import tempfile

        trader = _make_trader()
        trader._capture_window_image = MagicMock(return_value=MagicMock())
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ, {pop_dialog_handler.DIALOG_SHOT_DIR_ENV: tmp}
            ):
                path = trader._capture_dialog_evidence("提示信息")
            self.assertTrue(path)
            self.assertEqual(os.path.dirname(path), tmp)
            self.assertTrue(path.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
