# -*- coding: utf-8 -*-
import io
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
from PIL import Image, ImageDraw

from easytrader import clienttrader, exceptions
from easytrader.clienttrader import ClientTrader
from easytrader.exceptions import (
    HumanInterventionRequiredError,
    TradeError,
    TradeVerificationError,
    VisualArbitrationError,
)
from easytrader.vlm_visual_oracle import (
    ArbitrationDecision,
    ClientVisualLivenessWatchdog,
    ControlGroundingResult,
    DialogDecision,
    DualFrameReceiptResult,
    IVLMBackend,
    LivenessReport,
    MockVLMBackend,
    ModalDialogVisualArbitrator,
    OllamaHttpBackend,
    OpenAICompatibleBackend,
    RowAlignmentResult,
    StatusBarToastResult,
    StatusBarToastVerifier,
    TradeReceiptDiffArbitrator,
    VisualGroundingEngine,
    VLMVisualOracle,
    extract_json_from_response,
    image_to_bytes,
    image_to_pil,
    normalize_bbox_and_center,
)


class TestVLMVisualOracleHelpers(unittest.TestCase):
    """测试辅助函数：图像格式转换与 JSON 容错提取"""

    def test_image_format_conversions(self):
        # 1. PIL Image
        pil_img = Image.new("RGB", (60, 40), color="blue")
        bytes_data = image_to_bytes(pil_img)
        self.assertIsInstance(bytes_data, bytes)
        self.assertTrue(bytes_data.startswith(b"\x89PNG"))

        # 2. Numpy ndarray
        np_arr = np.zeros((40, 60, 3), dtype=np.uint8)
        bytes_from_np = image_to_bytes(np_arr)
        self.assertIsInstance(bytes_from_np, bytes)
        pil_from_np = image_to_pil(np_arr)
        self.assertEqual(pil_from_np.size, (60, 40))

        # 3. File path
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            pil_img.save(temp_path, format="PNG")
            bytes_from_file = image_to_bytes(temp_path)
            self.assertEqual(bytes_from_file[:4], b"\x89PNG")
            pil_from_file = image_to_pil(temp_path)
            self.assertEqual(pil_from_file.size, (60, 40))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # 4. Invalid types
        with self.assertRaises(TypeError):
            image_to_bytes(12345)
        with self.assertRaises(FileNotFoundError):
            image_to_bytes("non_existent_file_path.png")

    def test_extract_json_robustness(self):
        # 纯 JSON
        d1 = extract_json_from_response('{"status": "ok", "code": 0}')
        self.assertEqual(d1.get("status"), "ok")

        # Markdown 代码块包裹
        d2 = extract_json_from_response(
            "```json\n{\"dialog_type\": \"CAPTCHA\", \"countdown_seconds\": 5}\n```"
        )
        self.assertEqual(d2.get("dialog_type"), "CAPTCHA")
        self.assertEqual(d2.get("countdown_seconds"), 5)

        # 包含模型思索/评论前后缀
        d3 = extract_json_from_response(
            "经过视觉分析，输出如下：\n{\"decision\": \"SUBMIT_CONFIRMED\"}\n请注意核对。"
        )
        self.assertEqual(d3.get("decision"), "SUBMIT_CONFIRMED")

        # 损坏的 JSON 返回空 dict 且不报错
        d4 = extract_json_from_response("Malformed invalid json text")
        self.assertEqual(d4, {})


class TestStatusBarToastVerifier(unittest.TestCase):
    """
    R1: 自绘状态栏与浮动 Toast 拒单捕获
    Acceptance Criteria:
    - 提取到废单/拒绝文本时抛出 TradeError，严禁返回假成功
    - 提取到已申报与合同编号时正确解析回填至返回字典
    """

    def setUp(self):
        # 生成合成状态栏测试图
        self.img = Image.new("RGB", (600, 100), color="white")

    def test_rejection_keywords_raises_trade_error(self):
        """测试提取到各类废单/拒绝文本时严格抛出 TradeError"""
        rejection_cases = [
            '{"rejected": true, "reject_reason": "资金不足", "message": "废单：可用资金不足以买入"}',
            '{"rejected": true, "reject_reason": "超出涨跌停限制", "message": "废单：委托价格超出涨跌停"}',
            '{"rejected": false, "message": "委托失败: 无效委托，买入金额超限"}',
            '{"rejected": false, "message": "非交易时间，禁止交易"}',
        ]

        for resp in rejection_cases:
            backend = MockVLMBackend(response_text=resp)
            verifier = StatusBarToastVerifier(backend=backend)

            with self.assertRaises(TradeError) as ctx:
                verifier.verify(self.img, raise_on_reject=True)
            self.assertTrue(
                any(
                    kw in str(ctx.exception)
                    for kw in ["资金不足", "超出涨跌停", "无效委托", "禁止交易"]
                )
            )

    def test_rejection_without_raising_returns_structured_rejected_status(self):
        """测试 raise_on_reject=False 时返回完整的结构化 REJECTED 结果"""
        resp = '{"rejected": true, "reject_reason": "超出涨停限制", "message": "价格超出涨停限制"}'
        backend = MockVLMBackend(response_text=resp)
        verifier = StatusBarToastVerifier(backend=backend)

        res = verifier.verify(self.img, raise_on_reject=False)
        self.assertTrue(res.is_rejected)
        self.assertEqual(res.status, "REJECTED")
        self.assertIn("超出涨停", res.reject_reason)

    def test_success_toast_extracts_entrust_no(self):
        """测试提取到已申报与合同编号时正确解析回填"""
        resp = '{"rejected": false, "entrust_no": "88776655", "message": "委托已申报，合同编号: 88776655"}'
        backend = MockVLMBackend(response_text=resp)
        verifier = StatusBarToastVerifier(backend=backend)

        res = verifier.verify(self.img)
        self.assertFalse(res.is_rejected)
        self.assertEqual(res.status, "CONFIRMED")
        self.assertEqual(res.entrust_no, "88776655")
        self.assertIn("委托已申报", res.message)

    def test_regex_fallback_extracts_entrust_no_from_unstructured_text(self):
        """测试模型未提取字段时，通过正则兜底提取合同编号"""
        resp = '{"rejected": false, "message": "系统提示：买入委托已申报成功，申报号 654321 已提交柜台"}'
        backend = MockVLMBackend(response_text=resp)
        verifier = StatusBarToastVerifier(backend=backend)

        res = verifier.verify(self.img)
        self.assertFalse(res.is_rejected)
        self.assertEqual(res.status, "CONFIRMED")
        self.assertEqual(res.entrust_no, "654321")


class TestModalDialogVisualArbitrator(unittest.TestCase):
    """
    R2: 未知与多态阻断弹窗视觉智能仲裁
    Acceptance Criteria:
    - 找不到控件 ID 1365 时绝不盲目返回 success
    - 准确识别倒计时剩余时间、复选框位置与确认/跳过按钮安全决策
    - 识别到图形验证码时抛出 HumanInterventionRequiredError 熔断
    """

    def setUp(self):
        self.img = Image.new("RGB", (400, 300), color="gray")

    def test_captcha_triggers_circuit_breaker_error(self):
        """识别到图形验证码 (CAPTCHA) 时立即抛出 HumanInterventionRequiredError 熔断"""
        captcha_cases = [
            '{"dialog_type": "CAPTCHA", "has_captcha": true, "message": "请输入图片中的验证码"}',
            '{"dialog_type": "UNKNOWN", "message": "检测到人机校验，请滑动滑块完成拼图"}',
            '{"dialog_type": "CAPTCHA", "action": "CIRCUIT_BREAK", "message": "点选汉字验证码"}',
        ]

        for resp in captcha_cases:
            backend = MockVLMBackend(response_text=resp)
            arbitrator = ModalDialogVisualArbitrator(backend=backend)

            with self.assertRaises(HumanInterventionRequiredError) as ctx:
                arbitrator.arbitrate(self.img)
            self.assertTrue(
                any(kw in str(ctx.exception) for kw in ["验证码", "人机", "CAPTCHA"])
            )

    def test_risk_disclosure_countdown_and_checkbox_decision(self):
        """准确识别倒计时剩余时间、复选框位置与先勾选再等倒计时确认安全决策"""
        resp = json.dumps(
            {
                "dialog_type": "RISK_DISCLOSURE",
                "countdown_seconds": 5.0,
                "has_checkbox": True,
                "checkbox_bbox": [0.70, 0.15, 0.75, 0.20],
                "action": "CHECK_AND_WAIT_CONFIRM",
                "action_button_bbox": [0.82, 0.40, 0.90, 0.60],
                "action_button_text": "确定",
                "message": "风险揭示：请仔细阅读免责声明(5秒)",
            }
        )
        backend = MockVLMBackend(response_text=resp)
        arbitrator = ModalDialogVisualArbitrator(backend=backend)

        decision = arbitrator.arbitrate(self.img)
        self.assertEqual(decision.dialog_type, "RISK_DISCLOSURE")
        self.assertEqual(decision.action, "CHECK_AND_WAIT_CONFIRM")
        self.assertEqual(decision.countdown_seconds, 5.0)
        self.assertTrue(decision.has_checkbox)
        self.assertIsNotNone(decision.checkbox_coord)
        self.assertIsNotNone(decision.button_coord)
        self.assertFalse(decision.requires_human)

    def test_password_expiry_skip_decision(self):
        """准确识别密码过期提示并决策为跳过/稍后提醒"""
        resp = json.dumps(
            {
                "dialog_type": "PASSWORD_EXPIRY",
                "action": "SKIP",
                "action_button_bbox": [0.80, 0.60, 0.88, 0.80],
                "action_button_text": "稍后提醒",
                "message": "交易密码已使用超过90天，请及时修改",
            }
        )
        backend = MockVLMBackend(response_text=resp)
        arbitrator = ModalDialogVisualArbitrator(backend=backend)

        decision = arbitrator.arbitrate(self.img)
        self.assertEqual(decision.dialog_type, "PASSWORD_EXPIRY")
        self.assertEqual(decision.action, "SKIP")
        self.assertIsNotNone(decision.button_coord)
        self.assertEqual(decision.button_text, "稍后提醒")

    def test_unknown_blocking_dialog_strict_mode_raises_error(self):
        """严格模式下，未识别弹窗绝不盲目返回 success，必须抛出 TradeVerificationError"""
        resp = '{"dialog_type": "UNKNOWN", "message": "无法识别的非标弹窗"}'
        backend = MockVLMBackend(response_text=resp)
        arbitrator = ModalDialogVisualArbitrator(backend=backend)

        with self.assertRaises(TradeVerificationError) as ctx:
            arbitrator.arbitrate(self.img, strict=True)
        self.assertIn("无法视觉仲裁闭环", str(ctx.exception))


class TestTradeReceiptDiffArbitrator(unittest.TestCase):
    """
    R3: 委托终态双帧差分仲裁
    Acceptance Criteria:
    - 准确识别表单复位与资金扣减，给出 SUBMIT_CONFIRMED / SUBMIT_FAILED / AMBIGUOUS 决策
    - 置信度不足时引导降级查询
    """

    def setUp(self):
        # 模拟操作区与右侧五档跳动行情
        self.frame_before = Image.new("RGB", (600, 400), color="white")
        draw_b = ImageDraw.Draw(self.frame_before)
        # 左侧操作区：填写了股票代码与价格
        draw_b.text((50, 50), "600000", fill="black")
        draw_b.text((50, 80), "1000", fill="black")
        draw_b.text((50, 110), "FUNDS: 50000.00", fill="black")
        # 右侧五档：买一卖一
        draw_b.text((400, 50), "BID1: 10.00 500", fill="red")

        self.frame_after = Image.new("RGB", (600, 400), color="white")
        draw_a = ImageDraw.Draw(self.frame_after)
        # 左侧操作区：已清空复位，资金变动
        draw_a.text((50, 110), "FUNDS: 40000.00", fill="black")
        draw_a.text((50, 200), "FLOW: BUY 600000 1000", fill="blue")
        # 右侧五档：行情自然跳动变化
        draw_a.text((400, 50), "BID1: 10.01 600", fill="red")

    def test_dual_frame_submit_confirmed(self):
        """识别表单复位、资金扣减与流水新增，置信度高时裁决为 SUBMIT_CONFIRMED"""
        resp = json.dumps(
            {
                "decision": "SUBMIT_CONFIRMED",
                "confidence": 0.95,
                "form_cleared": True,
                "funds_frozen": True,
                "mini_flow_added": True,
                "reasons": ["表单清空", "资金减少10000", "流水新增买入记录"],
                "suggested_action": "PROCEED",
            }
        )
        backend = MockVLMBackend(response_text=resp)
        arbitrator = TradeReceiptDiffArbitrator(backend=backend)

        res = arbitrator.arbitrate(self.frame_before, self.frame_after)
        self.assertEqual(res.decision, ArbitrationDecision.SUBMIT_CONFIRMED)
        self.assertGreaterEqual(res.confidence, 0.85)
        self.assertTrue(res.form_cleared)
        self.assertTrue(res.funds_frozen)
        self.assertEqual(res.suggested_action, "PROCEED")

    def test_dual_frame_submit_failed(self):
        """识别到显式错误提示且表单未提交，裁决为 SUBMIT_FAILED"""
        resp = json.dumps(
            {
                "decision": "SUBMIT_FAILED",
                "confidence": 0.92,
                "form_cleared": False,
                "funds_frozen": False,
                "error_detected": True,
                "reasons": ["表单标红提示超出涨跌停", "资金无变化"],
                "suggested_action": "RETRY",
            }
        )
        backend = MockVLMBackend(response_text=resp)
        arbitrator = TradeReceiptDiffArbitrator(backend=backend)

        res = arbitrator.arbitrate(self.frame_before, self.frame_after)
        self.assertEqual(res.decision, ArbitrationDecision.SUBMIT_FAILED)
        self.assertEqual(res.suggested_action, "RETRY")

    def test_dual_frame_low_confidence_forces_ambiguous_and_guides_query(self):
        """置信度低于安全阈值时，强制裁决为 AMBIGUOUS 并引导降级查询 (QUERY_TODAY_ENTRUSTS)"""
        # 模型自称 CONFIRMED，但 confidence 仅为 0.70 (低于默认阈值 0.85)
        resp = json.dumps(
            {
                "decision": "SUBMIT_CONFIRMED",
                "confidence": 0.70,
                "form_cleared": True,
                "funds_frozen": False,
                "reasons": ["仅看到表单清空，资金未变化"],
                "suggested_action": "PROCEED",
            }
        )
        backend = MockVLMBackend(response_text=resp)
        arbitrator = TradeReceiptDiffArbitrator(backend=backend, confidence_threshold=0.85)

        res = arbitrator.arbitrate(self.frame_before, self.frame_after)
        self.assertEqual(res.decision, ArbitrationDecision.AMBIGUOUS)
        self.assertEqual(res.suggested_action, "QUERY_TODAY_ENTRUSTS")
        self.assertTrue(any("低于安全阈值" in r for r in res.reasons))


class TestVisualGroundingEngine(unittest.TestCase):
    """
    R4: 无句柄控件视觉 Grounding 与自适应点击
    Acceptance Criteria:
    - 支持根据语义描述（如“全撤”、“当日委托”）定位无句柄控件
    - 网格行定位具备局部几何吸附，严禁双击至错误相邻行
    """

    def setUp(self):
        self.img = Image.new("RGB", (500, 300), color="white")

    def test_semantic_grounding_cancel_all(self):
        """语义描述定位无句柄控件（如“全撤”）并输出准确归一化 BBox 与点击中心点"""
        resp = json.dumps(
            {
                "found": True,
                "description": "全撤",
                "bbox": [0.10, 0.70, 0.18, 0.85],
                "confidence": 0.98,
            }
        )
        backend = MockVLMBackend(response_text=resp)
        engine = VisualGroundingEngine(backend=backend)

        res = engine.ground_control(self.img, "全撤")
        self.assertTrue(res.found)
        self.assertEqual(res.bbox, (0.10, 0.70, 0.18, 0.85))
        # 500x300 图像下：
        # cx = ((0.70 + 0.85)/2) * 500 = 387
        # cy = ((0.10 + 0.18)/2) * 300 = 42
        self.assertEqual(res.center, (387, 42))

    def test_grid_row_geometric_snapping_prevents_border_or_adjacent_click(self):
        """测试网格行定位具备局部几何吸附，严禁双击至分割线或相邻行"""
        engine = VisualGroundingEngine()

        # 构造带有显著水平网格分割线的图像
        # 表头高 30，每行 20 像素，分割线位于 y=30, 50, 70, 90, 110
        grid_img = Image.new("L", (200, 150), color=255)
        draw = ImageDraw.Draw(grid_img)
        for y_line in [30, 50, 70, 90, 110]:
            draw.line([(0, y_line), (200, y_line)], fill=0, width=1)

        # 场景 1: nominal_y 恰好落在分割线边沿 (y=49，距分割线 50 仅 1 像素，若不吸附必点错)
        # target_row = 0 (行范围 30~50, 真实中心 40)
        res1 = engine.calibrate_grid_row_click(
            grid_image=grid_img,
            target_row=0,
            nominal_x=50,
            nominal_y=49,
            first_row_height=30,
            row_height=20,
            safety_margin=3,
        )
        self.assertTrue(res1.snapped)
        self.assertEqual(res1.calibrated_y, 40)
        self.assertEqual(res1.row_top, 30)
        self.assertEqual(res1.row_bottom, 50)

        # 场景 2: target_row = 1 (行范围 50~70, 真实中心 60), nominal_y 飘移到了 69 (临近分割线 70)
        res2 = engine.calibrate_grid_row_click(
            grid_image=grid_img,
            target_row=1,
            nominal_x=50,
            nominal_y=69,
            first_row_height=30,
            row_height=20,
            safety_margin=3,
        )
        self.assertTrue(res2.snapped)
        self.assertEqual(res2.calibrated_y, 60)

        # 场景 3: nominal_y 已经处于安全中心内部 (y=61)
        res3 = engine.calibrate_grid_row_click(
            grid_image=grid_img,
            target_row=1,
            nominal_x=50,
            nominal_y=61,
            first_row_height=30,
            row_height=20,
            safety_margin=3,
        )
        self.assertFalse(res3.snapped)
        self.assertEqual(res3.calibrated_y, 61)


class TestClientVisualLivenessWatchdog(unittest.TestCase):
    """
    R5: 客户端存活与状态视觉看门狗
    Acceptance Criteria:
    - 准确检测黑屏、通讯脱机红灯与界面阻塞覆盖层
    """

    def setUp(self):
        self.watchdog = ClientVisualLivenessWatchdog()

    def test_black_screen_detection(self):
        """准确检测 DWM 锁屏/远程断开导致的黑屏 (均值与方差近 0)"""
        black_img = Image.new("RGB", (400, 300), color=(0, 0, 0))
        report = self.watchdog.inspect(black_img)
        self.assertFalse(report.is_alive)
        self.assertEqual(report.state, "BLACK_SCREEN")
        self.assertEqual(report.recovery_action, "RECONNECT")
        self.assertTrue(any("黑屏" in issue for issue in report.issues))

    def test_offline_red_indicator_detection(self):
        """准确检测状态栏通讯指示灯红灯脱机"""
        img = Image.new("RGB", (400, 300), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        # 在底部状态栏 (y >= 255) 绘制一个纯红色的通信指示灯块 (20x20)
        draw.rectangle([(20, 265), (45, 285)], fill=(255, 0, 0))

        report = self.watchdog.inspect(img)
        self.assertFalse(report.is_alive)
        self.assertEqual(report.state, "OFFLINE")
        self.assertEqual(report.recovery_action, "RECONNECT")
        self.assertTrue(any("红灯脱机" in issue for issue in report.issues))

    def test_mask_locked_detection(self):
        """准确检测全屏半透明 DirectUI 遮罩卡死"""
        mask_img = Image.new("RGB", (400, 300), color=(30, 30, 30))
        report = self.watchdog.inspect(mask_img)
        self.assertFalse(report.is_alive)
        self.assertEqual(report.state, "MASK_LOCKED")
        self.assertEqual(report.recovery_action, "DISMISS_MASK")
        self.assertTrue(any("遮罩锁死" in issue for issue in report.issues))

    def test_win32_hung_app_window_detection(self):
        """准确检测 Win32 消息泵死锁挂起 (IsHungAppWindow)"""
        normal_img = Image.new("RGB", (400, 300), color=(200, 200, 200))
        with patch("ctypes.windll.user32.IsHungAppWindow", return_value=1):
            report = self.watchdog.inspect(normal_img, hwnd=99999)
            self.assertFalse(report.is_alive)
            self.assertEqual(report.state, "MESSAGE_PUMP_HUNG")
            self.assertEqual(report.recovery_action, "RESTART_PROCESS")

    def test_healthy_client_state(self):
        """正常交易界面检测为 HEALTHY 状态"""
        healthy_img = Image.new("RGB", (400, 300), color=(220, 220, 220))
        draw = ImageDraw.Draw(healthy_img)
        draw.text((20, 20), "TRADING SYSTEM 5.0", fill=(0, 0, 0))
        # 绿色通讯指示灯
        draw.rectangle([(20, 270), (35, 285)], fill=(0, 200, 0))

        with patch("ctypes.windll.user32.IsHungAppWindow", return_value=0):
            report = self.watchdog.inspect(healthy_img, hwnd=12345)
            self.assertTrue(report.is_alive)
            self.assertEqual(report.state, "HEALTHY")
            self.assertEqual(report.recovery_action, "NONE")
            self.assertEqual(len(report.issues), 0)

    def test_trigger_recovery(self):
        """测试看门狗自愈触发逻辑"""
        mock_trader = MagicMock()
        report_mask = LivenessReport(
            is_alive=False,
            state="MASK_LOCKED",
            issues=["遮罩锁死"],
            recovery_action="DISMISS_MASK",
        )
        self.watchdog.trigger_recovery(report_mask, trader=mock_trader)
        mock_trader.close_pop_dialog.assert_called_once()

        mock_cb = MagicMock()
        self.watchdog.trigger_recovery(report_mask, callback=mock_cb)
        mock_cb.assert_called_once_with(report_mask)


class TestClientTraderVisualOracleIntegration(unittest.TestCase):
    """
    ClientTrader 与 VLMVisualOracle 端到端集成测试
    验证交易链路中的状态栏校验、未知弹窗防静默成功与网格吸附
    """

    def setUp(self):
        self.trader = ClientTrader()
        self.trader._app = MagicMock()
        self.trader._main = MagicMock()
        self.trader._config = MagicMock()
        self.trader._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN = 50
        self.trader._config.CANCEL_ENTRUST_GRID_FIRST_ROW_HEIGHT = 30
        self.trader._config.CANCEL_ENTRUST_GRID_ROW_HEIGHT = 16
        self.trader._config.COMMON_GRID_CONTROL_ID = 1047
        self.trader._config.POP_DIALOD_TITLE_CONTROL_ID = 1365

    def test_handle_pop_dialogs_no_1365_fails_closed_without_oracle(self):
        """当找不到 1365 且无视觉仲裁器时，严禁静默返回 success，必须抛出 TradeVerificationError"""
        from pywinauto.findwindows import ElementNotFoundError

        with patch.object(self.trader, "is_exist_pop_dialog", side_effect=[True, False]):
            with patch.object(
                self.trader,
                "_get_pop_dialog_title",
                side_effect=ElementNotFoundError("No 1365"),
            ):
                with self.assertRaises(TradeVerificationError) as ctx:
                    self.trader._handle_pop_dialogs()
                self.assertIn("严禁返回假成功", str(ctx.exception))

    def test_handle_pop_dialogs_no_1365_arbitrates_with_oracle(self):
        """当找不到 1365 但配置了视觉仲裁器时，调用视觉仲裁处理密码过期/免责声明"""
        from pywinauto.findwindows import ElementNotFoundError

        mock_oracle = MagicMock()
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="PASSWORD_EXPIRY",
            action="SKIP",
            button_coord=(250, 180),
            button_text="稍后提醒",
        )
        self.trader.visual_oracle = mock_oracle

        with patch.object(self.trader, "is_exist_pop_dialog", side_effect=[True, False]):
            with patch.object(
                self.trader,
                "_get_pop_dialog_title",
                side_effect=ElementNotFoundError("No 1365"),
            ):
                with patch.object(self.trader, "_click_coords") as mock_click:
                    res = self.trader._handle_pop_dialogs()
                    mock_oracle.arbitrate_modal_dialog.assert_called_once()
                    mock_click.assert_called_once_with((250, 180))
                    self.assertEqual(res, {"message": "success"})

    def test_handle_pop_dialogs_no_1365_captcha_triggers_circuit_breaker(self):
        """当找不到 1365 且视觉仲裁发现验证码时，抛出 HumanInterventionRequiredError 熔断"""
        from pywinauto.findwindows import ElementNotFoundError

        mock_oracle = MagicMock()
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="CAPTCHA",
            action="CIRCUIT_BREAK",
            requires_human=True,
            message="验证码需要人工输入",
        )
        self.trader.visual_oracle = mock_oracle

        with patch.object(self.trader, "is_exist_pop_dialog", return_value=True):
            with patch.object(
                self.trader,
                "_get_pop_dialog_title",
                side_effect=ElementNotFoundError("No 1365"),
            ):
                with self.assertRaises(HumanInterventionRequiredError) as ctx:
                    self.trader._handle_pop_dialogs()
                self.assertIn("图形验证码", str(ctx.exception))

    def test_trade_method_toast_reject_raises_trade_error(self):
        """trade() 提交后，若 Toast/状态栏被 VLM 判定为废单拒绝，抛出 TradeError"""
        mock_oracle = MagicMock()
        mock_oracle.verify_status_bar_and_toast.side_effect = TradeError("交易废单拒绝: 资金不足")
        self.trader.visual_oracle = mock_oracle

        with patch.object(self.trader, "_set_trade_params"):
            with patch.object(self.trader, "_submit_trade"):
                with patch.object(
                    self.trader, "_handle_pop_dialogs", return_value={"message": "success"}
                ):
                    with self.assertRaises(TradeError) as ctx:
                        self.trader.trade("600000", 10.0, 100)
                    self.assertIn("资金不足", str(ctx.exception))

    def test_trade_method_toast_entrust_no_backfilled(self):
        """trade() 提交后，若状态栏/Toast 识别到合同编号，自动回填至结果字典"""
        mock_oracle = MagicMock()
        mock_oracle.verify_status_bar_and_toast.return_value = StatusBarToastResult(
            is_rejected=False,
            entrust_no="778899",
            message="委托已申报，合同编号: 778899",
            status="CONFIRMED",
        )
        self.trader.visual_oracle = mock_oracle

        with patch.object(self.trader, "_set_trade_params"):
            with patch.object(self.trader, "_submit_trade"):
                with patch.object(
                    self.trader, "_handle_pop_dialogs", return_value={"message": "success"}
                ):
                    res = self.trader.trade("600000", 10.0, 100)
                    self.assertEqual(res.get("entrust_no"), "778899")
                    self.assertIn("778899", res.get("message", ""))


class TestVLMVisualOracleUnifiedFacade(unittest.TestCase):
    """测试统一门面 VLMVisualOracle 所有对外封装方法"""

    def test_facade_methods(self):
        oracle = VLMVisualOracle()
        test_img = Image.new("RGB", (200, 100), color="white")

        # 1. 状态栏提取
        with patch.object(
            oracle.status_verifier,
            "verify",
            return_value=StatusBarToastResult(is_rejected=False, entrust_no="123"),
        ) as m_verify:
            res = oracle.verify_status_bar_and_toast(test_img)
            self.assertEqual(res.entrust_no, "123")

        # 2. 弹窗仲裁
        with patch.object(
            oracle.dialog_arbitrator,
            "arbitrate",
            return_value=DialogDecision(dialog_type="CONFIRMATION", action="CONFIRM"),
        ) as m_dialog:
            dec = oracle.arbitrate_modal_dialog(test_img)
            self.assertEqual(dec.action, "CONFIRM")

        # 3. 双帧差分
        with patch.object(
            oracle.receipt_arbitrator,
            "arbitrate",
            return_value=DualFrameReceiptResult(
                decision="SUBMIT_CONFIRMED",
                confidence=0.99,
                form_cleared=True,
                funds_frozen=True,
                mini_flow_added=True,
            ),
        ) as m_diff:
            diff_res = oracle.arbitrate_trade_receipt(test_img, test_img)
            self.assertEqual(diff_res.decision, "SUBMIT_CONFIRMED")

        # 4. Grounding
        with patch.object(
            oracle.grounding_engine,
            "ground_control",
            return_value=ControlGroundingResult(description="全撤", found=True, center=(50, 50)),
        ) as m_ground:
            g_res = oracle.ground_control(test_img, "全撤")
            self.assertEqual(g_res.center, (50, 50))

        # 5. 行高吸附校准
        with patch.object(
            oracle.grounding_engine,
            "calibrate_grid_row_click",
            return_value=RowAlignmentResult(
                nominal_y=49,
                calibrated_y=40,
                row_top=30,
                row_bottom=50,
                snapped=True,
                target_row=0,
                safety_margin=3,
            ),
        ) as m_calib:
            row_res = oracle.calibrate_grid_row(test_img, 0, 50, 49)
            self.assertTrue(row_res.snapped)
            self.assertEqual(row_res.calibrated_y, 40)

        # 6. 看门狗巡检
        with patch.object(
            oracle.liveness_watchdog,
            "inspect",
            return_value=LivenessReport(is_alive=True, state="HEALTHY"),
        ) as m_inspect:
            live_res = oracle.inspect_liveness(test_img)
            self.assertTrue(live_res.is_alive)


class TestVLMBackendsAndEdgeCases(unittest.TestCase):
    """测试 VLM 后端通信、边缘边界与容错处理分支"""

    def test_mock_backend_side_effect(self):
        backend = MockVLMBackend(side_effect=IOError("network failed"))
        with self.assertRaises(IOError):
            backend.request(b"fake_bytes", "prompt")

        # Callable side effect
        def echo_fn(b, p):
            return f"echo: {p}"

        backend2 = MockVLMBackend(side_effect=echo_fn)
        self.assertEqual(backend2.request(b"b", "hello"), "echo: hello")
        self.assertEqual(len(backend2.calls), 1)

    @patch("urllib.request.urlopen")
    def test_ollama_backend_success_and_error(self, mock_urlopen):
        # 成功响应
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": '{"test": 1}'}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OllamaHttpBackend(host="http://localhost:11434")
        resp = backend.request(b"fake_bytes", "hello")
        self.assertEqual(resp, '{"test": 1}')

        # 错误响应
        mock_resp_err = MagicMock()
        mock_resp_err.read.return_value = json.dumps({"error": "model not found"}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp_err

        with self.assertRaises(IOError):
            backend.request(b"fake_bytes", "hello")

    @patch("urllib.request.urlopen")
    def test_openai_compatible_backend_success_and_error(self, mock_urlopen):
        # 成功响应
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"choices": [{"message": {"content": '{"ok": true}'}}]}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        backend = OpenAICompatibleBackend(base_url="http://localhost:8000/v1")
        resp = backend.request(b"fake_bytes", "hello")
        self.assertEqual(resp, '{"ok": true}')

        # 空 choices 错误
        mock_resp_empty = MagicMock()
        mock_resp_empty.read.return_value = json.dumps({"choices": []}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp_empty

        with self.assertRaises(IOError):
            backend.request(b"fake_bytes", "hello")

    def test_grid_row_calibration_edge_cases(self):
        engine = VisualGroundingEngine()

        # 1. 极小图像 (height <= 20) 不崩溃，安全采用几何回退
        tiny_img = Image.new("L", (100, 15), color=255)
        res_tiny = engine.calibrate_grid_row_click(
            grid_image=tiny_img,
            target_row=0,
            nominal_x=50,
            nominal_y=35,
            first_row_height=10,
            row_height=10,
        )
        self.assertIsNotNone(res_tiny.calibrated_y)
        self.assertTrue(res_tiny.snapped)

        # 2. 超出网格高度的大行号 (target_row=999) 边界钳位保护
        normal_img = Image.new("L", (100, 100), color=255)
        res_overflow = engine.calibrate_grid_row_click(
            grid_image=normal_img,
            target_row=999,
            nominal_x=50,
            nominal_y=50,
            first_row_height=30,
            row_height=16,
        )
        self.assertLessEqual(res_overflow.calibrated_y, 100)

    def test_dual_frame_zero_pixel_difference_heuristics(self):
        """当两帧完全无物理像素差异且 VLM 返回空时，启发式裁决为 AMBIGUOUS"""
        same_img = Image.new("RGB", (300, 200), color="white")
        arbitrator = TradeReceiptDiffArbitrator(backend=MockVLMBackend(response_text="{}"))

        res = arbitrator.arbitrate(same_img, same_img)
        self.assertEqual(res.decision, ArbitrationDecision.AMBIGUOUS)
        self.assertEqual(res.suggested_action, "QUERY_TODAY_ENTRUSTS")
        self.assertTrue(any("无任何视觉变化" in r for r in res.reasons))

    def test_dark_mode_ui_not_misdiagnosed_as_black_screen(self):
        """深色主题但有高对比度亮色文字与表格时不应误判为黑屏"""
        dark_img = Image.new("RGB", (400, 300), color=(15, 15, 15))
        draw = ImageDraw.Draw(dark_img)
        # 绘制亮白色表格与文字
        draw.rectangle([(50, 50), (350, 250)], outline=(200, 200, 200), width=2)
        draw.text((60, 60), "DARK THEME STOCK", fill=(255, 255, 255))
        # 绿色通讯灯
        draw.rectangle([(20, 270), (35, 285)], fill=(0, 255, 0))

        watchdog = ClientVisualLivenessWatchdog()
        report = watchdog.inspect(dark_img)
        # 由于方差很大，不属于方差接近0的锁屏黑屏
        self.assertNotEqual(report.state, "BLACK_SCREEN")

    def test_top_right_offline_indicator_detection(self):
        """准确检测右上角通讯指示灯红灯脱机"""
        img = Image.new("RGB", (500, 300), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        # 在右上角 (x: 420~450, y: 10~30) 绘制红色脱机指示灯
        draw.rectangle([(420, 10), (450, 30)], fill=(255, 0, 0))

        watchdog = ClientVisualLivenessWatchdog()
        report = watchdog.inspect(img)
        self.assertFalse(report.is_alive)
        self.assertEqual(report.state, "OFFLINE")
        self.assertEqual(report.details.get("red_indicator_location"), "top_right")

    def test_status_bar_roi_cropping(self):
        """测试 status_bar_roi 准确裁切目标区域"""
        img = Image.new("RGB", (600, 400), color="white")
        backend = MockVLMBackend(response_text='{"rejected": false, "entrust_no": "112233"}')
        verifier = StatusBarToastVerifier(backend=backend)

        res = verifier.verify(img, status_bar_roi=(0, 350, 600, 400))
        self.assertEqual(res.entrust_no, "112233")
        # 校验发送到 VLM 的图像字节确实经过了裁剪
        call_bytes = backend.calls[0]["image_bytes"]
        call_pil = image_to_pil(call_bytes)
        self.assertEqual(call_pil.size, (600, 50))

    def test_negation_in_message_does_not_trigger_false_rejection(self):
        """测试'无废单'等否定修饰语不应误报交易拒绝"""
        resp = '{"rejected": false, "message": "买入委托已申报，无废单", "entrust_no": "667788"}'
        verifier = StatusBarToastVerifier(backend=MockVLMBackend(response_text=resp))
        res = verifier.verify(Image.new("RGB", (200, 100)), raise_on_reject=True)
        self.assertFalse(res.is_rejected)
        self.assertEqual(res.entrust_no, "667788")

    def test_model_reasoning_in_resp_text_does_not_trigger_false_rejection(self):
        """测试模型思考链中提及'未发现资金不足'不应误报拒绝"""
        raw_resp = (
            "经过视觉审计，未发现资金不足或超出涨跌停等废单异常。\n"
            '{"status": "CONFIRMED", "rejected": false, "entrust_no": "998877", "message": "委托已申报"}'
        )
        verifier = StatusBarToastVerifier(backend=MockVLMBackend(response_text=raw_resp))
        res = verifier.verify(Image.new("RGB", (200, 100)), raise_on_reject=True)
        self.assertFalse(res.is_rejected)
        self.assertEqual(res.entrust_no, "998877")

    def test_negated_captcha_does_not_trigger_circuit_breaker(self):
        """测试'无需人机验证'不应误触熔断报警"""
        resp = '{"dialog_type": "CONFIRMATION", "action": "CONFIRM", "message": "委托确认，无需人机验证"}'
        arbitrator = ModalDialogVisualArbitrator(backend=MockVLMBackend(response_text=resp))
        dec = arbitrator.arbitrate(Image.new("RGB", (300, 200)))
        self.assertEqual(dec.dialog_type, "CONFIRMATION")
        self.assertFalse(dec.requires_human)

    def test_plain_text_fallback_risk_disclosure(self):
        """测试非 JSON 纯文本回复时，能够正确提取倒计时、复选框并决策"""
        plain_text = "这是一个带倒计时的风险揭示弹窗，还有8秒倒计时，需要勾选免责声明然后点击确认"
        arbitrator = ModalDialogVisualArbitrator(backend=MockVLMBackend(response_text=plain_text))
        dec = arbitrator.arbitrate(Image.new("RGB", (400, 300)))
        self.assertEqual(dec.dialog_type, "RISK_DISCLOSURE")
        self.assertEqual(dec.countdown_seconds, 8.0)
        self.assertTrue(dec.has_checkbox)
        self.assertEqual(dec.action, "CHECK_AND_WAIT_CONFIRM")

    def test_ground_control_1000_scale_coordinates(self):
        """测试支持 Qwen-VL 等常用 1000-scale 归一化坐标"""
        engine = VisualGroundingEngine()
        # [ymin, xmin, ymax, xmax] 范围 0~1000
        bbox_1000 = [100, 700, 180, 850]
        resp = json.dumps({"found": True, "bbox": bbox_1000})
        engine.backend = MockVLMBackend(response_text=resp)

        img = Image.new("RGB", (500, 300))
        res = engine.ground_control(img, "全撤")
        self.assertTrue(res.found)
        # cx = ((700 + 850)/2000) * 500 = 387
        # cy = ((100 + 180)/2000) * 300 = 42
        self.assertEqual(res.center, (387, 42))

    def test_negative_target_row_raises_value_error(self):
        """测试 target_row 为负数时抛出 ValueError"""
        engine = VisualGroundingEngine()
        with self.assertRaises(ValueError):
            engine.calibrate_grid_row_click(
                grid_image=Image.new("L", (100, 100)),
                target_row=-1,
                nominal_x=50,
                nominal_y=50,
            )

    def test_borderless_flat_grid_fallback(self):
        """测试无水平分割线的平铺网格回退到几何估算并完成吸附"""
        # 纯白平铺网格，没有任何水平分割线
        flat_img = Image.new("L", (200, 150), color=255)
        engine = VisualGroundingEngine()
        # first_row_height=30, row_height=20
        # target_row=0: row_top=30, row_bottom=50, center=40
        # nominal_y=49 (距离 row_bottom 仅 1 像素，属于边界危险区)
        res = engine.calibrate_grid_row_click(
            grid_image=flat_img,
            target_row=0,
            nominal_x=50,
            nominal_y=49,
            first_row_height=30,
            row_height=20,
            safety_margin=3,
        )
        self.assertTrue(res.snapped)
        self.assertEqual(res.calibrated_y, 40)

    def test_dual_frame_raise_on_ambiguous_throws_visual_arbitration_error(self):
        """测试 raise_on_ambiguous=True 时抛出 VisualArbitrationError"""
        arbitrator = TradeReceiptDiffArbitrator(
            backend=MockVLMBackend(response_text='{"decision": "AMBIGUOUS", "confidence": 0.5}')
        )
        with self.assertRaises(VisualArbitrationError) as ctx:
            arbitrator.arbitrate(
                Image.new("RGB", (200, 100)),
                Image.new("RGB", (200, 100)),
                raise_on_ambiguous=True,
            )
        self.assertIsNotNone(ctx.exception.result)

    def test_handle_pop_dialogs_wait_and_confirm(self):
        """测试无复选框纯倒计时风险弹窗 (WAIT_AND_CONFIRM) 在 ClientTrader 中正常闭环"""
        from pywinauto.findwindows import ElementNotFoundError

        trader = ClientTrader()
        trader._app = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="RISK_DISCLOSURE",
            action="WAIT_AND_CONFIRM",
            countdown_seconds=0.0,
            has_checkbox=False,
            button_coord=(200, 250),
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "is_exist_pop_dialog", side_effect=[True, False]):
            with patch.object(
                trader,
                "_get_pop_dialog_title",
                side_effect=ElementNotFoundError("No 1365"),
            ):
                with patch.object(trader, "_click_coords") as mock_click:
                    res = trader._handle_pop_dialogs()
                    mock_click.assert_called_once_with((200, 250))
                    self.assertEqual(res, {"message": "success"})

    def test_handle_pop_dialogs_max_attempts_exceeded(self):
        """测试弹窗未关闭时，防止死循环并在超过最大次数时抛出 TradeVerificationError"""
        from pywinauto.findwindows import ElementNotFoundError

        trader = ClientTrader()
        trader._app = MagicMock()
        trader.visual_oracle = MagicMock()
        trader.visual_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="CONFIRMATION",
            action="CONFIRM",
            button_coord=(100, 100),
        )

        with patch.object(trader, "is_exist_pop_dialog", return_value=True):
            with patch.object(
                trader,
                "_get_pop_dialog_title",
                side_effect=ElementNotFoundError("No 1365"),
            ):
                with patch.object(trader, "_click_coords"):
                    with self.assertRaises(TradeVerificationError) as ctx:
                        trader._handle_pop_dialogs()
                    self.assertIn("超过最大尝试次数", str(ctx.exception))

    def test_ground_control_1000_scale_on_1080p_screen(self):
        """测试 1920x1080 大分辨率屏幕下 1000-scale 坐标准确归一化，严禁跌入绝对像素误差点偏 700 像素"""
        engine = VisualGroundingEngine(coord_format="1000")
        bbox_1000 = [100, 700, 180, 850]
        resp = json.dumps({"found": True, "bbox": bbox_1000})
        engine.backend = MockVLMBackend(response_text=resp)

        img_1080p = Image.new("RGB", (1920, 1080))
        res = engine.ground_control(img_1080p, "全撤")
        self.assertTrue(res.found)
        # cx = ((700 + 850) / 2000.0) * 1920 = 1488
        # cy = ((100 + 180) / 2000.0) * 1080 = 151
        self.assertEqual(res.center, (1488, 151))
        self.assertEqual(res.bbox, (0.10, 0.70, 0.18, 0.85))

    def test_grid_row_missing_header_divider_prevents_adjacent_click(self):
        """测试表头底部分割线未绘制时，自动补全表头边界，严禁将第0行误判为第1行并双击相邻行"""
        engine = VisualGroundingEngine()

        # 构造表头底端无分割线，但数据行之间有分割线的图像 (分割线在 y=50, 70, 90)
        # 表头高 30，每行 20。第0行应该在 y=30~50 (中心 40)
        grid_img = Image.new("L", (200, 150), color=255)
        draw = ImageDraw.Draw(grid_img)
        for y_line in [50, 70, 90]:
            draw.line([(0, y_line), (200, y_line)], fill=0, width=1)

        # target_row = 0, nominal_y = 49 (危险边界区)
        res = engine.calibrate_grid_row_click(
            grid_image=grid_img,
            target_row=0,
            nominal_x=50,
            nominal_y=49,
            first_row_height=30,
            row_height=20,
            safety_margin=3,
        )
        self.assertTrue(res.snapped)
        self.assertEqual(res.row_top, 30)
        self.assertEqual(res.row_bottom, 50)
        self.assertEqual(res.calibrated_y, 40)  # 严格吸附到第0行中心 40，绝不跳到第1行中心 60

    def test_grid_row_last_row_spacing_estimation(self):
        """测试最后一行无底部分割线时，根据已有行间距准确估算边界"""
        engine = VisualGroundingEngine()
        grid_img = Image.new("L", (200, 150), color=255)
        draw = ImageDraw.Draw(grid_img)
        # 仅有 3 条分割线：y=30, 50, 70 (第0行 30~50, 第1行 50~70, 第2行 70~90)
        for y_line in [30, 50, 70]:
            draw.line([(0, y_line), (200, y_line)], fill=0, width=1)

        res = engine.calibrate_grid_row_click(
            grid_image=grid_img,
            target_row=2,
            nominal_x=50,
            nominal_y=71,  # 靠近行顶部分割线
            first_row_height=30,
            row_height=20,
            safety_margin=3,
        )
        self.assertTrue(res.snapped)
        self.assertEqual(res.row_top, 70)
        self.assertEqual(res.row_bottom, 90)
        self.assertEqual(res.calibrated_y, 80)

    def test_dual_frame_physical_zero_diff_overrides_hallucinated_confirmed(self):
        """测试当两帧物理差分为0时，即使模型幻觉返回 SUBMIT_CONFIRMED，也强制降级为 AMBIGUOUS"""
        same_img = Image.new("RGB", (400, 300), color="white")
        # 模型幻觉：明明两张图一模一样，却宣称已提交确认
        hallucinated_resp = json.dumps(
            {
                "decision": "SUBMIT_CONFIRMED",
                "confidence": 0.98,
                "form_cleared": True,
                "funds_frozen": True,
                "mini_flow_added": True,
            }
        )
        arbitrator = TradeReceiptDiffArbitrator(backend=MockVLMBackend(response_text=hallucinated_resp))
        res = arbitrator.arbitrate(same_img, same_img)
        self.assertEqual(res.decision, ArbitrationDecision.AMBIGUOUS)
        self.assertEqual(res.suggested_action, "QUERY_TODAY_ENTRUSTS")
        self.assertTrue(any("无任何视觉变化" in r for r in res.reasons))

    def test_dual_frame_error_detected_overrides_hallucinated_confirmed(self):
        """测试当模型检测到报错时，绝对禁止判定为 SUBMIT_CONFIRMED"""
        img1 = Image.new("RGB", (400, 300), color="white")
        img2 = Image.new("RGB", (400, 300), color="red")
        contradictory_resp = json.dumps(
            {
                "decision": "SUBMIT_CONFIRMED",
                "confidence": 0.90,
                "error_detected": True,
                "form_cleared": False,
            }
        )
        arbitrator = TradeReceiptDiffArbitrator(backend=MockVLMBackend(response_text=contradictory_resp))
        res = arbitrator.arbitrate(img1, img2)
        self.assertEqual(res.decision, ArbitrationDecision.SUBMIT_FAILED)
        self.assertEqual(res.suggested_action, "RETRY")

    def test_dual_frame_no_evidence_overrides_hallucinated_confirmed(self):
        """测试当既无表单清空、又无资金扣减、又无流水新增时，严禁返回 SUBMIT_CONFIRMED"""
        img1 = Image.new("RGB", (400, 300), color="white")
        img2 = Image.new("RGB", (400, 300), color="gray")
        no_evidence_resp = json.dumps(
            {
                "decision": "SUBMIT_CONFIRMED",
                "confidence": 0.95,
                "form_cleared": False,
                "funds_frozen": False,
                "mini_flow_added": False,
            }
        )
        arbitrator = TradeReceiptDiffArbitrator(backend=MockVLMBackend(response_text=no_evidence_resp))
        res = arbitrator.arbitrate(img1, img2)
        self.assertEqual(res.decision, ArbitrationDecision.AMBIGUOUS)
        self.assertEqual(res.suggested_action, "QUERY_TODAY_ENTRUSTS")

    def test_trade_method_dual_frame_anti_duplication_integration(self):
        """测试 ClientTrader.trade() 在无合同编号的早盘高并发无回执阶段，自动触发双帧差分仲裁"""
        trader = ClientTrader()
        trader._app = MagicMock()
        trader._main = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.verify_status_bar_and_toast.return_value = StatusBarToastResult(
            is_rejected=False,
            entrust_no=None,  # 无回执
            status="NONE",
        )
        mock_oracle.arbitrate_trade_receipt.return_value = DualFrameReceiptResult(
            decision=ArbitrationDecision.AMBIGUOUS,
            confidence=0.60,
            form_cleared=False,
            funds_frozen=False,
            mini_flow_added=False,
            suggested_action="QUERY_TODAY_ENTRUSTS",
            reasons=["置信度不足，无回执无流水"],
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "_set_trade_params"):
            with patch.object(trader, "_submit_trade"):
                with patch.object(trader, "_handle_pop_dialogs", return_value={"message": "success"}):
                    res = trader.trade("600000", 10.0, 100)
                    mock_oracle.arbitrate_trade_receipt.assert_called_once()
                    self.assertEqual(res.get("visual_decision"), ArbitrationDecision.AMBIGUOUS)
                    self.assertTrue(res.get("ambiguous"))
                    self.assertEqual(res.get("suggested_action"), "QUERY_TODAY_ENTRUSTS")

    def test_trade_method_dual_frame_failure_raises_trade_error(self):
        """测试 ClientTrader.trade() 双帧仲裁判定下单失败时抛出 TradeError"""
        trader = ClientTrader()
        trader._app = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.verify_status_bar_and_toast.return_value = StatusBarToastResult(
            is_rejected=False,
            entrust_no=None,
            status="NONE",
        )
        mock_oracle.arbitrate_trade_receipt.return_value = DualFrameReceiptResult(
            decision=ArbitrationDecision.SUBMIT_FAILED,
            confidence=0.90,
            form_cleared=False,
            funds_frozen=False,
            mini_flow_added=False,
            suggested_action="RETRY",
            reasons=["表单标红，资金未变动"],
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "_set_trade_params"):
            with patch.object(trader, "_submit_trade"):
                with patch.object(trader, "_handle_pop_dialogs", return_value={"message": "success"}):
                    with self.assertRaises(TradeError) as ctx:
                        trader.trade("600000", 10.0, 100)
                    self.assertIn("双帧视觉仲裁判定下单失败", str(ctx.exception))

    def test_cancel_all_entrusts_visual_grounding_fallback(self):
        """测试券商换肤版无句柄全撤按钮时，自动回退至视觉 Grounding 并点击中心坐标"""
        trader = ClientTrader()
        trader._app = MagicMock()
        trader._main = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.ground_control.return_value = ControlGroundingResult(
            description="全撤",
            found=True,
            center=(350, 45),
        )
        trader.visual_oracle = mock_oracle

        # 模拟 child_window 找不到按钮 (exists() 返回 False)
        mock_btn = MagicMock()
        mock_btn.exists.return_value = False
        trader._app.top_window().child_window.return_value = mock_btn

        with patch.object(trader, "refresh"):
            with patch.object(trader, "_switch_left_menus"):
                with patch.object(trader, "is_exist_pop_dialog", return_value=False):
                    with patch.object(trader, "_click_coords") as mock_click:
                        trader.cancel_all_entrusts()
                        mock_oracle.ground_control.assert_called_once()
                        mock_click.assert_called_once_with((350, 45), window=trader._main)

    def test_switch_left_menus_visual_grounding_fallback(self):
        """测试自绘树控件或无句柄菜单在 SysTreeView32 报错时回退至视觉 Grounding"""
        trader = ClientTrader()
        trader._app = MagicMock()
        trader._main = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.ground_control.return_value = ControlGroundingResult(
            description="当日委托",
            found=True,
            center=(80, 220),
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "close_pop_dialog"):
            with patch.object(trader, "_get_left_menus_handle", side_effect=RuntimeError("No SysTreeView32")):
                with patch.object(trader, "_click_coords") as mock_click:
                    trader._switch_left_menus(["查询[F4]", "当日委托"])
                    mock_oracle.ground_control.assert_called_once()
                    mock_click.assert_called_once_with((80, 220), window=trader._main)

    def test_frosted_white_mask_detection(self):
        """测试准确检测高亮磨砂全屏遮罩锁死 (mean >= 215, std < 12)"""
        watchdog = ClientVisualLivenessWatchdog()
        # 纯浅灰白磨砂蒙层 (rgb 230, 230, 230)
        white_mask = Image.new("RGB", (400, 300), color=(230, 230, 230))
        report = watchdog.inspect(white_mask)
        self.assertFalse(report.is_alive)
        self.assertEqual(report.state, "MASK_LOCKED")
        self.assertEqual(report.recovery_action, "DISMISS_MASK")
        self.assertTrue(any("遮罩锁死" in issue for issue in report.issues))

    def test_extended_rejection_keywords(self):
        """测试捕获券商常见拒绝词汇：无可用资金与已停牌"""
        verifier = StatusBarToastVerifier()
        cases = [
            '{"rejected": false, "message": "委托拒绝：无可用资金"}',
            '{"rejected": false, "message": "委托失败：该证券已停牌，禁止交易"}',
        ]
        for c in cases:
            verifier.backend = MockVLMBackend(response_text=c)
            with self.assertRaises(TradeError):
                verifier.verify(Image.new("RGB", (200, 100)))

    def test_dual_frame_size_mismatch_resilience(self):
        """测试前后双帧尺寸不一致时不崩溃，自适应对齐差分"""
        img1 = Image.new("RGB", (300, 200), color="white")
        img2 = Image.new("RGB", (400, 250), color="white")
        arbitrator = TradeReceiptDiffArbitrator()
        # 只要不抛出 ValueError: images do not match 即说明保护生效
        res = arbitrator.arbitrate(img1, img2)
        self.assertIsNotNone(res.decision)

    def test_handle_pop_dialogs_timeout_error_transitions_to_oracle(self):
        """测试当 _get_pop_dialog_title 抛出 TimeoutError 时安全平滑过渡到视觉仲裁"""
        import pywinauto.timings
        trader = ClientTrader()
        trader._app = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="CONFIRMATION",
            action="CONFIRM",
            button_coord=(120, 150),
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "is_exist_pop_dialog", side_effect=[True, False]):
            with patch.object(
                trader,
                "_get_pop_dialog_title",
                side_effect=pywinauto.timings.TimeoutError("Wait timeout"),
            ):
                with patch.object(trader, "_click_coords") as mock_click:
                    res = trader._handle_pop_dialogs()
                    mock_oracle.arbitrate_modal_dialog.assert_called_once()
    def test_normalize_bbox_1000_scale_auto_mode_on_1080p_without_explicit_coord_format(self):
        """测试 1080p 屏幕下不显式指定 coord_format 时，auto 模式自动正确推断 1000-scale 坐标"""
        norm_bbox, center = normalize_bbox_and_center([100, 700, 180, 850], 1920, 1080, coord_format=None)
        self.assertEqual(center, (1488, 151))
        self.assertEqual(norm_bbox, (0.10, 0.70, 0.18, 0.85))

    def test_status_bar_verifier_negation_not_falsely_negated_by_unrestricted_price(self):
        """测试'废单：无限制价格'中的'无限制'不会错误否定'废单'关键字"""
        verifier = StatusBarToastVerifier()
        cases = [
            '{"message": "废单：无涨跌幅限制，买入超限"}',
            '{"message": "废单：无限制价格申报失败"}',
        ]
        for c in cases:
            verifier.backend = MockVLMBackend(response_text=c)
            with self.assertRaises(TradeError):
                verifier.verify(Image.new("RGB", (200, 100)))

    def test_status_bar_verifier_inherently_negative_rejection_keywords(self):
        """测试本身包含否定字眼的拒绝原因 (无交易权限、无可用资金) 绝不被误判为被否定"""
        verifier = StatusBarToastVerifier()
        for text in ["提示：无交易权限，禁止委托", "提示：无可用资金，委托被拒绝"]:
            verifier.backend = MockVLMBackend(response_text=f'{{"message": "{text}"}}')
            with self.assertRaises(TradeError):
                verifier.verify(Image.new("RGB", (200, 100)))

    def test_dual_frame_unparsed_plain_text_identical_frames_overrides_to_ambiguous(self):
        """测试模型返回非 JSON 纯文本时，若两帧物理像素完全相同，绝对禁止返回 SUBMIT_CONFIRMED"""
        same_img = Image.new("RGB", (300, 200), color="white")
        arbitrator = TradeReceiptDiffArbitrator(
            backend=MockVLMBackend(response_text="分析完毕，看起来界面提交成功了")
        )
        res = arbitrator.arbitrate(same_img, same_img)
        self.assertEqual(res.decision, ArbitrationDecision.AMBIGUOUS)
        self.assertEqual(res.suggested_action, "QUERY_TODAY_ENTRUSTS")
        self.assertTrue(any("无任何视觉变化" in r for r in res.reasons))

    def test_dual_frame_unparsed_plain_text_negated_success(self):
        """测试模型纯文本中包含否定成功词语 (未提交成功，没有清空) 时绝不误判为确认"""
        img1 = Image.new("RGB", (300, 200), color="white")
        img2 = Image.new("RGB", (300, 200), color="gray")
        arbitrator = TradeReceiptDiffArbitrator(
            backend=MockVLMBackend(response_text="分析发现：未提交成功，表单没有清空，请核对")
        )
        res = arbitrator.arbitrate(img1, img2)
        self.assertNotEqual(res.decision, ArbitrationDecision.SUBMIT_CONFIRMED)

    def test_trade_method_ambiguous_diff_updates_res_message_preventing_false_success(self):
        """测试当双帧仲裁判定为 AMBIGUOUS 时，trade() 覆盖原 success 提示，彻底根除假成功"""
        trader = ClientTrader()
        mock_oracle = MagicMock()
        mock_oracle.verify_status_bar_and_toast.return_value = StatusBarToastResult(
            is_rejected=False,
            entrust_no=None,
            status="NONE",
        )
        mock_oracle.arbitrate_trade_receipt.return_value = DualFrameReceiptResult(
            decision=ArbitrationDecision.AMBIGUOUS,
            confidence=0.5,
            form_cleared=False,
            funds_frozen=False,
            mini_flow_added=False,
            reasons=["双帧无变化"],
            suggested_action="QUERY_TODAY_ENTRUSTS",
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "_set_trade_params"):
            with patch.object(trader, "_capture_screen_image", return_value=Image.new("RGB", (100, 100))):
                with patch.object(trader, "_submit_trade"):
                    with patch.object(trader, "_handle_pop_dialogs", return_value={"message": "success"}):
                        res = trader.trade("000001", 10.0, 100)
                        self.assertNotEqual(res.get("message"), "success")
                        self.assertIn("ambiguous", res.get("message", ""))
                        self.assertTrue(res.get("ambiguous"))

    def test_liveness_watchdog_market_ticker_red_text_not_misdiagnosed_as_offline(self):
        """测试状态栏中央的红色行情指数 (SH000001 +1.50% 3350.21) 不会被误判为通讯脱机红灯"""
        watchdog = ClientVisualLivenessWatchdog()
        img = Image.new("RGB", (800, 600), color=(230, 230, 230))
        draw = ImageDraw.Draw(img)
        draw.rectangle([(10, 10), (790, 500)], fill=(255, 255, 255), outline=(180, 180, 180))
        # 绘制状态栏中央的红色股票指数跑马灯
        draw.text((300, 550), "SH000001 +1.50% 3350.21", fill=(255, 0, 0))

        report = watchdog.inspect(img)
        self.assertTrue(report.is_alive)
        self.assertEqual(report.state, "HEALTHY")

    def test_liveness_watchdog_extract_hwnd_from_window_specification(self):
        """测试从 WindowSpecification 对象通过 wrapper_object().handle 正确提取 hwnd 并检测死锁"""
        watchdog = ClientVisualLivenessWatchdog()
        mock_trader = MagicMock()
        mock_spec = MagicMock()
        del mock_spec.handle  # WindowSpecification 没有 handle 属性
        mock_wrapper = MagicMock()
        mock_wrapper.handle = 77889
        mock_spec.wrapper_object.return_value = mock_wrapper
        mock_trader._main = mock_spec

        with patch("ctypes.windll.user32.IsHungAppWindow", return_value=1):
            report = watchdog.inspect(Image.new("RGB", (100, 100)), trader=mock_trader)
            self.assertFalse(report.is_alive)
            self.assertEqual(report.state, "MESSAGE_PUMP_HUNG")
            self.assertEqual(report.recovery_action, "RESTART_PROCESS")

    def test_handle_pop_dialogs_unhandled_title_falls_back_to_visual_arbitration(self):
        """测试当弹窗控件 1365 包含 TradePopDialogHandler 不认识的标题 (如人机校验) 时，回退至视觉仲裁并熔断"""
        trader = ClientTrader()
        trader._app = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="CAPTCHA",
            action="CIRCUIT_BREAK",
            requires_human=True,
            message="请完成滑块验证",
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "is_exist_pop_dialog", side_effect=[True, True, False]):
            with patch.object(trader, "_get_pop_dialog_title", return_value="人机校验"):
                with self.assertRaises(HumanInterventionRequiredError):
                    trader._handle_pop_dialogs()

    def test_handle_pop_dialogs_handler_exception_falls_back_to_visual_arbitration(self):
        """测试当 handler.handle 内部控件缺失抛出异常时，平滑回退至视觉智能仲裁"""
        trader = ClientTrader()
        trader._app = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="CONFIRMATION",
            action="CONFIRM",
            button_coord=(50, 50),
        )
        trader.visual_oracle = mock_oracle

        mock_handler = MagicMock()
        mock_handler.handle.side_effect = RuntimeError("Static text control missing")
        mock_handler_class = MagicMock(return_value=mock_handler)

        with patch.object(trader, "is_exist_pop_dialog", side_effect=[True, False]):
            with patch.object(trader, "_get_pop_dialog_title", return_value="提示"):
                with patch.object(trader, "_click_coords") as mock_click:
                    res = trader._handle_pop_dialogs(handler_class=mock_handler_class)
                    mock_oracle.arbitrate_modal_dialog.assert_called_once()
                    mock_click.assert_called_once_with((50, 50))
                    self.assertEqual(res, {"message": "success"})

    def test_handle_pop_dialogs_directui_mask_locked_falls_back_to_visual_arbitration(self):
        """测试当 is_exist_pop_dialog 为 False 但存在 DirectUI 全屏蒙层锁定时，触发视觉仲裁与自愈"""
        trader = ClientTrader()
        trader._app = MagicMock()
        mock_oracle = MagicMock()
        mock_oracle.inspect_liveness.return_value = LivenessReport(
            is_alive=False,
            state="MASK_LOCKED",
            issues=["暗色蒙层锁死"],
            recovery_action="DISMISS_MASK",
        )
        mock_oracle.arbitrate_modal_dialog.return_value = DialogDecision(
            dialog_type="CONFIRMATION",
            action="CONFIRM",
            button_coord=(100, 100),
        )
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "is_exist_pop_dialog", return_value=False):
            with patch.object(trader, "_capture_screen_image", return_value=Image.new("RGB", (200, 200))):
                with patch.object(trader, "_click_coords") as mock_click:
                    res = trader._handle_pop_dialogs()
                    mock_oracle.arbitrate_modal_dialog.assert_called_once()
                    mock_click.assert_called_once_with((100, 100))
                    self.assertEqual(res, {"message": "success"})

    def test_switch_left_menus_nested_folded_traversal(self):
        """测试折叠菜单树在叶子节点隐藏时，依次展开父级菜单层级并成功点击"""
        trader = ClientTrader()
        trader._app = MagicMock()
        trader._main = MagicMock()
        mock_oracle = MagicMock()

        # 模拟第一次寻找'当日委托'未找到，但依次寻找'查询[F4]'和'当日委托'成功
        mock_oracle.ground_control.side_effect = [
            ControlGroundingResult(description="当日委托", found=False),
            ControlGroundingResult(description="查询[F4]", found=True, center=(50, 100)),
            ControlGroundingResult(description="当日委托", found=True, center=(60, 140)),
        ]
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "close_pop_dialog"):
            with patch.object(trader, "_get_left_menus_handle", side_effect=RuntimeError("SysTreeView32 missing")):
                with patch.object(trader, "_click_coords") as mock_click:
                    trader._switch_left_menus(["查询[F4]", "当日委托"])
                    self.assertEqual(mock_click.call_count, 2)
                    mock_click.assert_any_call((50, 100), window=trader._main)
                    mock_click.assert_any_call((60, 140), window=trader._main)

    def test_grid_row_center_misalignment_fallback_prevents_adjacent_row_click(self):
        """测试当探测到的分割线使中心偏移超过半行时，自动回退到理论几何边界，严禁误点相邻行"""
        engine = VisualGroundingEngine()

        # 表头30，每行16。第0行理论为 [30, 46]，中心 38。
        # 构造异常分割线 dividers = [30, 56]，使行边界变为 [30, 56]，中心 43 (偏移 > 0.5 * 16)
        grid_img = Image.new("L", (100, 100), color=255)
        draw = ImageDraw.Draw(grid_img)
        draw.line([(0, 30), (100, 30)], fill=0, width=1)
        draw.line([(0, 56), (100, 56)], fill=0, width=1)

        res = engine.calibrate_grid_row_click(
            grid_image=grid_img,
            target_row=0,
            nominal_x=50,
            nominal_y=38,
            first_row_height=30,
            row_height=16,
        )
        # 应回退至理论行边界 [30, 46]，中心 38，绝不偏移到 43
        self.assertEqual(res.row_top, 30)
        self.assertEqual(res.row_bottom, 46)
        self.assertEqual(res.calibrated_y, 38)

    def test_trader_check_liveness_integration(self):
        """测试 ClientTrader.check_liveness() 非侵入式健康巡检接口"""
        trader = ClientTrader()
        mock_oracle = MagicMock()
        mock_oracle.inspect_liveness.return_value = LivenessReport(is_alive=True, state="HEALTHY")
        trader.visual_oracle = mock_oracle

        with patch.object(trader, "_capture_screen_image"):
            report = trader.check_liveness()
            self.assertTrue(report.is_alive)
            self.assertEqual(report.state, "HEALTHY")


if __name__ == "__main__":
    unittest.main()

