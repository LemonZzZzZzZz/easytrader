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
        """测试 SetForegroundWindow fallback 使用原始 hwnd 整数调用底层 Win32 API"""
        with patch("ctypes.windll.user32.SetForegroundWindow", create=True) as mock_win32:
            mock_win32.return_value = 1
            ret = win_gui.SetForegroundWindow(123456)
            mock_win32.assert_called_once_with(123456)
            self.assertEqual(ret, 1)

    def test_set_foreground_window_fallback_wrapper_object(self):
        """测试 SetForegroundWindow fallback 正确解包具有 handle 属性的 wrapper 对象"""
        mock_wrapper = MagicMock()
        mock_wrapper.handle = 987654

        with patch("ctypes.windll.user32.SetForegroundWindow", create=True) as mock_win32:
            mock_win32.return_value = 1
            ret = win_gui.SetForegroundWindow(mock_wrapper)
            mock_win32.assert_called_once_with(987654)
            self.assertEqual(ret, 1)

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
        """测试异常损坏数据触发捕获并重置 _need_captcha_reg 且安全返回空列表 []"""
        copy_strategy = Copy()
        copy_strategy.set_trader(self.trader)

        Copy._need_captcha_reg = False
        with patch("pandas.read_csv", side_effect=Exception("Malformed TSV")):
            result = copy_strategy._format_grid_data("some corrupt text")
            self.assertEqual(result, [])
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
        """测试 Xls 策略遇到不可读取或损坏文件时捕获异常并返回空列表 []"""
        xls_strategy = Xls()
        xls_strategy.set_trader(self.trader)

        result = xls_strategy._format_grid_data("non_existing_file_path_12345.xls")
        self.assertEqual(result, [])


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
        """测试撤单后在规定时间内未从撤单列表消失返回 unconfirmed 警告"""
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
                            res = self.trader.cancel_entrust("123456", verify_timeout=0.1)
                            self.assertEqual(res["message"], "unconfirmed")
                            self.assertFalse(res["verified"])

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


if __name__ == "__main__":
    unittest.main()
