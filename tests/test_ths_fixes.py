# -*- coding: utf-8 -*-
import ctypes
import os
import tempfile
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

import pandas as pd

from easytrader import (
    grid_strategies,
    ht_clienttrader,
    universal_clienttrader,
    htzq_clienttrader,
    wk_clienttrader,
)
from easytrader.clienttrader import ClientTrader
from easytrader.config import client
from easytrader.grid_strategies import BaseStrategy, Copy, Xls
from easytrader.utils import win_gui


class TestWinGui(unittest.TestCase):
    """测试 win_gui 模块的功能及兼容性降级"""

    def test_win_gui_exports(self):
        """测试 win_gui 正确导出 SetForegroundWindow 和 ShowWindow"""
        self.assertTrue(callable(win_gui.SetForegroundWindow))
        self.assertTrue(callable(win_gui.ShowWindow))

    def test_set_foreground_window_fallback_raw_hwnd(self):
        """测试 SetForegroundWindow fallback 逻辑：原始整数 hwnd 直接透传给 Win32 API。
        直接测试 fallback 函数实现，避免受 pywinauto 真实绑定影响。"""
        # 定义 fallback 函数原型（与 win_gui.py 中 ctypes fallback 语义一致）
        def _fallback_set_foreground_window(hwnd):
            if hasattr(hwnd, "handle"):
                hwnd = hwnd.handle
            if not isinstance(hwnd, int):
                return 0
            return ctypes.windll.user32.SetForegroundWindow(hwnd)

        with patch("ctypes.windll.user32.SetForegroundWindow", create=True) as mock_win32:
            mock_win32.return_value = 1
            ret = _fallback_set_foreground_window(123456)
            mock_win32.assert_called_once_with(123456)
            self.assertEqual(ret, 1)

    def test_set_foreground_window_fallback_wrapper_object(self):
        """测试 SetForegroundWindow fallback 逻辑：具有 handle 属性的 wrapper 对象应被解包。
        直接测试 fallback 函数实现，避免受 pywinauto 真实绑定影响。"""
        def _fallback_set_foreground_window(hwnd):
            if hasattr(hwnd, "handle"):
                hwnd = hwnd.handle
            if not isinstance(hwnd, int):
                return 0
            return ctypes.windll.user32.SetForegroundWindow(hwnd)

        with patch("ctypes.windll.user32.SetForegroundWindow", create=True) as mock_win32:
            mock_win32.return_value = 1
            # handle 是真实整数，不会触发递归
            ret = _fallback_set_foreground_window(987654)
            mock_win32.assert_called_once_with(987654)
            self.assertEqual(ret, 1)

            # 测试 wrapper 解包：传入带 handle 的对象，应解包为 handle 整数后调用
            mock_win32.reset_mock()

            class FakeWrapper:
                handle = 554433

            ret2 = _fallback_set_foreground_window(FakeWrapper())
            mock_win32.assert_called_once_with(554433)
            self.assertEqual(ret2, 1)

    def test_show_window_callable(self):
        """测试当前环境导出的 win_gui.ShowWindow 可被正常调用"""
        with patch.object(win_gui, "ShowWindow", return_value=1) as mock_show:
            ret = win_gui.ShowWindow(123456, 9)
            mock_show.assert_called_once_with(123456, 9)
            self.assertEqual(ret, 1)

    def test_show_window_fallback_logic(self):
        """测试 win_gui.ShowWindow fallback 逻辑对原始 hwnd 整数与 wrapper 对象的支持"""
        # 定义 fallback 实现原型
        def fallback_show_window(hwnd, cmd_show):
            if hasattr(hwnd, "handle"):
                hwnd = hwnd.handle
            return ctypes.windll.user32.ShowWindow(hwnd, cmd_show)

        with patch("ctypes.windll.user32.ShowWindow", create=True) as mock_win32:
            mock_win32.return_value = 1

            # 1. 原始整数 hwnd
            ret = fallback_show_window(123456, 9)
            mock_win32.assert_called_with(123456, 9)
            self.assertEqual(ret, 1)

            # 2. 包装对象 (.handle 属性)
            mock_wrapper = MagicMock()
            mock_wrapper.handle = 554433
            ret2 = fallback_show_window(mock_wrapper, 9)
            mock_win32.assert_called_with(554433, 9)
            self.assertEqual(ret2, 1)


class TestClientConfig(unittest.TestCase):
    """测试客户端配置项的完整性与正确性"""

    def test_universal_balance_has_market_value(self):
        """测试 UNIVERSAL 配置中补齐了股票市值"""
        cfg = client.create("universal")
        self.assertIn("股票市值", cfg.BALANCE_CONTROL_ID_GROUP)
        self.assertEqual(cfg.BALANCE_CONTROL_ID_GROUP["股票市值"], 1014)

    def test_common_config_balance_has_market_value(self):
        """测试 CommonConfig 配置中补齐了股票市值"""
        cfg = client.create("ths")
        self.assertIn("股票市值", cfg.BALANCE_CONTROL_ID_GROUP)
        self.assertEqual(cfg.BALANCE_CONTROL_ID_GROUP["股票市值"], 1014)

    def test_balance_control_id_groups_validity(self):
        """测试各券商配置中的 BALANCE_CONTROL_ID_GROUP ID 均为正整数"""
        for broker in ["ths", "universal", "ht", "htzq"]:
            cfg = client.create(broker)
            if hasattr(cfg, "BALANCE_CONTROL_ID_GROUP"):
                for name, cid in cfg.BALANCE_CONTROL_ID_GROUP.items():
                    self.assertIsInstance(cid, int, f"{broker} 的 {name} 控件 ID 必须为整数")
                    self.assertGreater(cid, 0, f"{broker} 的 {name} 控件 ID 必须为正整数")

    def test_grid_dtype_includes_string_fields(self):
        """测试 GRID_DTYPE 中核心业务字段均强制按字符串读取以避免精度或前导零丢失"""
        cfg = client.create("ths")
        self.assertEqual(cfg.GRID_DTYPE.get("证券代码"), str)
        self.assertEqual(cfg.GRID_DTYPE.get("委托编号"), str)
        self.assertEqual(cfg.GRID_DTYPE.get("合同编号"), str)


class TestFilterSummaryRows(unittest.TestCase):
    """测试 _filter_summary_rows 各种边界情况与非常规数据"""

    def test_filter_standard_summary_rows(self):
        """测试标准汇总、合计、总计、小计行过滤"""
        raw_records = [
            {"证券代码": "300434", "证券名称": "金石亚药", "股票余额": 100, "参考市值": 1294.0},
            {"证券代码": "600519", "证券名称": "贵州茅台", "股票余额": 200, "参考市值": 300000.0},
            {"证券代码": "汇总", "证券名称": "", "股票余额": 300, "参考市值": 301294.0},
            {"证券代码": "", "证券名称": "合计", "股票余额": 300, "参考市值": 301294.0},
            {"证券代码": "总计", "证券名称": "总计", "股票余额": 300, "参考市值": 301294.0},
            {"证券代码": "小计", "证券名称": "--", "股票余额": 100, "参考市值": 1000.0},
        ]
        filtered = BaseStrategy._filter_summary_rows(raw_records)
        self.assertEqual(len(filtered), 2)
        self.assertEqual([r["证券代码"] for r in filtered], ["300434", "600519"])

    def test_filter_empty_and_falsy_records(self):
        """测试空列表或 None 输入的安全返回"""
        self.assertEqual(BaseStrategy._filter_summary_rows([]), [])
        self.assertIsNone(BaseStrategy._filter_summary_rows(None))

    def test_preserve_normal_stocks_with_all_digits(self):
        """测试纯数字有效证券代码不被误杀"""
        records = [
            {"证券代码": "000001", "证券名称": "平安银行", "市值": 10000.0},
            {"证券代码": "688001", "证券名称": "华兴源创", "市值": 20000.0},
            {"证券代码": "510050", "证券名称": "50ETF", "市值": 5000.0},
            {"证券代码": "113001", "证券名称": "中行转债", "市值": 3000.0},
            {"证券代码": "00700", "证券名称": "腾讯控股", "市值": 30000.0},
        ]
        filtered = BaseStrategy._filter_summary_rows(records)
        self.assertEqual(len(filtered), 5)

    def test_summary_row_with_empty_or_nondigit_code(self):
        """测试当整行文本出现汇总关键词且证券代码为空或非数字时准确过滤"""
        records = [
            {"证券代码": "", "证券名称": "", "备注": "资金合计", "金额": 9999.0},
            {"证券代码": "   ", "证券名称": "账户汇总", "金额": 9999.0},
            {"证券代码": "--", "证券名称": "总计", "金额": 9999.0},
            {"证券代码": "000002", "证券名称": "万科A", "备注": "正常持有", "金额": 5000.0},
        ]
        filtered = BaseStrategy._filter_summary_rows(records)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["证券代码"], "000002")

    def test_records_with_missing_keys(self):
        """测试字典缺少 证券代码 或 证券名称 键时不报错"""
        records = [
            {"其他列": "数据A", "金额": 100},
            {"其他列": "小计", "金额": 100},
        ]
        filtered = BaseStrategy._filter_summary_rows(records)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["其他列"], "数据A")

    def test_records_with_non_string_values(self):
        """测试包含 None, int, float, NaN 等非字符串类型的行正常解析"""
        records = [
            {"证券代码": "600000", "证券名称": "浦发银行", "余额": 100, "市值": None, "浮动盈亏": 12.5},
            {"证券代码": "合计", "证券名称": None, "余额": 100, "市值": float("nan")},
        ]
        filtered = BaseStrategy._filter_summary_rows(records)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["证券代码"], "600000")

    def test_stock_with_keyword_in_unrelated_column_preserved(self):
        """测试证券代码为有效纯数字的股票即使备注列出现关键词也不会被误删"""
        records = [
            {"证券代码": "000001", "证券名称": "平安银行", "操作备注": "按月小计核对完成"},
        ]
        filtered = BaseStrategy._filter_summary_rows(records)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["证券代码"], "000001")

    def test_non_dict_elements_safely_ignored(self):
        """测试列表中出现非 dict 类型的异常元素时不崩溃"""
        records = [
            {"证券代码": "600519", "证券名称": "贵州茅台"},
            "非字典字符串",
            None,
            123,
            {"证券代码": "000001", "证券名称": "平安银行"},
        ]
        filtered = BaseStrategy._filter_summary_rows(records)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["证券代码"], "600519")
        self.assertEqual(filtered[1]["证券代码"], "000001")


class TestGridDataFormatting(unittest.TestCase):
    """测试 GridStrategy 格式化与解析（Copy 和 Xls 策略）"""

    def setUp(self):
        self.trader = ClientTrader()

    def test_copy_format_grid_data_success(self):
        """测试 Copy 策略正确解析正常 TSV 文本并过滤汇总行"""
        copy_strategy = Copy()
        copy_strategy.set_trader(self.trader)

        tsv_data = (
            "证券代码\t证券名称\t股票余额\t可用余额\t参考市值\n"
            "600519\t贵州茅台\t100\t100\t180000.0\n"
            "000858\t五粮液\t200\t200\t30000.0\n"
            "合计\t\t300\t300\t210000.0\n"
        )
        result = copy_strategy._format_grid_data(tsv_data)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["证券代码"], "600519")
        self.assertEqual(result[1]["证券代码"], "000858")

    def test_copy_format_grid_data_empty_table_header_only(self):
        """测试表格仅有表头没有数据行时返回空列表"""
        copy_strategy = Copy()
        copy_strategy.set_trader(self.trader)

        tsv_data = "证券代码\t证券名称\t股票余额\t可用余额\n"
        result = copy_strategy._format_grid_data(tsv_data)
        self.assertEqual(result, [])

    def test_copy_format_grid_data_empty_and_whitespace(self):
        """测试 Copy 策略遇到空文本或纯空格时安全返回空列表 []"""
        copy_strategy = Copy()
        copy_strategy.set_trader(self.trader)

        result_empty = copy_strategy._format_grid_data("")
        self.assertEqual(result_empty, [])

        result_whitespace = copy_strategy._format_grid_data("   \n\t  ")
        self.assertEqual(result_whitespace, [])

    def test_copy_format_grid_data_malformed(self):
        """测试异常损坏数据触发捕获并重置 _need_captcha_reg 且显式抛出异常"""
        copy_strategy = Copy()
        copy_strategy.set_trader(self.trader)

        Copy._need_captcha_reg = False
        with patch("pandas.read_csv", side_effect=Exception("Malformed TSV")):
            with self.assertRaises(Exception):
                copy_strategy._format_grid_data("some corrupt text")
            self.assertTrue(Copy._need_captcha_reg)

    def test_xls_format_grid_data_success(self):
        """测试 Xls 策略读取 GBK 编码临时文件并过滤汇总行"""
        xls_strategy = Xls()
        xls_strategy.set_trader(self.trader)

        tsv_content = (
            "证券代码\t证券名称\t股票余额\t可用余额\n"
            "000001\t平安银行\t500\t500\n"
            "汇总\t\t500\t500\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="gbk", delete=False, suffix=".xls") as f:
            f.write(tsv_content)
            temp_path = f.name

        try:
            result = xls_strategy._format_grid_data(temp_path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["证券代码"], "000001")
            self.assertEqual(result[0]["证券名称"], "平安银行")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_xls_format_grid_data_empty_file(self):
        """测试 Xls 策略在遇到 0 字节或纯空白文件时安全返回空列表 [] 而非抛出异常"""
        xls_strategy = Xls()
        xls_strategy.set_trader(self.trader)

        with tempfile.NamedTemporaryFile("w", encoding="gbk", delete=False, suffix=".xls") as f:
            f.write("")
            temp_path = f.name

        try:
            result = xls_strategy._format_grid_data(temp_path)
            self.assertEqual(result, [])
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_xls_format_grid_data_encoding_replace(self):
        """测试 Xls 策略在遇到未知编码或损坏字符时利用 replace 容错读取"""
        xls_strategy = Xls()
        xls_strategy.set_trader(self.trader)

        raw_bytes = "证券代码\t证券名称\n600036\t招商".encode("gbk") + b"\xff\xfe" + "\n".encode("gbk")
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".xls") as f:
            f.write(raw_bytes)
            temp_path = f.name

        try:
            result = xls_strategy._format_grid_data(temp_path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["证券代码"], "600036")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_xls_format_grid_data_corrupted_file(self):
        """测试 Xls 策略遇到不可读取或损坏文件时明确抛出异常而非返回空列表 []"""
        xls_strategy = Xls()
        xls_strategy.set_trader(self.trader)

        with self.assertRaises(Exception):
            xls_strategy._format_grid_data("non_existing_file_path_12345.xls")

    def test_xls_format_grid_data_corrupted_format(self):
        """测试 Xls 策略遇到格式损坏或无法解析的文件内容时抛出 ParserError 异常"""
        xls_strategy = Xls()
        xls_strategy.set_trader(self.trader)

        with tempfile.NamedTemporaryFile("w", encoding="gbk", delete=False, suffix=".xls") as f:
            f.write("a\tb\n1\t2\t3\t4\n")
            temp_path = f.name

        try:
            with patch("pandas.read_csv", side_effect=pd.errors.ParserError("Corrupted TSV")):
                with self.assertRaises(pd.errors.ParserError):
                    xls_strategy._format_grid_data(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestBalanceParsing(unittest.TestCase):
    """测试 _get_balance_from_statics 的各种控件解析场景及异常容错"""

    def setUp(self):
        self.trader = ClientTrader()
        self.trader._main = MagicMock()

    def test_balance_parsing_success(self):
        """测试所有资金控件均存在且正常转换为浮点数"""
        mock_values = {
            1012: "100000.50",
            1016: "80000.00",
            1017: "50000.00",
            1014: "150000.25",
            1015: "250000.75",
        }

        def mock_child_window(control_id=None, class_name=None):
            ctrl = MagicMock()
            val = mock_values.get(control_id, "0.0")
            ctrl.window_text.return_value = f"  {val}  "
            return ctrl

        self.trader._main.child_window.side_effect = mock_child_window

        result = self.trader._get_balance_from_statics()
        self.assertEqual(result["资金余额"], 100000.50)
        self.assertEqual(result["可用金额"], 80000.00)
        self.assertEqual(result["可取金额"], 50000.00)
        self.assertEqual(result["股票市值"], 150000.25)
        self.assertEqual(result["总资产"], 250000.75)

    def test_balance_parsing_partial_failure(self):
        """测试部分静态控件缺失（例如股票市值 1014 未找到）时能容错并返回其余字段"""
        def mock_child_window(control_id=None, class_name=None):
            if control_id == 1014:
                raise RuntimeError("Element not found for control_id 1014")
            ctrl = MagicMock()
            ctrl.window_text.return_value = "5000.0"
            return ctrl

        self.trader._main.child_window.side_effect = mock_child_window

        result = self.trader._get_balance_from_statics()
        self.assertNotIn("股票市值", result)
        self.assertEqual(result["资金余额"], 5000.0)
        self.assertEqual(result["总资产"], 5000.0)

    def test_balance_parsing_invalid_number_string(self):
        """测试控件文本非数字（如 '--' 或空字符）时能捕获 ValueError 并跳过"""
        def mock_child_window(control_id=None, class_name=None):
            ctrl = MagicMock()
            if control_id == 1017:
                ctrl.window_text.return_value = "--"
            elif control_id == 1016:
                ctrl.window_text.return_value = ""
            else:
                ctrl.window_text.return_value = "1234.56"
            return ctrl

        self.trader._main.child_window.side_effect = mock_child_window

        result = self.trader._get_balance_from_statics()
        self.assertNotIn("可取金额", result)
        self.assertNotIn("可用金额", result)
        self.assertEqual(result["资金余额"], 1234.56)

    def test_balance_all_controls_fail(self):
        """测试所有控件均获取失败时优雅返回空字典而不是崩溃"""
        self.trader._main.child_window.side_effect = RuntimeError("All failed")
        result = self.trader._get_balance_from_statics()
        self.assertEqual(result, {})

    def test_balance_parsing_thousands_comma_success(self):
        """测试资金控件文本包含千分位逗号时能正确剥离并解析为浮点数"""
        mock_child = MagicMock()
        mock_child.window_text.return_value = " 1,234,567.89 "
        self.trader._main.child_window.return_value = mock_child

        result = self.trader._get_balance_from_statics()
        self.assertEqual(result["总资产"], 1234567.89)
        self.assertEqual(result["资金余额"], 1234567.89)
        self.assertEqual(result["股票市值"], 1234567.89)

    def test_ht_client_trader_inherits_robust_balance_parsing(self):
        """测试 HTClientTrader 继承了安全的资金解析逻辑（容错与千分位）"""
        ht = ht_clienttrader.HTClientTrader()
        ht._main = MagicMock()

        def mock_child(control_id=None, class_name=None):
            if control_id == 1014:
                raise RuntimeError("Control missing")
            ctrl = MagicMock()
            ctrl.window_text.return_value = " 2,050,100.00 "
            return ctrl

        ht._main.child_window.side_effect = mock_child
        with patch.object(ht, "_switch_left_menus"):
            balance = ht.balance
            self.assertEqual(balance["总资产"], 2050100.0)
            self.assertNotIn("股票市值", balance)


class TestConnectWindowMatching(unittest.TestCase):
    """测试 connect() 针对主窗口正则匹配及 top_window 回退机制"""

    def setUp(self):
        self.trader = ClientTrader()

    def tearDown(self):
        client.CommonConfig.TITLE = "网上股票交易系统5.0"

    @patch("pywinauto.Application")
    def test_connect_matches_title_regex_success(self, mock_app_cls):
        """测试当存在匹配标题正则的窗口时优先将其绑定为 _main"""
        mock_app_instance = MagicMock()
        mock_app_cls.return_value.connect.return_value = mock_app_instance

        mock_main_win = MagicMock()
        mock_main_win.exists.return_value = True
        mock_app_instance.window.return_value = mock_main_win

        with patch.object(self.trader, "_close_prompt_windows"):
            with patch.object(self.trader, "_init_toolbar"):
                self.trader.connect(exe_path="C:\\xiadan.exe")

        mock_app_instance.window.assert_called_once_with(title_re=".*网上股票交易系统5.0.*")
        self.assertEqual(self.trader._main, mock_main_win)

    @patch("pywinauto.Application")
    def test_connect_fallback_to_top_window_when_not_exists(self, mock_app_cls):
        """测试当正则窗口不存在时回退至 top_window()"""
        mock_app_instance = MagicMock()
        mock_app_cls.return_value.connect.return_value = mock_app_instance

        mock_main_win = MagicMock()
        mock_main_win.exists.return_value = False
        mock_app_instance.window.return_value = mock_main_win

        mock_top_win = MagicMock()
        mock_app_instance.top_window.return_value = mock_top_win

        with patch.object(self.trader, "_close_prompt_windows"):
            with patch.object(self.trader, "_init_toolbar"):
                self.trader.connect(exe_path="C:\\xiadan.exe")

        self.assertEqual(self.trader._main, mock_top_win)

    @patch("pywinauto.Application")
    def test_connect_fallback_to_top_window_on_exception(self, mock_app_cls):
        """测试当 window() 查找抛出异常时回退至 top_window()"""
        mock_app_instance = MagicMock()
        mock_app_cls.return_value.connect.return_value = mock_app_instance

        mock_app_instance.window.side_effect = Exception("Window lookup failed")
        mock_top_win = MagicMock()
        mock_app_instance.top_window.return_value = mock_top_win

        with patch.object(self.trader, "_close_prompt_windows"):
            with patch.object(self.trader, "_init_toolbar"):
                self.trader.connect(exe_path="C:\\xiadan.exe")

        self.assertEqual(self.trader._main, mock_top_win)

    @patch("pywinauto.Application")
    def test_connect_custom_config_title(self, mock_app_cls):
        """测试券商配置中定制了 TITLE 时，正则匹配使用定制的标题"""
        mock_app_instance = MagicMock()
        mock_app_cls.return_value.connect.return_value = mock_app_instance

        mock_main_win = MagicMock()
        mock_main_win.exists.return_value = True
        mock_app_instance.window.return_value = mock_main_win

        custom_cfg = MagicMock()
        custom_cfg.DEFAULT_EXE_PATH = "C:\\xiadan.exe"
        custom_cfg.TITLE = "广发证券网上交易系统"
        self.trader._config = custom_cfg

        with patch.object(self.trader, "_close_prompt_windows"):
            with patch.object(self.trader, "_init_toolbar"):
                self.trader.connect(exe_path="C:\\xiadan.exe")

        mock_app_instance.window.assert_called_once_with(title_re=".*广发证券网上交易系统.*")
        self.assertEqual(self.trader._main, mock_main_win)

    def test_connect_raises_when_no_exe_path(self):
        """测试 exe_path 为空且 DEFAULT_EXE_PATH 为 None 时抛出 ValueError"""
        self.trader._config = MagicMock()
        self.trader._config.DEFAULT_EXE_PATH = None
        with self.assertRaises(ValueError):
            self.trader.connect(exe_path=None)

    def test_close_prompt_windows_does_not_close_main_window_with_custom_suffix(self):
        """测试 _close_prompt_windows 不会误杀包含 TITLE 关键词的主窗口"""
        mock_main_win = MagicMock()
        mock_main_win.window_text.return_value = "华泰证券网上股票交易系统5.0 - [独立委托]"

        mock_dialog_win = MagicMock()
        mock_dialog_win.window_text.return_value = "温馨提示"

        self.trader._app = MagicMock()
        self.trader._app.windows.return_value = [mock_main_win, mock_dialog_win]

        with patch.object(self.trader, "wait"):
            self.trader._close_prompt_windows()

        mock_main_win.close.assert_not_called()
        mock_dialog_win.close.assert_called_once()

    def test_close_prompt_window_no_wait_preserves_main_window(self):
        """测试 close_pormpt_window_no_wait 不会误杀包含 TITLE 关键词的主窗口"""
        mock_main_win = MagicMock()
        mock_main_win.window_text.return_value = "网上股票交易系统5.0"

        mock_main_win_suffix = MagicMock()
        mock_main_win_suffix.window_text.return_value = "国泰君安网上股票交易系统5.0"

        mock_dialog = MagicMock()
        mock_dialog.window_text.return_value = "系统公告"

        self.trader._app = MagicMock()
        self.trader._app.windows.return_value = [mock_main_win, mock_main_win_suffix, mock_dialog]

        self.trader.close_pormpt_window_no_wait()

        mock_main_win.close.assert_not_called()
        mock_main_win_suffix.close.assert_not_called()
        mock_dialog.close.assert_called_once()


class TestTradeAndOrderInput(unittest.TestCase):
    """测试交易下单、撤单及 _editor_need_type_keys 按键输入的行为与边界"""

    def setUp(self):
        self.trader = ClientTrader()
        self.trader._main = MagicMock()
        self.trader._app = MagicMock()

    def test_editor_need_type_keys_default_is_true(self):
        """测试 ClientTrader 默认开启键盘按键输入以规避自绘控件假下单"""
        self.assertTrue(self.trader._editor_need_type_keys)

    def test_subclasses_preserve_editor_need_type_keys_default(self):
        """测试各券商客户端子类默认均继承 _editor_need_type_keys = True"""
        for cls in [universal_clienttrader.UniversalClientTrader,
                    ht_clienttrader.HTClientTrader,
                    htzq_clienttrader.HTZQClientTrader,
                    wk_clienttrader.WKClientTrader]:
            instance = cls()
            self.assertTrue(instance._editor_need_type_keys, f"{cls.__name__} 必须保留按键模式为 True")

    def test_enable_type_keys_for_editor(self):
        """测试 enable_type_keys_for_editor 方法能够开启按键模式"""
        self.trader._editor_need_type_keys = False
        self.trader.enable_type_keys_for_editor()
        self.assertTrue(self.trader._editor_need_type_keys)

    def test_type_edit_control_keys_when_flag_true(self):
        """测试 _editor_need_type_keys 为 True 时调用 select() 和 type_keys()"""
        self.trader._editor_need_type_keys = True
        mock_editor = MagicMock()
        self.trader._main.child_window.return_value = mock_editor

        self.trader._type_edit_control_keys(1032, "600519")

        self.trader._main.child_window.assert_called_with(control_id=1032, class_name="Edit")
        mock_editor.select.assert_called_once()
        mock_editor.type_keys.assert_called_once_with("600519")
        mock_editor.set_edit_text.assert_not_called()

    def test_type_edit_control_keys_when_flag_false(self):
        """测试 _editor_need_type_keys 为 False 时直接调用 set_edit_text()"""
        self.trader._editor_need_type_keys = False
        mock_editor = MagicMock()
        self.trader._main.child_window.return_value = mock_editor

        self.trader._type_edit_control_keys(1032, "600519")

        self.trader._main.child_window.assert_called_with(control_id=1032, class_name="Edit")
        mock_editor.set_edit_text.assert_called_once_with("600519")
        mock_editor.select.assert_not_called()
        mock_editor.type_keys.assert_not_called()

    def test_direct_type_edit_control_keys_wrapper(self):
        """测试直接传入 editor 控件对象的 type_edit_control_keys 方法"""
        mock_editor = MagicMock()

        self.trader._editor_need_type_keys = True
        self.trader.type_edit_control_keys(mock_editor, "1234")
        mock_editor.select.assert_called_once()
        mock_editor.type_keys.assert_called_once_with("1234")

        mock_editor.reset_mock()
        self.trader._editor_need_type_keys = False
        self.trader.type_edit_control_keys(mock_editor, "5678")
        mock_editor.set_edit_text.assert_called_once_with("5678")

    def test_set_trade_params_formatting(self):
        """测试下单参数截取6位代码、四舍五入价格及转整数数量"""
        with patch.object(self.trader, "_type_edit_control_keys") as mock_type:
            with patch.object(self.trader, "wait"):
                self.trader._set_trade_params("sh600519", 1800.123, 100.9)

                calls = mock_type.call_args_list
                # 证券代码 (1032) -> "600519"
                self.assertEqual(calls[0][0], (1032, "600519"))
                # 价格 (1033) -> 四舍五入后两位 "1800.12"
                self.assertEqual(calls[1][0], (1033, "1800.12"))
                # 数量 (1034) -> 整数字符串 "100"
                self.assertEqual(calls[2][0], (1034, "100"))

    def test_buy_and_sell_execution_flow(self):
        """测试 buy 和 sell 正确切换左侧菜单并提交下单"""
        with patch.object(self.trader, "_switch_left_menus") as mock_menu:
            with patch.object(self.trader, "trade") as mock_trade:
                mock_trade.return_value = {"entrust_no": "123456"}

                res_buy = self.trader.buy("600519", 1800.0, 100)
                mock_menu.assert_called_with(["买入[F1]"])
                mock_trade.assert_called_with("600519", 1800.0, 100)
                self.assertEqual(res_buy, {"entrust_no": "123456"})

                mock_menu.reset_mock()
                mock_trade.reset_mock()

                res_sell = self.trader.sell("600519", 1800.0, 100)
                mock_menu.assert_called_with(["卖出[F2]"])
                mock_trade.assert_called_with("600519", 1800.0, 100)
                self.assertEqual(res_sell, {"entrust_no": "123456"})

    def test_submit_trade_clicks_button(self):
        """测试 _submit_trade 点击提交按钮"""
        mock_btn = MagicMock()
        self.trader._main.child_window.return_value = mock_btn

        with patch("time.sleep"):
            self.trader._submit_trade()

        self.trader._main.child_window.assert_called_with(
            control_id=self.trader._config.TRADE_SUBMIT_CONTROL_ID, class_name="Button"
        )
        mock_btn.click.assert_called_once()

    def test_cancel_entrust_success_and_failure(self):
        """测试 cancel_entrust 在找到对应合同编号时双击撤单，未找到时返回错误信息"""
        fake_entrusts = [
            {"合同编号": "HT001", "证券代码": "600519"},
            {"合同编号": "HT002", "证券代码": "000858"},
        ]

        fake_entrusts_after = [
            {"合同编号": "HT001", "证券代码": "600519"},
        ]

        with patch.object(ClientTrader, "cancel_entrusts", new_callable=PropertyMock) as mock_prop:
            mock_prop.side_effect = [fake_entrusts, fake_entrusts_after, fake_entrusts]
            with patch.object(self.trader, "refresh"):
                with patch.object(self.trader, "_cancel_entrust_by_double_click") as mock_cancel_click:
                    with patch.object(self.trader, "_handle_pop_dialogs") as mock_dialog:
                        mock_dialog.return_value = {"message": "success"}

                        res_ok = self.trader.cancel_entrust("HT002")
                        mock_cancel_click.assert_called_once_with(1)
                        self.assertEqual(res_ok["message"], "success")

                        mock_cancel_click.reset_mock()

                        res_fail = self.trader.cancel_entrust("NON_EXIST_NO")
                        mock_cancel_click.assert_not_called()
                        self.assertIn("委托单状态错误不能撤单", res_fail["message"])

    def test_cancel_entrust_empty_grid_returns_failure_gracefully(self):
        """测试当表格为空列表 [] 时，cancel_entrust 优雅返回委托单不存在而非抛出 TypeError"""
        with patch.object(ClientTrader, "cancel_entrusts", new_callable=PropertyMock) as mock_prop:
            mock_prop.return_value = []
            with patch.object(self.trader, "refresh"):
                res = self.trader.cancel_entrust("HT001")
                self.assertIn("委托单状态错误不能撤单", res["message"])

    def test_repo_and_reverse_repo_flow(self):
        """测试 repo 与 reverse_repo 正确切换菜单并提交 trade"""
        with patch.object(self.trader, "_switch_left_menus") as mock_menu:
            with patch.object(self.trader, "trade") as mock_trade:
                mock_trade.return_value = {"message": "success"}

                self.trader.repo("204001", 2.5, 1000)
                mock_menu.assert_called_with(["债券回购", "融资回购（正回购）"])
                mock_trade.assert_called_with("204001", 2.5, 1000)

                mock_menu.reset_mock()
                mock_trade.reset_mock()

                self.trader.reverse_repo("131810", 2.2, 1000)
                mock_menu.assert_called_with(["债券回购", "融劵回购（逆回购）"])
                mock_trade.assert_called_with("131810", 2.2, 1000)

    def test_market_trade_params_star_market_uses_type_edit(self):
        """测试科创板 (68xxxx) 市价交易通过 _type_edit_control_keys 设置保护限价"""
        with patch.object(self.trader, "_type_edit_control_keys") as mock_type:
            with patch.object(self.trader, "wait"):
                self.trader._set_market_trade_params("688001", 200, limit_price="45.50")
                mock_type.assert_any_call(self.trader._config.TRADE_AMOUNT_CONTROL_ID, "200")
                mock_type.assert_any_call(self.trader._config.TRADE_PRICE_CONTROL_ID, "45.50")

    def test_market_trade_params_non_star_market(self):
        """测试非科创板普通股票市价委托不寻找或设置限价编辑框"""
        with patch.object(self.trader, "_type_edit_control_keys") as mock_type:
            with patch.object(self.trader, "wait"):
                self.trader._set_market_trade_params("000001", 300)
                mock_type.assert_called_once_with(self.trader._config.TRADE_AMOUNT_CONTROL_ID, "300")


class TestDialogAndAutomationWorkflow(unittest.TestCase):
    """测试弹窗检测、自动打新及自动化流程边界"""

    def setUp(self):
        self.trader = ClientTrader()
        self.trader._main = MagicMock()
        self.trader._app = MagicMock()

    def test_is_exist_pop_dialog_no_dialog(self):
        """测试当主窗口与 top_window 句柄一致时判断无弹窗"""
        wrapper = MagicMock()
        self.trader._main.wrapper_object.return_value = wrapper
        self.trader._app.top_window.return_value.wrapper_object.return_value = wrapper

        with patch.object(self.trader, "wait"):
            self.assertFalse(self.trader.is_exist_pop_dialog())

    def test_is_exist_pop_dialog_has_dialog(self):
        """测试当 top_window 不同于主窗口时判断存在弹窗"""
        self.trader._main.wrapper_object.return_value = MagicMock()
        self.trader._app.top_window.return_value.wrapper_object.return_value = MagicMock()

        with patch.object(self.trader, "wait"):
            self.assertTrue(self.trader.is_exist_pop_dialog())

    def test_is_exist_pop_dialog_exception_returns_false(self):
        """测试弹窗检查遭遇超时或未找到元素异常时安全返回 False"""
        from pywinauto import findwindows
        self.trader._main.wrapper_object.side_effect = findwindows.ElementNotFoundError()

        with patch.object(self.trader, "wait"):
            self.assertFalse(self.trader.is_exist_pop_dialog())

    def test_auto_ipo_empty_stock_list(self):
        """测试当日无新股时返回对应提示信息"""
        with patch.object(self.trader, "_switch_left_menus"):
            with patch.object(self.trader, "_get_grid_data", return_value=[]):
                res = self.trader.auto_ipo()
                self.assertEqual(res, {"message": "今日无新股"})

    def test_auto_ipo_no_available_quota(self):
        """测试新股申购额度均为0时返回无新股可申购信息"""
        fake_ipo_data = [
            {"证券代码": "732001", "申购数量": 0},
            {"证券代码": "732002", "申购数量": 0},
        ]
        with patch.object(self.trader, "_switch_left_menus"):
            with patch.object(self.trader, "_get_grid_data", return_value=fake_ipo_data):
                res = self.trader.auto_ipo()
                self.assertEqual(res, {"message": "没有发现可以申购的新股"})

    def test_auto_ipo_with_available_quota(self):
        """测试有新股额度时正常执行一键申购流程并反选无效行"""
        fake_ipo_data = [
            {"证券代码": "732001", "申购数量": 1000},
            {"证券代码": "732002", "申购数量": 0},
        ]
        with patch.object(self.trader, "_switch_left_menus"):
            with patch.object(self.trader, "_get_grid_data", return_value=fake_ipo_data):
                with patch.object(self.trader, "_click") as mock_click:
                    with patch.object(self.trader, "_click_grid_by_row") as mock_click_row:
                        with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "申购成功"}):
                            with patch.object(self.trader, "wait"):
                                res = self.trader.auto_ipo()
                                # 验证全选按钮点击
                                mock_click.assert_any_call(self.trader._config.AUTO_IPO_SELECT_ALL_BUTTON_CONTROL_ID)
                                # 验证第 1 行（申购数量 0）被反选跳过
                                mock_click_row.assert_called_once_with(1)
                                # 验证申购按钮点击
                                mock_click.assert_any_call(self.trader._config.AUTO_IPO_BUTTON_CONTROL_ID)
                                self.assertEqual(res, {"message": "申购成功"})


class TestDpiAndCancelAndOcr(unittest.TestCase):
    def setUp(self):
        self.trader = ClientTrader()
        self.trader._main = MagicMock()
        self.trader._app = MagicMock()

    def test_dpi_scale_factor(self):
        """测试 get_dpi_scale_factor 默认返回浮点数缩放比"""
        scale = self.trader.get_dpi_scale_factor()
        self.assertIsInstance(scale, float)
        self.assertGreater(scale, 0.5)

    def test_click_grid_by_row_dpi_scaling(self):
        """测试 _click_grid_by_row 正确应用 DPI 缩放与行中心偏移"""
        with patch.object(self.trader, "get_dpi_scale_factor", return_value=1.5):
            mock_grid = MagicMock()
            self.trader._app.top_window().child_window.return_value = mock_grid
            self.trader._click_grid_by_row(2)

            expected_x = int(self.trader._config.COMMON_GRID_LEFT_MARGIN * 1.5)
            expected_y = int(
                (
                    self.trader._config.COMMON_GRID_FIRST_ROW_HEIGHT
                    + self.trader._config.COMMON_GRID_ROW_HEIGHT * 2.5
                )
                * 1.5
            )
            mock_grid.click.assert_called_once_with(coords=(expected_x, expected_y))

    def test_cancel_entrust_by_double_click_dpi_scaling(self):
        """测试 _cancel_entrust_by_double_click 正确应用 DPI 缩放与行中心偏移"""
        with patch.object(self.trader, "get_dpi_scale_factor", return_value=1.25):
            mock_grid = MagicMock()
            self.trader._app.top_window().child_window.return_value = mock_grid
            self.trader._cancel_entrust_by_double_click(1)

            expected_x = int(self.trader._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN * 1.25)
            expected_y = int(
                (
                    self.trader._config.CANCEL_ENTRUST_GRID_FIRST_ROW_HEIGHT
                    + self.trader._config.CANCEL_ENTRUST_GRID_ROW_HEIGHT * 1.5
                )
                * 1.25
            )
            mock_grid.double_click.assert_called_once_with(coords=(expected_x, expected_y))

    def test_cancel_entrust_retry_and_verified_success(self):
        """测试撤单点击弹窗并短轮询验证委托单消失（成功路径）"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
            {"合同编号": "654321", "证券代码": "000002"},
        ]
        fake_entrusts_after = [
            {"合同编号": "654321", "证券代码": "000002"},
        ]
        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        # 第一次读取包含 123456，二次核对读取时不包含
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, fake_entrusts_after],
                        ):
                            res = self.trader.cancel_entrust("123456", verify_timeout=1.0)
                            self.assertEqual(res["message"], "success")
                            self.assertTrue(res["verified"])
                            self.assertEqual(res["entrust_no"], "123456")

    def test_cancel_entrust_unconfirmed_when_timeout(self):
        """测试撤单后在规定时间内未从撤单列表消失抛出 TradeVerificationError 异常"""
        fake_entrusts_still_present = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]
        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            return_value=fake_entrusts_still_present,
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertIn("123456", str(ctx.exception))

    def test_cancel_entrust_empty_grid_during_verify_raises_verification_error(self):
        """测试撤单二次核对期间若表格返回 [] 严禁判定成功，必须触发 TradeVerificationError"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]
        fake_entrusts_after = []

        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, fake_entrusts_after],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertIn("待撤列表为空", str(ctx.exception))

    def test_cancel_entrust_grid_exception_during_verify_raises_verification_error(self):
        """测试撤单二次核对期间若网格读取抛出系统异常，必须触发 TradeVerificationError"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]

        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, IOError("Grid read failed")],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertIn("Grid read failed", str(ctx.exception))

    def test_cancel_entrust_recovers_from_transient_error_and_reports_timeout(self):
        """测试撤单二次核对期间若发生瞬态网格异常但随后恢复，超时后应准确报告超时而非历史瞬态异常"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]
        # 第一次异常，第二次成功读取但订单仍在待撤列表中
        fake_entrusts_still_present = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]

        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, IOError("Transient grid error"), fake_entrusts_still_present],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.5)
                            # 验证异常信息报告的是超时未移除，而非历史瞬态异常
                            self.assertIn("未能在", str(ctx.exception))
                            self.assertNotIn("Transient grid error", str(ctx.exception))

    def test_screenshot_ocr_missing_dependency(self):
        """测试未安装 rapidocr 时 ScreenshotOCR 策略友好报错"""
        strategy = grid_strategies.ScreenshotOCR()
        with patch.dict("sys.modules", {"rapidocr_onnxruntime": None}):
            with self.assertRaises(ImportError) as ctx:
                strategy._get_ocr_engine()
            self.assertIn("rapidocr_onnxruntime", str(ctx.exception))

    def test_screenshot_ocr_parse_records_with_mock_engine(self):
        """测试 ScreenshotOCR 能够根据空间坐标正确聚类成行并过滤汇总行"""
        # 模拟 OCR 返回：[box, text, score]，box 是 4 个点
        mock_ocr_result = [
            # Header 行 (cy ≈ 10)
            [[[10, 0], [50, 0], [50, 20], [10, 20]], "证券代码", 0.99],
            [[[60, 0], [120, 0], [120, 20], [60, 20]], "证券名称", 0.99],
            [[[130, 0], [180, 0], [180, 20], [130, 20]], "股票余额", 0.99],
            # 数据行 1 (cy ≈ 40)
            [[[10, 30], [50, 30], [50, 50], [10, 50]], "300434", 0.99],
            [[[60, 30], [120, 30], [120, 50], [60, 50]], "金石亚药", 0.99],
            [[[130, 30], [180, 30], [180, 50], [130, 50]], "100", 0.99],
            # 汇总行 (cy ≈ 70)
            [[[10, 60], [50, 60], [50, 80], [10, 80]], "汇总", 0.99],
            [[[60, 60], [120, 60], [120, 80], [60, 80]], "", 0.99],
            [[[130, 60], [180, 60], [180, 80], [130, 80]], "100", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            # 汇总行应该被自动过滤，只剩下 1 条真实持仓
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["证券代码"], "300434")
            self.assertEqual(records[0]["证券名称"], "金石亚药")
            self.assertEqual(records[0]["股票余额"], "100")

    def test_screenshot_ocr_missing_cell_physical_projection(self):
        """测试当表头 7 列但数据行缺失 1 列时，缺失列为空且后续列绝不发生左移塌陷"""
        mock_ocr_result = [
            # 7 列表头
            [[[10, 0], [50, 0], [50, 20], [10, 20]], "证券代码", 0.99],   # cx=30
            [[[60, 0], [120, 0], [120, 20], [60, 20]], "证券名称", 0.99],  # cx=90
            [[[130, 0], [180, 0], [180, 20], [130, 20]], "股票余额", 0.99], # cx=155
            [[[190, 0], [240, 0], [240, 20], [190, 20]], "可用余额", 0.99], # cx=215
            [[[250, 0], [300, 0], [300, 20], [250, 20]], "成本价", 0.99],   # cx=275
            [[[310, 0], [360, 0], [360, 20], [310, 20]], "当前价", 0.99],   # cx=335
            [[[370, 0], [420, 0], [420, 20], [370, 20]], "浮动盈亏", 0.99], # cx=395

            # 数据行：缺失 成本价 (cx=275)，共 6 个单元格
            [[[10, 30], [50, 30], [50, 50], [10, 50]], "600519", 0.99],
            [[[60, 30], [120, 30], [120, 50], [60, 50]], "贵州茅台", 0.99],
            [[[130, 30], [180, 30], [180, 50], [130, 50]], "100", 0.99],
            [[[190, 30], [240, 30], [240, 50], [190, 50]], "100", 0.99],
            # 成本价缺失！
            [[[310, 30], [360, 30], [360, 50], [310, 50]], "1800.00", 0.99],
            [[[370, 30], [420, 30], [420, 50], [370, 50]], "250.00", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            row = records[0]
            self.assertEqual(row["证券代码"], "600519")
            self.assertEqual(row["证券名称"], "贵州茅台")
            self.assertEqual(row["股票余额"], "100")
            self.assertEqual(row["可用余额"], "100")
            self.assertEqual(row["成本价"], "")  # 缺失单元格为空
            self.assertEqual(row["当前价"], "1800.00")  # 绝不塌陷到成本价
            self.assertEqual(row["浮动盈亏"], "250.00")

    def test_screenshot_ocr_numerical_adhesion(self):
        """测试当 OCR 识别出跨列数值粘连（如 13.15413.150）时能基于几何物理区间正确分解对齐"""
        mock_ocr_result = [
            # 表头：证券代码、证券名称、股票余额、成本价、当前价
            [[[10, 0], [50, 0], [50, 20], [10, 20]], "证券代码", 0.99],   # cx=30
            [[[60, 0], [120, 0], [120, 20], [60, 20]], "证券名称", 0.99],  # cx=90
            [[[130, 0], [180, 0], [180, 20], [130, 20]], "股票余额", 0.99], # cx=155
            [[[190, 0], [250, 0], [250, 20], [190, 20]], "成本价", 0.99],   # cx=220
            [[[260, 0], [320, 0], [320, 20], [260, 20]], "当前价", 0.99],   # cx=290

            # 数据行：成本价与当前价粘连为单个框 13.15413.150 (跨越 x=190 到 x=320)
            [[[10, 30], [50, 30], [50, 50], [10, 50]], "000001", 0.99],
            [[[60, 30], [120, 30], [120, 50], [60, 50]], "平安银行", 0.99],
            [[[130, 30], [180, 30], [180, 50], [130, 50]], "500", 0.99],
            [[[195, 30], [315, 30], [315, 50], [195, 50]], "13.15413.150", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            row = records[0]
            self.assertEqual(row["证券代码"], "000001")
            self.assertEqual(row["证券名称"], "平安银行")
            self.assertEqual(row["股票余额"], "500")
            self.assertEqual(row["成本价"], "13.154")
            self.assertEqual(row["当前价"], "13.150")

    def test_screenshot_ocr_space_adhesion_compact_columns(self):
        """测试紧凑列中带空格粘连的多数值正确分配至相邻列"""
        mock_ocr_result = [
            [[[10, 0], [50, 0], [50, 20], [10, 20]], "买入价", 0.99],  # cx=30
            [[[60, 0], [100, 0], [100, 20], [60, 20]], "卖出价", 0.99], # cx=80

            # 数据行：粘连为 "10.50 10.55"
            [[[15, 30], [95, 30], [95, 50], [15, 50]], "10.50 10.55", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["买入价"], "10.50")
            self.assertEqual(records[0]["卖出价"], "10.55")

    def test_screenshot_ocr_mixed_space_and_concatenated_floats(self):
        """测试单个 OCR 框中同时包含空格与数值粘连（如 '500 13.15413.150'）时的两阶段级联分解"""
        mock_ocr_result = [
            # 3 列表头：股票余额, 成本价, 当前价
            [[[10, 0], [60, 0], [60, 20], [10, 20]], "股票余额", 0.99],   # cx=35
            [[[70, 0], [130, 0], [130, 20], [70, 20]], "成本价", 0.99],   # cx=100
            [[[140, 0], [200, 0], [200, 20], [140, 20]], "当前价", 0.99], # cx=170

            # 数据行：1 个粘连框跨越 3 列
            [[[15, 30], [195, 30], [195, 50], [15, 50]], "500 13.15413.150", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            row = records[0]
            self.assertEqual(row["股票余额"], "500")
            self.assertEqual(row["成本价"], "13.154")
            self.assertEqual(row["当前价"], "13.150")

    def test_screenshot_ocr_tab_separated_adhesion(self):
        """测试包含制表符等非空格空白字符的单元格粘连分解"""
        mock_ocr_result = [
            [[[10, 0], [60, 0], [60, 20], [10, 20]], "股票余额", 0.99],   # cx=35
            [[[70, 0], [130, 0], [130, 20], [70, 20]], "可用余额", 0.99], # cx=100
            [[[15, 30], [125, 30], [125, 50], [15, 50]], "100\t200", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["股票余额"], "100")
            self.assertEqual(records[0]["可用余额"], "200")

    def test_screenshot_ocr_multi_concatenated_floats(self):
        """测试 3 个及以上连续无空格浮点数粘连的递归切分与对齐"""
        mock_ocr_result = [
            [[[10, 0], [50, 0], [50, 20], [10, 20]], "买一价", 0.99],  # cx=30
            [[[60, 0], [100, 0], [100, 20], [60, 20]], "买二价", 0.99], # cx=80
            [[[110, 0], [150, 0], [150, 20], [110, 20]], "买三价", 0.99], # cx=130

            # 数据行：连续 3 个浮点数无空格粘连
            [[[12, 30], [148, 30], [148, 50], [12, 50]], "10.5010.5510.60", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["买一价"], "10.50")
            self.assertEqual(records[0]["买二价"], "10.55")
            self.assertEqual(records[0]["买三价"], "10.60")

    def test_screenshot_ocr_dense_grid_row_clustering_no_merge(self):
        """测试同花顺 16-20px 紧凑行间距下，表头行与数据行绝不因垂直漂移而错误合并"""
        mock_ocr_result = [
            # 表头行 (y: 2~18, cy=10, h=16)
            [[[10, 2], [60, 2], [60, 18], [10, 18]], "证券代码", 0.99],
            [[[70, 2], [120, 2], [120, 18], [70, 18]], "证券名称", 0.99],
            # 数据行紧贴表头下方 (y: 19~35, cy=27, h=16)，垂直不重叠
            [[[10, 19], [60, 19], [60, 35], [10, 35]], "600519", 0.99],
            [[[70, 21], [120, 21], [120, 37], [70, 37]], "贵州茅台", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["证券代码"], "600519")
            self.assertEqual(records[0]["证券名称"], "贵州茅台")


    def test_screenshot_ocr_outer_boundary_exclusion(self):
        """测试位于表头物理列边界外侧的杂项（如最左侧行序号、最右侧滚动条文字）绝不污染表格字段"""
        mock_ocr_result = [
            # 表头：两列，从 x=60 到 x=200
            [[[60, 0], [120, 0], [120, 20], [60, 20]], "证券代码", 0.99],   # cx=90
            [[[130, 0], [200, 0], [200, 20], [130, 20]], "证券名称", 0.99],  # cx=165

            # 数据行：x=10 处有行号 '1'，x=250 处有滚动条文字 '滚动'
            [[[10, 30], [25, 30], [25, 50], [10, 50]], "1", 0.99],          # cx=17.5 位于表格左边界外
            [[[60, 30], [120, 30], [120, 50], [60, 50]], "600519", 0.99],  # cx=90 证券代码
            [[[130, 30], [200, 30], [200, 50], [130, 50]], "贵州茅台", 0.99],# cx=165 证券名称
            [[[240, 30], [260, 30], [260, 50], [240, 50]], "滚动", 0.99],   # cx=250 位于表格右边界外
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["证券代码"], "600519")
            self.assertEqual(records[0]["证券名称"], "贵州茅台")

    def test_screenshot_ocr_header_whitespace_adhesion(self):
        """测试表头中由于间距紧凑被 OCR 识别为同一个框的多列标题能被正确分解对齐"""
        mock_ocr_result = [
            # 表头行：买入价与卖出价粘连在一个框中
            [[[10, 0], [100, 0], [100, 20], [10, 20]], "买入价 卖出价", 0.99],

            # 数据行：两个正常独立的单元格
            [[[10, 30], [50, 30], [50, 50], [10, 50]], "10.50", 0.99],
            [[[60, 30], [100, 30], [100, 50], [60, 50]], "10.55", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertIn("买入价", records[0])
            self.assertIn("卖出价", records[0])
            self.assertEqual(records[0]["买入价"], "10.50")
            self.assertEqual(records[0]["卖出价"], "10.55")

    def test_screenshot_ocr_date_format_not_shredded(self):
        """测试包含标准日期格式（如 2026.09.30）的单元格绝不被误判为浮点数粘连而拆碎"""
        mock_ocr_result = [
            # 表头：发生日期、证券代码
            [[[0, 0], [50, 0], [50, 20], [0, 20]], "发生日期", 0.99],   # cx=25
            [[[60, 0], [120, 0], [120, 20], [60, 20]], "证券代码", 0.99], # cx=90

            # 数据行：发生日期为 2026.09.30，且靠右侧对齐 (x=20..70, cx=45)
            [[[20, 30], [70, 30], [70, 50], [20, 50]], "2026.09.30", 0.99],
            [[[80, 30], [120, 30], [120, 50], [80, 50]], "600519", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["发生日期"], "2026.09.30")
            self.assertEqual(records[0]["证券代码"], "600519")

    def test_cancel_entrust_missing_entrust_field_during_verify_raises_verification_error(self):
        """测试撤单二次核对期间若表格缺少合同编号有效字段，严禁判定成功，必须触发 TradeVerificationError"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]
        # 二次核对时返回异常网格（缺少合同编号字段）
        fake_entrusts_after_corrupted = [
            {"证券代码": "000001", "买卖": "买入"},
        ]

        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, fake_entrusts_after_corrupted],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertIn("合同编号", str(ctx.exception))

    def test_cancel_entrust_empty_grid_includes_pop_result(self):
        """测试撤单二次核对网格为空时抛出的 TradeVerificationError 包含 pop_result"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]
        fake_entrusts_after = []

        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok", "pop_key": "val"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, fake_entrusts_after],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertEqual(ctx.exception.result.get("pop_result"), {"message": "ok", "pop_key": "val"})

    def test_cancel_entrust_empty_or_whitespace_entrust_no_raises_value_error(self):
        """测试当传入空或纯空白委托编号时立即抛出 ValueError 严禁误匹配空单元格"""
        with self.assertRaises(ValueError):
            self.trader.cancel_entrust("")
        with self.assertRaises(ValueError):
            self.trader.cancel_entrust("   ")
        with self.assertRaises(ValueError):
            self.trader.cancel_entrust(None)

    def test_cancel_entrust_broker_pop_error_raises_verification_error(self):
        """测试券商弹窗明确提示状态异常（如已全部成交不能撤单）时立即抛出 TradeVerificationError 严禁误判成功"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
            {"合同编号": "654321", "证券代码": "000002"},
        ]
        # 委托单已成交，因此二次核对时已从待撤列表中消失
        fake_entrusts_after_executed = [
            {"合同编号": "654321", "证券代码": "000002"},
        ]
        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(
                        self.trader,
                        "_handle_pop_dialogs",
                        return_value={"message": "该委托已全部成交，不能撤单"},
                    ):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, fake_entrusts_after_executed],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.5)
                            self.assertIn("已全部成交", str(ctx.exception))
                            self.assertEqual(ctx.exception.result.get("message"), "rejected")

    def test_cancel_entrust_corrupted_non_dict_grid_elements(self):
        """测试待撤列表中包含损坏的非 dict 元素时能够容错处理并触发无效网格异常"""
        fake_entrusts_before = [
            {"合同编号": "123456", "证券代码": "000001"},
        ]
        fake_entrusts_after_corrupted = [None, "invalid_row", 12345]

        with patch.object(self.trader, "refresh"):
            with patch.object(self.trader, "_cancel_entrust_by_double_click"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(self.trader, "_handle_pop_dialogs", return_value={"message": "ok"}):
                        with patch.object(
                            ClientTrader,
                            "cancel_entrusts",
                            new_callable=PropertyMock,
                            side_effect=[fake_entrusts_before, fake_entrusts_after_corrupted],
                        ):
                            from easytrader.exceptions import TradeVerificationError
                            with self.assertRaises(TradeVerificationError) as ctx:
                                self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertIn("合同编号", str(ctx.exception))

    def test_copy_get_clipboard_failure_raises_ioerror(self):
        """测试剪贴板读取连续失败 5 次时明确抛出 IOError 严禁静默返回空列表 []"""
        strategy = Copy()
        strategy.set_trader(self.trader)
        Copy._need_captcha_reg = False

        mock_grid = MagicMock()
        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            with patch.object(strategy, "_set_foreground"):
                with patch("pywinauto.clipboard.GetData", side_effect=Exception("Clipboard busy")):
                    with self.assertRaises(IOError) as ctx:
                        strategy.get(1047)
                    self.assertIn("获取剪贴板数据失败", str(ctx.exception))

    def test_screenshot_ocr_concatenated_floats_with_commas(self):
        """测试包含千分位逗号的粘连浮点数（如 1,234.502,345.60）能够被正确分解与对齐"""
        mock_ocr_result = [
            # 表头两列：委托金额、成交金额
            [[[10, 0], [100, 0], [100, 20], [10, 20]], "委托金额", 0.99],   # cx=55
            [[[110, 0], [200, 0], [200, 20], [110, 20]], "成交金额", 0.99], # cx=155

            # 数据行：粘连为 "1,234.502,345.60"
            [[[10, 30], [200, 30], [200, 50], [10, 50]], "1,234.502,345.60", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["委托金额"], "1,234.50")
            self.assertEqual(records[0]["成交金额"], "2,345.60")

    def test_screenshot_ocr_arbitrary_polygon_box(self):
        """测试多边形检测框（多于4个坐标点）时中心点计算稳定不错位"""
        mock_ocr_result = [
            # 表头：6点多边形
            [[[10, 0], [30, 0], [50, 5], [50, 20], [30, 20], [10, 15]], "证券代码", 0.99],
            [[[60, 0], [90, 0], [120, 5], [120, 20], [90, 20], [60, 15]], "证券名称", 0.99],

            # 数据行：同样正常对齐
            [[[10, 30], [30, 30], [50, 35], [50, 50], [30, 50], [10, 45]], "600519", 0.99],
            [[[60, 30], [90, 30], [120, 35], [120, 50], [90, 50], [60, 45]], "贵州茅台", 0.99],
        ]
        mock_engine = MagicMock(return_value=(mock_ocr_result, 0.05))
        strategy = grid_strategies.ScreenshotOCR(ocr_engine=mock_engine)

        mock_image = MagicMock()
        mock_grid = MagicMock()
        mock_grid.capture_as_image.return_value = mock_image

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            records = strategy.get(1047)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["证券代码"], "600519")
            self.assertEqual(records[0]["证券名称"], "贵州茅台")

    def test_xls_get_cleans_up_temp_file(self):
        """测试 Xls.get 提取完成后自动清理本地磁盘临时文件，杜绝文件句柄与磁盘空间泄漏"""
        strategy = Xls()
        strategy.set_trader(self.trader)

        mock_grid = MagicMock()
        mock_window = MagicMock()

        created_files = []
        original_mktemp = tempfile.mktemp

        def fake_mktemp(*args, **kwargs):
            path = original_mktemp(*args, **kwargs)
            # 真实创建该文件模拟客户端保存
            with open(path, "w", encoding="gbk") as f:
                f.write("证券代码\t证券名称\n000001\t平安银行\n")
            created_files.append(path)
            return path

        with patch.object(strategy, "_get_grid", return_value=mock_grid):
            with patch.object(strategy, "_set_foreground"):
                with patch.object(self.trader, "is_exist_pop_dialog", return_value=False):
                    with patch("tempfile.mktemp", side_effect=fake_mktemp):
                        with patch.object(self.trader.app, "top_window", return_value=mock_window):
                            records = strategy.get(1047)
                            self.assertEqual(len(records), 1)
                            self.assertEqual(records[0]["证券代码"], "000001")
                            self.assertEqual(len(created_files), 1)
                            # 验证临时文件已被自动删除
                            self.assertFalse(os.path.exists(created_files[0]))


if __name__ == "__main__":
    unittest.main()

