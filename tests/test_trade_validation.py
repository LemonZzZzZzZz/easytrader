# -*- coding: utf-8 -*-
"""资金路径入口校验测试（代码审查标准 5.2：双向校验之入口侧）。

覆盖：
- easytrader.utils.validation 的参数校验助手
- ClientTrader.trade / market_trade 的入口拦截（非法参数不得进入 GUI 流程）
- market_trade 在市价委托类型设置失败时必须拒绝提交（防止错误类型成交）
- server.py 远端交易接口的入口校验
"""
import unittest
from unittest.mock import MagicMock, patch

from easytrader import server
from easytrader.clienttrader import ClientTrader
from easytrader.exceptions import TradeError
from easytrader.utils.validation import (
    ensure_positive_number,
    extract_security_code,
    validate_market_trade_params,
    validate_security_code,
    validate_trade_params,
)


class TestValidationHelpers(unittest.TestCase):
    """校验助手的单元测试"""

    def test_extract_security_code(self):
        self.assertEqual(extract_security_code("600519"), "600519")
        self.assertEqual(extract_security_code("sh600519"), "600519")
        self.assertEqual(extract_security_code("SZ000001"), "000001")
        self.assertEqual(extract_security_code(" 600519 "), "600519")
        with self.assertRaises(ValueError):
            extract_security_code(600519)
        with self.assertRaises(ValueError):
            extract_security_code(None)

    def test_validate_security_code(self):
        self.assertEqual(validate_security_code("600519"), "600519")
        self.assertEqual(validate_security_code("sh600519"), "600519")
        self.assertEqual(validate_security_code("204001"), "204001")
        for bad in [
            "",
            "   ",
            "60051",
            "6005191",
            "abc123",
            "600519.SH",
            "sh60051",
            None,
        ]:
            with self.assertRaises(ValueError):
                validate_security_code(bad)

    def test_ensure_positive_number(self):
        self.assertEqual(ensure_positive_number(100, "数量"), 100.0)
        self.assertEqual(ensure_positive_number("1800.12", "价格"), 1800.12)
        bad_values = [
            0, -1, -0.001, "abc", "", None, float("nan"), float("inf")
        ]
        for bad in bad_values:
            with self.assertRaises(ValueError):
                ensure_positive_number(bad, "测试值")

    def test_validate_trade_params(self):
        code, price, amount = validate_trade_params(
            "sh600519", "1800.12", "100"
        )
        self.assertEqual(code, "600519")
        self.assertEqual(price, 1800.12)
        self.assertEqual(amount, 100.0)
        # 小数数量合法（由调用方取整），但必须为正
        _, _, amount_frac = validate_trade_params("600519", 1800.0, 100.9)
        self.assertEqual(amount_frac, 100.9)
        with self.assertRaises(ValueError):
            validate_trade_params("60051", 1800.0, 100)
        with self.assertRaises(ValueError):
            validate_trade_params("600519", 0, 100)
        with self.assertRaises(ValueError):
            validate_trade_params("600519", 1800.0, 0)
        with self.assertRaises(ValueError):
            validate_trade_params("600519", 1800.0, -100)

    def test_validate_market_trade_params(self):
        code, amount = validate_market_trade_params("688001", "200")
        self.assertEqual((code, amount), ("688001", 200.0))
        with self.assertRaises(ValueError):
            validate_market_trade_params("688001", -5)


class TestClientTraderEntryValidation(unittest.TestCase):
    """ClientTrader.trade / market_trade 入口拦截：非法参数不得触碰 GUI"""

    def setUp(self):
        self.trader = ClientTrader()
        self.trader._main = MagicMock()
        self.trader._app = MagicMock()
        self.trader._config = MagicMock()

    def test_trade_rejects_invalid_params_without_gui(self):
        invalid_cases = [
            ("60051", 10.0, 100),  # 代码不是 6 位
            ("600519.SH", 10.0, 100),  # 代码后缀非法
            ("600519", 0, 100),  # 价格为零
            ("600519", -10.0, 100),  # 价格为负
            ("600519", 10.0, 0),  # 数量为零
            ("600519", 10.0, -100),  # 数量为负
        ]
        for args in invalid_cases:
            with patch.object(self.trader, "_set_trade_params") as mock_set:
                with self.assertRaises(ValueError):
                    self.trader.trade(*args)
                mock_set.assert_not_called()

    def test_trade_accepts_valid_params(self):
        with patch.object(self.trader, "_set_trade_params") as mock_set:
            with patch.object(self.trader, "_capture_screen_image"):
                with patch.object(self.trader, "_submit_trade") as mock_submit:
                    with patch.object(
                        self.trader,
                        "_handle_pop_dialogs",
                        return_value={"message": "success"},
                    ):
                        res = self.trader.trade("sh600519", 1800.12, 100)
        mock_set.assert_called_once_with("sh600519", 1800.12, 100)
        mock_submit.assert_called_once()
        self.assertEqual(res["message"], "success")

    def test_market_trade_rejects_invalid_amount_without_gui(self):
        with patch.object(self.trader, "_type_edit_control_keys") as mock_type:
            with self.assertRaises(ValueError):
                self.trader.market_trade("688001", 0)
            mock_type.assert_not_called()


class TestMarketTradeFailClosed(unittest.TestCase):
    """市价委托类型设置失败必须拒绝提交，防止以错误类型成交"""

    def setUp(self):
        self.trader = ClientTrader()
        self.trader._main = MagicMock()
        self.trader._app = MagicMock()
        self.trader._config = MagicMock()

    def test_market_trade_raises_after_retries_exhausted(self):
        """GUI 持续异常时重试耗尽后抛 TradeError，且不点击提交"""
        with patch.object(self.trader, "_type_edit_control_keys"):
                with patch.object(
                    self.trader, "_set_market_trade_params"
                ):
                    with patch.object(
                        self.trader,
                        "_set_market_trade_type",
                        side_effect=RuntimeError("控件未就绪"),
                    ):
                        with patch.object(self.trader, "wait"):
                            with patch.object(
                                self.trader, "_submit_trade"
                            ) as mock_submit:
                                with self.assertRaises(TradeError):
                                    self.trader.market_trade(
                                        "688001", 200, ttype="最优五档成交剩余撤销"
                                    )
                                mock_submit.assert_not_called()

    def test_market_trade_propagates_type_error_immediately(self):
        """市价类型不在候选项内是确定性错误，立即失败且不重试"""
        with patch.object(self.trader, "_type_edit_control_keys"):
            with patch.object(
                self.trader,
                "_set_market_trade_type",
                side_effect=TypeError("不支持对应的市价类型"),
            ):
                with patch.object(self.trader, "wait") as mock_wait:
                    with patch.object(
                        self.trader, "_submit_trade"
                    ) as mock_submit:
                        with self.assertRaises(TypeError):
                            self.trader.market_trade(
                                "688001", 200, ttype="不存在的类型"
                            )
                        mock_wait.assert_not_called()
                        mock_submit.assert_not_called()


class TestServerTradeValidation(unittest.TestCase):
    """server.py 远端交易接口入口校验（审查标准 5.2 远端接口注入面）"""

    def setUp(self):
        self.app = server.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self._saved_store = dict(server.global_store)

    def tearDown(self):
        server.global_store.clear()
        server.global_store.update(self._saved_store)

    def _set_user(self):
        user = MagicMock()
        user.buy.return_value = {"entrust_no": "1"}
        user.sell.return_value = {"entrust_no": "2"}
        user.cancel_entrust.return_value = {"message": "success"}
        server.global_store["user"] = user
        return user

    def test_buy_rejects_invalid_payloads(self):
        self._set_user()
        invalid_payloads = [
            {"security": "600519", "price": -1, "amount": 100},  # 价格为负
            {"security": "600519", "price": 0, "amount": 100},  # 价格为零
            {"security": "600519", "price": 10, "amount": 0},  # 数量为零
            {"security": "", "price": 10, "amount": 100},  # 代码为空
            {"security": 600519, "price": 10, "amount": 100},  # 代码非字符串
            {"security": "600519", "amount": 100},  # 无价格且无成交量
        ]
        for payload in invalid_payloads:
            resp = self.client.post("/buy", json=payload)
            self.assertEqual(resp.status_code, 400, msg=str(payload))

    def test_buy_accepts_valid_payload(self):
        user = self._set_user()
        resp = self.client.post(
            "/buy", json={"security": "600519", "price": 10.5, "amount": 100}
        )
        self.assertEqual(resp.status_code, 201)
        user.buy.assert_called_once_with(
            security="600519", price=10.5, amount=100
        )

    def test_sell_rejects_invalid_amount(self):
        self._set_user()
        resp = self.client.post(
            "/sell", json={"security": "600519", "price": 10, "amount": -5}
        )
        self.assertEqual(resp.status_code, 400)

    def test_cancel_entrust_rejects_empty_entrust_no(self):
        self._set_user()
        for payload in [{"entrust_no": ""}, {"entrust_no": "   "}, {}]:
            resp = self.client.post("/cancel_entrust", json=payload)
            self.assertEqual(resp.status_code, 400, msg=str(payload))

    def test_cancel_entrust_accepts_valid_payload(self):
        user = self._set_user()
        resp = self.client.post(
            "/cancel_entrust", json={"entrust_no": "HT001"}
        )
        self.assertEqual(resp.status_code, 201)
        user.cancel_entrust.assert_called_once_with(entrust_no="HT001")


if __name__ == "__main__":
    unittest.main()
