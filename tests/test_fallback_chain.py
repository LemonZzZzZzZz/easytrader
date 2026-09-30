# -*- coding: utf-8 -*-
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from easytrader import clienttrader, exceptions, grid_strategies
from easytrader.exceptions import CircuitBreakerOpenError, SchemaValidationError
from easytrader.grid_strategies import (
    AdaptiveSchemaValidator,
    BaseStrategy,
    CircuitBreaker,
    Copy,
    FallbackChain,
    IVLMBackend,
    MockVLMBackend,
    OllamaHttpBackend,
    OllamaVLM,
    OpenAICompatibleBackend,
    PipelineContext,
    ScreenshotOCR,
    ValidationResult,
    Xls,
)


class DummyTrader:
    """Mock trader for testing"""
    def __init__(self):
        self.broker_type = "dummy"
        self._main = MagicMock()
        self.app = MagicMock()
        self.config = MagicMock()
        self.config.GRID_DTYPE = None
        self._statics_balance = {}

    @property
    def main(self):
        return self._main

    def _get_balance_from_statics(self):
        return self._statics_balance


class TestPipelineContext(unittest.TestCase):
    """测试 PipelineContext 单帧截图缓存与跨策略调用追踪"""

    def test_single_frame_capture_caching(self):
        context = PipelineContext(control_id=1001)
        mock_grid = MagicMock()
        mock_img = Image.new("RGB", (100, 100), color="white")
        mock_grid.capture_as_image.return_value = mock_img

        # First call: should capture from grid
        img1 = context.get_screenshot(mock_grid)
        self.assertEqual(img1, mock_img)
        self.assertEqual(mock_grid.capture_as_image.call_count, 1)

        # Second call: should return cached image without calling capture_as_image
        img2 = context.get_screenshot(mock_grid)
        self.assertEqual(img2, mock_img)
        self.assertEqual(mock_grid.capture_as_image.call_count, 1)

    def test_explicit_set_screenshot(self):
        context = PipelineContext(control_id=1002)
        mock_img = Image.new("RGB", (50, 50), color="blue")
        context.set_screenshot(mock_img)

        mock_grid = MagicMock()
        res = context.get_screenshot(mock_grid)
        self.assertEqual(res, mock_img)
        mock_grid.capture_as_image.assert_not_called()

    def test_log_trace(self):
        context = PipelineContext(control_id=1003)
        context.log_trace("Copy", status="SUCCESS", result_len=5, duration=0.02)
        context.log_trace("ScreenshotOCR", status="FAILED", error="OCR failed")

        self.assertEqual(len(context.traces), 2)
        self.assertEqual(context.traces[0]["strategy"], "Copy")
        self.assertEqual(context.traces[0]["status"], "SUCCESS")
        self.assertEqual(context.traces[0]["result_len"], 5)
        self.assertEqual(context.traces[1]["strategy"], "ScreenshotOCR")
        self.assertEqual(context.traces[1]["status"], "FAILED")
        self.assertEqual(context.traces[1]["error"], "OCR failed")


class TestAdaptiveSchemaValidator(unittest.TestCase):
    """测试 AdaptiveSchemaValidator 多层自适应网格数据验证"""

    def setUp(self):
        self.validator = AdaptiveSchemaValidator()

    def test_empty_table_without_market_value_is_valid(self):
        # 无持股市值时，空表完全合法（空仓）
        res = self.validator.validate([], market_value=0.0)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.table_type, "empty")

    def test_empty_table_with_market_value_flags_false_empty(self):
        # 股票市值 > 0 但返回空表，判定为假成功漏报 (leak)
        res = self.validator.validate([], market_value=12345.67)
        self.assertFalse(res.is_valid)
        self.assertIn("False-empty", res.error)
        self.assertEqual(res.table_type, "position")

        # 测试 raise_on_error 选项
        with self.assertRaises(SchemaValidationError):
            self.validator.validate([], market_value=12345.67, raise_on_error=True)

    def test_empty_table_with_trader_statics_market_value(self):
        # 通过 trader 的静态资金控件检查持股市值
        trader = DummyTrader()
        trader._statics_balance = {"股票市值": 99999.0}

        res = self.validator.validate([], trader=trader)
        self.assertFalse(res.is_valid)
        self.assertIn("False-empty", res.error)

    def test_empty_table_for_trades_or_entrusts_is_valid_with_market_value(self):
        # 委托或成交表在持股市值 > 0 时为空是正常的
        res = self.validator.validate([], table_type="entrusts", market_value=99999.0)
        self.assertTrue(res.is_valid)

        res2 = self.validator.validate([], table_type="trades", market_value=99999.0)
        self.assertTrue(res2.is_valid)

    def test_schema_matching_position(self):
        records = [
            {"证券代码": "600519", "证券名称": "贵州茅台", "股票余额": 100, "可用余额": 100, "参考市值": 180000.0}
        ]
        res = self.validator.validate(records)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.table_type, "position")

    def test_schema_matching_entrusts(self):
        records = [
            {"合同编号": "HT001", "证券代码": "600519", "操作": "买入", "委托数量": 100, "成交数量": 100}
        ]
        res = self.validator.validate(records)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.table_type, "entrusts")

    def test_schema_matching_trades(self):
        records = [
            {"成交编号": "CJ001", "证券代码": "600519", "成交数量": 100, "成交价格": 1800.0, "成交金额": 180000.0}
        ]
        res = self.validator.validate(records)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.table_type, "trades")

    def test_missing_stock_code_column(self):
        records = [{"股票余额": 100, "可用余额": 100}]
        res = self.validator.validate(records)
        self.assertFalse(res.is_valid)
        self.assertIn("Missing required security code", res.error)

    def test_missing_position_quantity_column(self):
        records = [{"证券代码": "600519", "证券名称": "贵州茅台"}]
        res = self.validator.validate(records, table_type="position")
        self.assertFalse(res.is_valid)
        self.assertIn("missing quantity columns", res.error)

    def test_invalid_stock_code_format(self):
        # 含有非数字代码
        records1 = [{"证券代码": "茅台", "股票余额": 100}]
        res1 = self.validator.validate(records1)
        self.assertFalse(res1.is_valid)
        self.assertIn("invalid stock code", res1.error)

        # 代码长度不对（如3位）
        records2 = [{"证券代码": "123", "股票余额": 100}]
        res2 = self.validator.validate(records2)
        self.assertFalse(res2.is_valid)
        self.assertIn("invalid stock code", res2.error)

        # 港股通5位纯数字合法
        records3 = [{"证券代码": "00700", "股票余额": 100}]
        res3 = self.validator.validate(records3)
        self.assertTrue(res3.is_valid)

        # A股6位纯数字合法
        records4 = [{"证券代码": "000001", "股票余额": 100}]
        res4 = self.validator.validate(records4)
        self.assertTrue(res4.is_valid)

    def test_corrupted_float_format(self):
        records = [{"证券代码": "600519", "股票余额": "invalid_num"}]
        res = self.validator.validate(records)
        self.assertFalse(res.is_valid)
        self.assertIn("non-numeric value", res.error)

    def test_negative_numeric_sanity_check(self):
        # 股票余额为负数不合常理
        records = [{"证券代码": "600519", "股票余额": -100}]
        res = self.validator.validate(records)
        self.assertFalse(res.is_valid)
        self.assertIn("negative value", res.error)

        # 浮动盈亏为负数是合法的
        records_pnl = [{"证券代码": "600519", "股票余额": 100, "浮动盈亏": -123.45}]
        res_pnl = self.validator.validate(records_pnl)
        self.assertTrue(res_pnl.is_valid)

    def test_financial_invariant_available_exceeds_total_balance(self):
        # 可用余额 (200) > 股票余额 (100) 违背金融业务守恒律
        records = [{"证券代码": "600519", "股票余额": 100, "可用余额": 200}]
        res = self.validator.validate(records)
        self.assertFalse(res.is_valid)
        self.assertIn("Financial invariant violated", res.error)
        self.assertIn("可用余额", res.error)

    def test_financial_invariant_deal_exceeds_entrust_quantity(self):
        # 成交数量 (200) > 委托数量 (100) 违背金融业务守恒律
        records = [{"委托编号": "101", "证券代码": "600519", "委托数量": 100, "成交数量": 200}]
        res = self.validator.validate(records)
        self.assertFalse(res.is_valid)
        self.assertIn("Financial invariant violated", res.error)
        self.assertIn("成交数量", res.error)

    def test_financial_invariant_deal_plus_cancel_exceeds_entrust(self):
        # 成交数量 (80) + 撤单数量 (30) > 委托数量 (100) 违背守恒律
        records = [{"委托编号": "101", "证券代码": "600519", "委托数量": 100, "成交数量": 80, "撤单数量": 30}]
        res = self.validator.validate(records)
        self.assertFalse(res.is_valid)
        self.assertIn("Financial invariant violated", res.error)

    def test_empty_table_with_string_market_value(self):
        # 字符串形式的持股市值（如 "12,345.67"）不应崩溃，且能准确拦截假空表
        res = self.validator.validate([], market_value="12,345.67")
        self.assertFalse(res.is_valid)
        self.assertIn("False-empty", res.error)

        # 字符串 "0.00" 判定为正常空仓
        res_zero = self.validator.validate([], market_value="0.00")
        self.assertTrue(res_zero.is_valid)

    def test_numeric_placeholders_not_rejected(self):
        # 真实行情/客户端常见占位符（如成本价为 "--" 或 "-"）不应被视为格式错误
        records = [
            {"证券代码": "600519", "股票余额": 100, "成本价": "--", "市价": "-"}
        ]
        res = self.validator.validate(records)
        self.assertTrue(res.is_valid)

    def test_multi_field_stock_codes(self):
        # 异构表格中有的行使用 "代码" 字段，有的行使用 "证券代码" 字段
        records = [
            {"代码": "600519", "股票余额": 100},
            {"证券代码": "000001", "股票余额": 200},
        ]
        res = self.validator.validate(records)
        self.assertTrue(res.is_valid)


class TestCircuitBreaker(unittest.TestCase):
    """测试 CircuitBreaker 状态机流转 (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)"""

    def test_circuit_breaker_state_flow(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.05, half_open_success_threshold=1)
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertTrue(cb.can_execute())

        # 记录前 2 次失败，不触发熔断
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertTrue(cb.can_execute())

        # 第 3 次失败，达到阈值，切换至 OPEN
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        self.assertFalse(cb.can_execute())

        # 在 recovery_timeout 之前，保持 OPEN
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        self.assertFalse(cb.can_execute())

        # 等待超时后，进入 HALF_OPEN
        time.sleep(0.06)
        self.assertTrue(cb.can_execute())
        self.assertEqual(cb.state, CircuitBreaker.HALF_OPEN)

        # 在 HALF_OPEN 阶段若失败，重新切回 OPEN
        cb.record_failure()
        self.assertEqual(cb.state, CircuitBreaker.OPEN)
        self.assertFalse(cb.can_execute())

        # 再次等待恢复超时，进入 HALF_OPEN
        time.sleep(0.06)
        self.assertTrue(cb.can_execute())
        self.assertEqual(cb.state, CircuitBreaker.HALF_OPEN)

        # 探活成功，状态恢复为 CLOSED
        cb.record_success()
        self.assertEqual(cb.state, CircuitBreaker.CLOSED)
        self.assertTrue(cb.can_execute())


class TestVLMBackends(unittest.TestCase):
    """测试 VLM 后端 (MockVLMBackend, OllamaHttpBackend, OpenAICompatibleBackend)"""

    def test_mock_backend(self):
        backend = MockVLMBackend(response_text='[{"证券代码": "600519", "股票余额": 100}]')
        output = backend.request(b"fake_bytes", "prompt")
        self.assertIn("600519", output)
        self.assertEqual(len(backend.calls), 1)

        # 测试 side_effect 异常
        err_backend = MockVLMBackend(side_effect=IOError("Network timeout"))
        with self.assertRaises(IOError):
            err_backend.request(b"fake_bytes", "prompt")

    @patch("urllib.request.urlopen")
    def test_ollama_http_backend_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": '[{"证券代码": "000001"}]'}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OllamaHttpBackend(model="qwen3.6:35b", host="http://localhost:11434")
        self.assertEqual(backend.timeout, 300.0)
        result = backend.request(b"fake_image", "prompt")
        self.assertEqual(result, '[{"证券代码": "000001"}]')

        # 验证发送的 HTTP 请求 payload 包含 think=False、format=json、options 等预算参数
        req_arg = mock_urlopen.call_args[0][0]
        sent_data = json.loads(req_arg.data.decode("utf-8"))
        self.assertEqual(sent_data["model"], "qwen3.6:35b")
        self.assertEqual(sent_data["format"], "json")
        self.assertFalse(sent_data["think"])
        self.assertEqual(sent_data["options"], {"num_predict": 2048, "temperature": 0})

    @patch("urllib.request.urlopen")
    def test_ollama_http_backend_error(self, mock_urlopen):
        mock_urlopen.side_effect = IOError("Connection refused")
        backend = OllamaHttpBackend(host="http://localhost:11434")
        with self.assertRaises(IOError):
            backend.request(b"fake_image", "prompt")

    @patch("urllib.request.urlopen")
    def test_openai_compatible_backend_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": '[{"证券代码": "688001"}]'}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OpenAICompatibleBackend(base_url="http://localhost:8000/v1")
        result = backend.request(b"fake_image", "prompt")
        self.assertEqual(result, '[{"证券代码": "688001"}]')


    @patch("urllib.request.urlopen")
    def test_ollama_http_backend_error_payload(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"error": "model not found"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OllamaHttpBackend(host="http://localhost:11434")
        with self.assertRaises(IOError) as ctx:
            backend.request(b"fake_image", "prompt")
        self.assertIn("model not found", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_openai_compatible_backend_error_payload(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"error": {"message": "invalid api key"}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OpenAICompatibleBackend(base_url="http://localhost:8000/v1")
        with self.assertRaises(IOError):
            backend.request(b"fake_image", "prompt")

    @patch("urllib.request.urlopen")
    def test_openai_compatible_backend_empty_choices(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"choices": []}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OpenAICompatibleBackend(base_url="http://localhost:8000/v1")
        with self.assertRaises(IOError):
            backend.request(b"fake_image", "prompt")


class TestOllamaVLMStrategy(unittest.TestCase):
    """测试 OllamaVLM 策略及鲁棒 JSON 解析"""

    def test_parse_json_variations(self):
        # 1. 干净的 JSON
        clean_json = '[{"证券代码": "600519", "股票余额": 100}]'
        self.assertEqual(len(OllamaVLM._parse_json_output(clean_json)), 1)

        # 2. Markdown 代码块包裹
        markdown_json = "```json\n" + clean_json + "\n```"
        self.assertEqual(len(OllamaVLM._parse_json_output(markdown_json)), 1)

        # 3. 带前后杂质文本
        surrounded_json = "下面是识别结果：\n" + clean_json + "\n识别完毕，祝交易顺利！"
        self.assertEqual(len(OllamaVLM._parse_json_output(surrounded_json)), 1)

        # 4. 空数组 []
        self.assertEqual(OllamaVLM._parse_json_output("[]"), [])
        self.assertEqual(OllamaVLM._parse_json_output("```json\n[]\n```"), [])

        # 5. 字典封装如 {"data": [...]}
        wrapped_json = '{"data": [{"证券代码": "600519", "股票余额": 100}]}'
        self.assertEqual(len(OllamaVLM._parse_json_output(wrapped_json)), 1)

        # 6. 常见 VLM 缺陷：尾随逗号
        trailing_comma_json = '[{"证券代码": "600519", "股票余额": 100,},]'
        self.assertEqual(len(OllamaVLM._parse_json_output(trailing_comma_json)), 1)

        # 7. 单引号 Python 字典字面量
        single_quote_json = "[{'证券代码': '600519', '股票余额': 100}]"
        self.assertEqual(len(OllamaVLM._parse_json_output(single_quote_json)), 1)

        # 8. 非法输出抛出 ValueError
        with self.assertRaises(ValueError):
            OllamaVLM._parse_json_output("无法识别该图像，请重试")

    def test_ollama_vlm_end_to_end_mocked(self):
        raw_output = '[{"证券代码": "600519", "证券名称": "贵州茅台", "股票余额": 100}, {"证券代码": "汇总", "股票余额": 100}]'
        backend = MockVLMBackend(response_text=raw_output)
        vlm = OllamaVLM(backend=backend)

        mock_trader = DummyTrader()
        vlm.set_trader(mock_trader)

        mock_grid = MagicMock()
        mock_img = Image.new("RGB", (100, 100), color="red")
        mock_grid.capture_as_image.return_value = mock_img
        mock_trader._main.child_window.return_value = mock_grid

        records = vlm.get(1001)
        # 统计汇总行应被 _filter_summary_rows 过滤
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["证券代码"], "600519")


class TestFallbackChain(unittest.TestCase):
    """测试 FallbackChain 责任链、熔断器、自适应验证与制品转储"""

    def setUp(self):
        self.temp_artifact_dir = tempfile.mkdtemp(prefix="test_fallback_artifacts_")

    def tearDown(self):
        if os.path.exists(self.temp_artifact_dir):
            shutil.rmtree(self.temp_artifact_dir, ignore_errors=True)

    def test_primary_success(self):
        strat1 = MagicMock(spec=BaseStrategy)
        strat1.get.return_value = [{"证券代码": "600519", "股票余额": 100}]
        strat2 = MagicMock(spec=BaseStrategy)

        chain = FallbackChain(
            strategies=[strat1, strat2],
            artifact_dir=self.temp_artifact_dir,
        )
        trader = DummyTrader()
        chain.set_trader(trader)

        result = chain.get(1001)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["证券代码"], "600519")
        # strat1 成功，strat2 不会被调用
        strat2.get.assert_not_called()
        # 无降级且成功，不转储制品
        self.assertEqual(len(os.listdir(self.temp_artifact_dir)), 0)

    def test_primary_fail_fallback_success(self):
        # strat1 抛出异常
        strat1 = MagicMock(spec=BaseStrategy)
        strat1.get.side_effect = IOError("Clipboard locked")
        # strat2 成功返回有效数据
        strat2 = MagicMock(spec=BaseStrategy)
        strat2.get.return_value = [{"证券代码": "000001", "股票余额": 200}]

        fallback_events = []
        chain = FallbackChain(
            strategies=[strat1, strat2],
            on_fallback=lambda info: fallback_events.append(info),
            artifact_dir=self.temp_artifact_dir,
        )
        trader = DummyTrader()
        chain.set_trader(trader)

        result = chain.get(1001)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["证券代码"], "000001")

        # 触发了回调通知
        self.assertEqual(len(fallback_events), 1)
        self.assertEqual(fallback_events[0]["tier"], 1)

        # 降级发生，转储了制品文件 (PNG + JSON)
        files = os.listdir(self.temp_artifact_dir)
        self.assertTrue(any(f.endswith(".json") for f in files))

    def test_primary_validation_fail_triggers_fallback(self):
        # strat1 返回违背金融守恒律的数据 (可用余额 > 股票余额)
        strat1 = MagicMock(spec=BaseStrategy)
        strat1.get.return_value = [{"证券代码": "600519", "股票余额": 100, "可用余额": 200}]
        # strat2 返回正确数据
        strat2 = MagicMock(spec=BaseStrategy)
        strat2.get.return_value = [{"证券代码": "600519", "股票余额": 100, "可用余额": 50}]

        chain = FallbackChain(
            strategies=[strat1, strat2],
            artifact_dir=self.temp_artifact_dir,
        )
        chain.set_trader(DummyTrader())

        result = chain.get(1001)
        self.assertEqual(result[0]["可用余额"], 50)

    def test_total_failure_exhausted_dumps_artifact_and_raises(self):
        strat1 = MagicMock(spec=BaseStrategy)
        strat1.get.side_effect = IOError("Copy fail")
        strat2 = MagicMock(spec=BaseStrategy)
        strat2.get.side_effect = IOError("OCR fail")

        chain = FallbackChain(
            strategies=[strat1, strat2],
            artifact_dir=self.temp_artifact_dir,
        )
        chain.set_trader(DummyTrader())

        with self.assertRaises(IOError) as ctx:
            chain.get(1001)
        self.assertIn("FallbackChain exhausted all strategies", str(ctx.exception))

        # 发生全部失败，转储故障现场
        files = os.listdir(self.temp_artifact_dir)
        self.assertTrue(any("total_failure" in f and f.endswith(".json") for f in files))

    def test_circuit_breaker_skips_failed_strategy(self):
        strat1 = MagicMock(spec=BaseStrategy)
        strat1.get.side_effect = IOError("Network dead")
        strat2 = MagicMock(spec=BaseStrategy)
        strat2.get.return_value = [{"证券代码": "600519", "股票余额": 100}]

        chain = FallbackChain(
            strategies=[strat1, strat2],
            failure_threshold=2,
            recovery_timeout=60.0,
            artifact_dir=self.temp_artifact_dir,
        )
        chain.set_trader(DummyTrader())

        # 调用两次使 strat1 连续失败2次，触发熔断
        chain.get(1001)
        chain.get(1001)
        self.assertEqual(strat1.get.call_count, 2)
        cb1 = chain.get_circuit_breaker(strat1)
        self.assertEqual(cb1.state, CircuitBreaker.OPEN)

        # 第3次调用：strat1 熔断中，直接跳过，strat1.get 不会被调用！
        result = chain.get(1001)
        self.assertEqual(result[0]["证券代码"], "600519")
        self.assertEqual(strat1.get.call_count, 2)

    def test_single_frame_caching_across_chain_strategies(self):
        mock_grid = MagicMock()
        mock_img = Image.new("RGB", (64, 64), color="green")
        mock_grid.capture_as_image.return_value = mock_img

        class VisionStratA(BaseStrategy):
            def get(self, control_id, context=None):
                img = context.get_screenshot(mock_grid)
                # 模拟处理并失败
                raise RuntimeError("Vision A failed")

        class VisionStratB(BaseStrategy):
            def get(self, control_id, context=None):
                img = context.get_screenshot(mock_grid)
                return [{"证券代码": "600519", "股票余额": 100}]

        chain = FallbackChain(
            strategies=[VisionStratA(), VisionStratB()],
            artifact_dir=self.temp_artifact_dir,
        )
        chain.set_trader(DummyTrader())

        res = chain.get(1001)
        self.assertEqual(len(res), 1)
        # 即使 VisionStratA 和 VisionStratB 两个视觉策略级联执行，截图只截取了一次！
        self.assertEqual(mock_grid.capture_as_image.call_count, 1)


    def test_empty_strategies_raises_value_error(self):
        with self.assertRaises(ValueError):
            FallbackChain(strategies=[])

    def test_all_circuit_breakers_open_raises_circuit_breaker_open_error(self):
        strat1 = MagicMock(spec=BaseStrategy)
        strat1.get.side_effect = IOError("Fail 1")
        strat2 = MagicMock(spec=BaseStrategy)
        strat2.get.side_effect = IOError("Fail 2")

        chain = FallbackChain(
            strategies=[strat1, strat2],
            failure_threshold=1,
            recovery_timeout=60.0,
            artifact_dir=self.temp_artifact_dir,
        )
        chain.set_trader(DummyTrader())

        # 第一次执行：两个策略各失败一次，均进入 OPEN
        with self.assertRaises(IOError):
            chain.get(1001)

        # 第二次执行：全部熔断，应明确抛出 CircuitBreakerOpenError
        with self.assertRaises(CircuitBreakerOpenError):
            chain.get(1001)

    def test_artifact_cleanup_rotation(self):
        strat = MagicMock(spec=BaseStrategy)
        strat.get.side_effect = IOError("Always fail")

        chain = FallbackChain(
            strategies=[strat],
            circuit_breaker=False,
            artifact_dir=self.temp_artifact_dir,
            max_artifacts=5,
        )
        chain.set_trader(DummyTrader())

        for _ in range(8):
            try:
                chain.get(1001)
            except IOError:
                pass

        files = os.listdir(self.temp_artifact_dir)
        # max_artifacts=5，保留的文件数量应受控
        self.assertLessEqual(len(files), 10)


class TestClientTraderFallbackIntegration(unittest.TestCase):
    """测试 ClientTrader 对 grid_strategy property/setter 缓存重置与 enable_vlm_fallback 的集成"""

    def test_grid_strategy_setter_invalidates_cache(self):
        trader = clienttrader.ClientTrader()
        # 默认网格策略为 Copy
        self.assertEqual(trader.grid_strategy, Copy)
        inst1 = trader.grid_strategy_instance
        self.assertIsInstance(inst1, Copy)

        # 重新赋值为 Xls 类型
        trader.grid_strategy = Xls
        self.assertEqual(trader.grid_strategy, Xls)
        inst2 = trader.grid_strategy_instance
        self.assertIsInstance(inst2, Xls)
        self.assertIsNot(inst1, inst2)

        # 赋值为已有实例
        custom_strat = Copy()
        trader.grid_strategy = custom_strat
        self.assertIs(trader.grid_strategy_instance, custom_strat)
        self.assertIs(custom_strat._trader, trader)

    def test_enable_vlm_fallback(self):
        trader = clienttrader.ClientTrader()
        chain = trader.enable_vlm_fallback(
            model="qwen2.5-vl:7b",
            host="http://localhost:11434",
            circuit_breaker=True,
        )

        self.assertIsInstance(chain, FallbackChain)
        self.assertIs(trader.grid_strategy_instance, chain)
        self.assertIs(chain._trader, trader)

        # 检查策略层级包含 [Copy, ScreenshotOCR, OllamaVLM]
        strat_types = [type(s) for s in chain.strategies]
        self.assertIn(Copy, strat_types)
        self.assertIn(ScreenshotOCR, strat_types)
        self.assertIn(OllamaVLM, strat_types)

    def test_enable_vlm_fallback_repeated_calls_do_not_nest(self):
        trader = clienttrader.ClientTrader()
        chain1 = trader.enable_vlm_fallback(model="qwen2.5-vl:7b")
        chain2 = trader.enable_vlm_fallback(model="qwen-vl-plus", failure_threshold=5)

        self.assertIsInstance(chain2, FallbackChain)
        self.assertIsInstance(chain2.strategies[0], Copy)
        self.assertNotIsInstance(chain2.strategies[0], FallbackChain)
        self.assertEqual(chain2.failure_threshold, 5)

    def test_subclass_grid_strategy_class_attribute_inheritance(self):
        class MockSubTrader(clienttrader.ClientTrader):
            grid_strategy = Xls

        # 类级别访问
        self.assertEqual(MockSubTrader.grid_strategy, Xls)

        # 实例访问
        sub_trader = MockSubTrader()
        self.assertEqual(sub_trader.grid_strategy, Xls)
        inst = sub_trader.grid_strategy_instance
        self.assertIsInstance(inst, Xls)

        # 切换实例策略
        sub_trader.grid_strategy = Copy
        self.assertIsInstance(sub_trader.grid_strategy_instance, Copy)


if __name__ == "__main__":
    unittest.main()
