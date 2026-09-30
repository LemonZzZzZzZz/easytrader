# -*- coding: utf-8 -*-
"""
VLMVisualOracle: 基于视觉多模态大模型的交易 GUI 全流程视觉安全卫士体系

落地五大核心安全兜底场景：
R1. 自绘状态栏与浮动 Toast 拒单捕获 (StatusBarToastVerifier)
R2. 未知与多态阻断弹窗视觉智能仲裁 (ModalDialogVisualArbitrator)
R3. 委托终态双帧差分仲裁 (TradeReceiptDiffArbitrator)
R4. 无句柄控件视觉 Grounding 与自适应点击 (VisualGroundingEngine)
R5. 客户端存活与状态视觉看门狗 (ClientVisualLivenessWatchdog)
"""

import abc
import base64
import ctypes
import io
import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from PIL import Image, ImageChops, ImageStat

from easytrader import exceptions
from easytrader.exceptions import (
    HumanInterventionRequiredError,
    TradeError,
    TradeVerificationError,
    VisualArbitrationError,
)

logger = logging.getLogger("easytrader.vlm_visual_oracle")


# ============================================================================
# VLM Backends (Interfaces & Implementations)
# ============================================================================

class IVLMBackend(abc.ABC):
    """VLM 多模态大模型后端抽象接口"""

    @abc.abstractmethod
    def request(self, image_bytes: bytes, prompt: str) -> str:
        """
        发送图像字节和提示词给 VLM 模型，返回模型生成的文本内容
        :param image_bytes: 图像 PNG/JPEG 原始字节
        :param prompt: 提示词
        :return: 文本回复 (通常为 JSON 格式)
        """
        pass


class MockVLMBackend(IVLMBackend):
    """用于测试与离线模拟的 Mock VLM 后端"""

    def __init__(
        self,
        response_text: str = "{}",
        side_effect: Optional[Union[Exception, Callable[[bytes, str], str]]] = None,
    ):
        self.response_text = response_text
        self.side_effect = side_effect
        self.calls: List[Dict[str, Any]] = []

    def request(self, image_bytes: bytes, prompt: str) -> str:
        self.calls.append({"image_bytes": image_bytes, "prompt": prompt})
        if self.side_effect is not None:
            if isinstance(self.side_effect, Exception):
                raise self.side_effect
            if callable(self.side_effect):
                return self.side_effect(image_bytes, prompt)
        return self.response_text


class OllamaHttpBackend(IVLMBackend):
    """Ollama HTTP 本地 VLM 模型后端"""

    def __init__(
        self,
        model: str = "qwen2.5-vl:7b",
        host: str = "http://localhost:11434",
        timeout: float = 60.0,
        options: Optional[Dict[str, Any]] = None,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.options = options or {"num_predict": 2048, "temperature": 0}

    def request(self, image_bytes: bytes, prompt: str) -> str:
        url = f"{self.host}/api/generate"
        b64_img = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64_img],
            "stream": False,
            "format": "json",
            "options": self.options,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                resp_bytes = response.read()
                resp_json = json.loads(resp_bytes.decode("utf-8"))
                if "error" in resp_json:
                    raise IOError(f"Ollama API returned error: {resp_json['error']}")
                return resp_json.get("response", "")
        except Exception as e:
            logger.error("OllamaHttpBackend request failed: %s", e)
            raise IOError(f"Ollama request error: {e}") from e


class OpenAICompatibleBackend(IVLMBackend):
    """OpenAI 兼容接口的 VLM 后端 (支持 vLLM, DeepSeek, Qwen 等)"""

    def __init__(
        self,
        model: str = "qwen2.5-vl:7b",
        base_url: str = "http://localhost:8000/v1",
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "EMPTY"
        self.timeout = timeout

    def request(self, image_bytes: bytes, prompt: str) -> str:
        url = f"{self.base_url}/chat/completions"
        b64_img = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_img}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.0,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                resp_bytes = response.read()
                resp_json = json.loads(resp_bytes.decode("utf-8"))
                if "error" in resp_json:
                    raise IOError(
                        f"OpenAI compatible API returned error: {resp_json['error']}"
                    )
                choices = resp_json.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
                raise IOError(f"OpenAI compatible API returned empty choices: {resp_json}")
        except Exception as e:
            logger.error("OpenAICompatibleBackend request failed: %s", e)
            raise IOError(f"OpenAI compatible request error: {e}") from e


# ============================================================================
# Utilities
# ============================================================================

def image_to_bytes(img: Union[Image.Image, bytes, np.ndarray, str]) -> bytes:
    """将多种图像输入格式安全转换为 PNG 字节流"""
    if isinstance(img, bytes):
        return img
    if isinstance(img, str):
        if not os.path.exists(img):
            raise FileNotFoundError(f"图像文件不存在: {img}")
        with open(img, "rb") as f:
            return f.read()
    if isinstance(img, np.ndarray):
        pil_img = Image.fromarray(img)
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()
    if isinstance(img, Image.Image):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    raise TypeError(f"不支持的图像格式类型: {type(img)}")


def image_to_pil(img: Union[Image.Image, bytes, np.ndarray, str]) -> Image.Image:
    """将多种图像输入格式转换为 PIL Image 对象 (统一输出 RGB 模式)"""
    if isinstance(img, Image.Image):
        return img.convert("RGB") if img.mode != "RGB" else img
    if isinstance(img, bytes):
        return Image.open(io.BytesIO(img)).convert("RGB")
    if isinstance(img, str):
        return Image.open(img).convert("RGB")
    if isinstance(img, np.ndarray):
        return Image.fromarray(img).convert("RGB")
    raise TypeError(f"不支持的图像格式类型: {type(img)}")


def extract_json_from_response(text: str) -> Dict[str, Any]:
    """从模型回复文本中鲁棒提取 JSON 对象"""
    text = text.strip()
    # 尝试匹配 ```json ... ``` 块
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block_match:
        try:
            return json.loads(code_block_match.group(1))
        except Exception:
            pass
    # 尝试非贪婪提取最外层或首个有效 JSON 字典
    for match in re.finditer(r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})", text, re.DOTALL):
        try:
            res = json.loads(match.group(1))
            if isinstance(res, dict) and res:
                return res
        except Exception:
            pass
    # 尝试匹配首尾最宽范围的大括号
    brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except Exception:
            pass
    # 直接解析
    try:
        res = json.loads(text)
        if isinstance(res, dict):
            return res
    except Exception:
        pass
    return {}


def normalize_bbox_and_center(
    bbox: Any,
    width: int,
    height: int,
    coord_format: Optional[str] = None,
) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[Tuple[int, int]]]:
    """
    鲁棒解析不同视觉模型输出的 BBox，统一转换为归一化 [ymin, xmin, ymax, xmax] (0.0~1.0)
    及绝对像素中心坐标 (cx, cy)。
    兼容格式：
    1. 归一化浮点 [0.0, 1.0] (coord_format='norm')
    2. Qwen-VL 等常用千分比 [0, 1000] (coord_format='1000')
    3. 绝对像素坐标 [0, width / height] (coord_format='pixel')
    4. 自动推断 (coord_format='auto' 或 None)
    """
    if not bbox or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None, None
    try:
        y1, x1, y2, x2 = [float(v) for v in bbox]
        ymin, ymax = min(y1, y2), max(y1, y2)
        xmin, xmax = min(x1, x2), max(x1, x2)

        max_val = max(ymin, xmin, ymax, xmax)
        fmt = (coord_format or "auto").lower()

        if fmt == "norm" or (fmt == "auto" and max_val <= 1.0):
            # 模式 1: 0.0 ~ 1.0 归一化坐标
            norm_bbox = (ymin, xmin, ymax, xmax)
            cx = int(((xmin + xmax) / 2.0) * width)
            cy = int(((ymin + ymax) / 2.0) * height)
        elif fmt == "1000" or (
            fmt == "auto"
            and 1.0 < max_val <= 1000.0
        ):
            # 模式 2: 1000-scale 归一化 (如 Qwen2.5-VL 常用格式)
            norm_bbox = (ymin / 1000.0, xmin / 1000.0, ymax / 1000.0, xmax / 1000.0)
            cx = int(((xmin + xmax) / 2000.0) * width)
            cy = int(((ymin + ymax) / 2000.0) * height)
        else:
            # 模式 3: 绝对像素坐标
            norm_bbox = (
                max(0.0, min(1.0, ymin / max(1, height))),
                max(0.0, min(1.0, xmin / max(1, width))),
                max(0.0, min(1.0, ymax / max(1, height))),
                max(0.0, min(1.0, xmax / max(1, width))),
            )
            cx = int((xmin + xmax) / 2.0)
            cy = int((ymin + ymax) / 2.0)

        # 边界约束保护
        cx = max(0, min(width - 1, cx))
        cy = max(0, min(height - 1, cy))
        return norm_bbox, (cx, cy)
    except Exception:
        return None, None


def compose_dual_frames(
    before: Image.Image,
    after: Image.Image,
    label_height: int = 24,
) -> Image.Image:
    """
    将下单前后的双帧局部图像水平拼接为单张对比图，带有显式视觉分界与中英文状态标题，
    确保发送给 VLM 时模型能够清晰对比下单前后的表单、资金与流水差异。
    """
    from PIL import ImageDraw

    w1, h1 = before.size
    w2, h2 = after.size
    total_w = w1 + w2 + 8  # 8px 分隔区
    total_h = max(h1, h2) + label_height

    composite = Image.new("RGB", (total_w, total_h), color=(240, 240, 240))
    draw = ImageDraw.Draw(composite)
    draw.text((8, 4), "[Before] Frame A", fill=(40, 40, 40))
    draw.text((w1 + 16, 4), "[After] Frame B", fill=(40, 40, 40))
    draw.line([(w1 + 4, 0), (w1 + 4, total_h)], fill=(160, 160, 160), width=2)
    composite.paste(before, (0, label_height))
    composite.paste(after, (w1 + 8, label_height))
    return composite


# ============================================================================
# R1: 自绘状态栏与浮动 Toast 拒单捕获 (StatusBarToastVerifier)
# ============================================================================

REJECTION_KEYWORDS = [
    "资金不足",
    "可用资金不足",
    "无可用资金",
    "没有可用资金",
    "余额不足",
    "超出涨跌停",
    "超出涨停",
    "超出跌停",
    "涨跌停限制",
    "废单",
    "废单原因",
    "委托失败",
    "下单失败",
    "无效委托",
    "拒绝",
    "超限",
    "买入金额超限",
    "不可买入",
    "禁止交易",
    "非交易时间",
    "不在交易时间",
    "没有足够的资金",
    "申报失败",
    "无交易权限",
    "没有交易权限",
    "权限不足",
    "额度不足",
    "头寸不足",
    "证券代码有误",
    "证券代码错误",
    "数量不符",
    "价格不符",
    "无权委托",
    "禁止买入",
    "禁止卖出",
    "账户冻结",
    "已停牌",
    "停牌",
    "休市",
]


@dataclass
class StatusBarToastResult:
    is_rejected: bool
    reject_reason: Optional[str] = None
    entrust_no: Optional[str] = None
    message: str = ""
    status: str = "NONE"  # REJECTED, CONFIRMED, NONE
    raw_response: str = ""


class StatusBarToastVerifier:
    """
    自绘状态栏与浮动 Toast 拒单捕获器
    在下单提交后精准提取底部状态栏与中央/右上角 Toast 提示，
    捕获拒单原因并抛出强类型 TradeError，或提取真实合同编号补全返回字典。
    """

    def __init__(self, backend: Optional[IVLMBackend] = None):
        self.backend = backend or MockVLMBackend()

    def verify(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        status_bar_roi: Optional[Tuple[int, int, int, int]] = None,
        toast_roi: Optional[Tuple[int, int, int, int]] = None,
        raise_on_reject: bool = True,
    ) -> StatusBarToastResult:
        """
        验证屏幕图像中的状态栏与 Toast 文本。
        若检测到废单/拒绝文本，在 raise_on_reject=True 时抛出 TradeError；
        若检测到已申报及合同编号，返回提取到的编号。
        """
        img_pil = image_to_pil(image)

        # 处理 ROI 截取
        if status_bar_roi is not None or toast_roi is not None:
            if status_bar_roi is not None and toast_roi is None:
                img_pil = img_pil.crop(status_bar_roi)
            elif toast_roi is not None and status_bar_roi is None:
                img_pil = img_pil.crop(toast_roi)
            else:
                sb_crop = img_pil.crop(status_bar_roi)
                toast_crop = img_pil.crop(toast_roi)
                cw = max(sb_crop.width, toast_crop.width)
                ch = sb_crop.height + toast_crop.height
                stitched = Image.new("RGB", (cw, ch), color="white")
                stitched.paste(toast_crop, (0, 0))
                stitched.paste(sb_crop, (0, toast_crop.height))
                img_pil = stitched

        img_bytes = image_to_bytes(img_pil)
        prompt = (
            "你是一个证券交易系统视觉审计专家。请分析交易界面截图中底部的状态栏以及屏幕中央或右上角的浮动 Toast 提示信息。\n"
            "判断是否有废单、委托拒绝提示（如“资金不足”、“超出涨跌停”、“无效委托”等），或者委托申报成功提示（如“已申报”、“合同编号: xxx”）。\n"
            "请严格输出 JSON 格式：\n"
            "{\n"
            '  "status": "REJECTED" | "CONFIRMED" | "NONE",\n'
            '  "rejected": true | false,\n'
            '  "reject_reason": "废单或拒绝原因描述" (若无填 null),\n'
            '  "entrust_no": "提取到的合同编号或委托编号" (若无填 null),\n'
            '  "message": "提取到的完整提示文本"\n'
            "}"
        )

        resp_text = self.backend.request(img_bytes, prompt)
        parsed = extract_json_from_response(resp_text)

        message = str(parsed.get("message", "") or "")
        is_rejected = bool(parsed.get("rejected", False))
        reject_reason = parsed.get("reject_reason")
        entrust_no = parsed.get("entrust_no")
        if entrust_no is not None:
            entrust_no = str(entrust_no).strip()

        # 检查是否包含拒绝关键词
        # 若 parsed 解析出有效字段，优先检查实际业务文本 (message & reject_reason)
        # 仅当 parsed 为空（非 JSON 纯文本回复）时才回退至完整 resp_text，防止模型推理过程中的词汇造成误判
        text_to_check = f"{message} {reject_reason or ''}" if parsed else resp_text

        for kw in REJECTION_KEYWORDS:
            if kw in text_to_check:
                # 检查该关键词是否被否定词直接修饰 (如 "无废单", "未见超出", "没有失败")
                # 若关键词本身就以否定词开头 (如 "无可用资金", "没有可用资金", "无交易权限")，绝不视为被否定
                inherently_negative = any(
                    kw.startswith(prefix) for prefix in ("无", "没有", "未", "非")
                )
                if not inherently_negative:
                    negation_pattern = rf"(?:无|没有|未|非|0笔)[\s]*{re.escape(kw)}"
                    if re.search(negation_pattern, text_to_check):
                        continue

                is_rejected = True
                if not reject_reason:
                    reject_reason = kw
                break

        # 尝试通过正则提取合同编号或委托编号
        full_text_for_regex = f"{message} {reject_reason or ''} {resp_text}"
        if not entrust_no:
            entrust_match = re.search(
                r"(?:合同编号|委托编号|合同号|委托号|申报号)[\s:：]*([0-9a-zA-Z]+)",
                full_text_for_regex,
            )
            if entrust_match:
                entrust_no = entrust_match.group(1).strip()
            elif "已申报" in full_text_for_regex:
                num_match = re.search(r"已申报[\s:：]*([0-9a-zA-Z]+)", full_text_for_regex)
                if num_match:
                    entrust_no = num_match.group(1).strip()

        # 判定状态
        status = "NONE"
        if is_rejected:
            status = "REJECTED"
        elif entrust_no or ("已申报" in full_text_for_regex) or ("成功" in full_text_for_regex):
            status = "CONFIRMED"

        result = StatusBarToastResult(
            is_rejected=is_rejected,
            reject_reason=reject_reason,
            entrust_no=entrust_no,
            message=message,
            status=status,
            raw_response=resp_text,
        )

        # 关键约束：提取到废单/拒绝文本时必须抛出 TradeError，严禁返回假成功！
        if is_rejected and raise_on_reject:
            if message:
                err_msg = message
                if reject_reason and reject_reason not in message:
                    err_msg = f"{reject_reason}: {message}"
            else:
                err_msg = reject_reason or "交易委托被拒绝（状态栏/Toast提示废单）"
            raise TradeError(f"交易废单拒绝: {err_msg}")

        return result


# ============================================================================
# R2: 未知与多态阻断弹窗视觉智能仲裁 (ModalDialogVisualArbitrator)
# ============================================================================

@dataclass
class DialogDecision:
    dialog_type: str  # CAPTCHA, RISK_DISCLOSURE, PASSWORD_EXPIRY, REJECTION, CONFIRMATION, UNKNOWN
    action: str  # CIRCUIT_BREAK, CHECK_AND_WAIT_CONFIRM, SKIP, CONFIRM, RAISE_ERROR, UNRESOLVED
    countdown_seconds: float = 0.0
    has_checkbox: bool = False
    checkbox_coord: Optional[Tuple[int, int]] = None
    button_coord: Optional[Tuple[int, int]] = None
    button_text: Optional[str] = None
    requires_human: bool = False
    message: str = ""
    raw_response: str = ""


class ModalDialogVisualArbitrator:
    """
    未知与多态阻断弹窗视觉智能仲裁器
    针对 DirectUI 蒙层、带倒计时风险揭示、前置免责勾选框、密码到期改密提示等非标弹窗进行意图识别与安全闭环决策。
    """

    def __init__(
        self,
        backend: Optional[IVLMBackend] = None,
        coord_format: Optional[str] = None,
    ):
        self.backend = backend or MockVLMBackend()
        self.coord_format = coord_format
        if self.coord_format is None:
            model_name = str(getattr(self.backend, "model", "")).lower()
            if "qwen" in model_name:
                self.coord_format = "1000"

    def arbitrate(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        strict: bool = True,
        coord_format: Optional[str] = None,
    ) -> DialogDecision:
        """
        对弹窗截图进行视觉智能仲裁。
        遇到图形验证码 (CAPTCHA) 立即触发 HumanInterventionRequiredError 熔断；
        遇到未识别未知阻断弹窗且 strict=True 时，严禁盲目静默返回 success。
        """
        img_pil = image_to_pil(image)
        width, height = img_pil.size
        img_bytes = image_to_bytes(image)

        prompt = (
            "你是一个 GUI 自动化与交易弹窗决策专家。请分析截图中出现的未知阻断弹窗或 DirectUI 蒙层。\n"
            "判断弹窗类型：\n"
            "1. 是否是图形验证码/人机校验 (CAPTCHA)？\n"
            "2. 是否是带倒计时或免责声明勾选的风险揭示书 (RISK_DISCLOSURE)？\n"
            "3. 是否是密码过期/修改提醒 (PASSWORD_EXPIRY)？\n"
            "4. 是否是交易报错/拒绝弹窗 (REJECTION)？\n"
            "5. 是否是普通确认弹窗 (CONFIRMATION)？\n"
            "输出严格 JSON:\n"
            "{\n"
            '  "dialog_type": "CAPTCHA" | "RISK_DISCLOSURE" | "PASSWORD_EXPIRY" | "REJECTION" | "CONFIRMATION" | "UNKNOWN",\n'
            '  "has_captcha": true | false,\n'
            '  "countdown_seconds": 0,\n'
            '  "has_checkbox": true | false,\n'
            '  "checkbox_bbox": [ymin, xmin, ymax, xmax] (0.0~1.0, 若无填 null),\n'
            '  "action": "CIRCUIT_BREAK" | "CHECK_AND_WAIT_CONFIRM" | "SKIP" | "CONFIRM" | "RAISE_ERROR" | "UNRESOLVED",\n'
            '  "action_button_bbox": [ymin, xmin, ymax, xmax] (0.0~1.0, 若无填 null),\n'
            '  "action_button_text": "确定" | "稍后提醒" | "跳过" | "取消",\n'
            '  "message": "弹窗核心文本或提示"\n'
            "}"
        )

        resp_text = self.backend.request(img_bytes, prompt)
        parsed = extract_json_from_response(resp_text)

        dialog_type = str(parsed.get("dialog_type", "UNKNOWN")).upper()
        has_captcha = bool(parsed.get("has_captcha", False))
        countdown_seconds = float(parsed.get("countdown_seconds", 0.0) or 0.0)
        has_checkbox = bool(parsed.get("has_checkbox", False))
        message = str(parsed.get("message", "") or "")
        action_button_text = parsed.get("action_button_text")

        fmt = coord_format or self.coord_format
        # 坐标解析使用通用归一化函数
        _, checkbox_coord = normalize_bbox_and_center(
            parsed.get("checkbox_bbox"), width, height, coord_format=fmt
        )
        _, button_coord = normalize_bbox_and_center(
            parsed.get("action_button_bbox"), width, height, coord_format=fmt
        )

        # 文本辅助分析：优先业务文本，仅在 parsed 为空时回退到 resp_text
        text_for_heuristics = f"{message} {action_button_text or ''}" if parsed else resp_text
        raw_check = text_for_heuristics.lower()

        # 排除否定语境（如“无验证码”、“无需人机验证”）
        negated_captcha = bool(
            re.search(r"(?:无|没有|未|无需|免)[\s]*(?:验证码|captcha|人机|滑块|点选)", text_for_heuristics)
        )
        if not negated_captcha and any(
            kw in raw_check for kw in ["验证码", "captcha", "人机验证", "滑块", "点选"]
        ):
            has_captcha = True
            dialog_type = "CAPTCHA"

        if dialog_type == "UNKNOWN":
            if any(kw in raw_check for kw in ["风险", "揭示", "协议", "免责"]):
                dialog_type = "RISK_DISCLOSURE"
            elif "密码" in raw_check and ("过期" in raw_check or "修改" in raw_check):
                dialog_type = "PASSWORD_EXPIRY"
            elif any(kw in raw_check for kw in REJECTION_KEYWORDS):
                dialog_type = "REJECTION"
            elif any(kw in raw_check for kw in ["确认", "提示", "通知", "请确认"]):
                dialog_type = "CONFIRMATION"

        # 针对纯文本回复，辅助提取复选框
        if not has_checkbox and any(
            kw in raw_check for kw in ["勾选", "复选框", "已阅读", "同意并阅读", "checkbox"]
        ):
            has_checkbox = True

        # 倒计时提取辅助
        if countdown_seconds == 0.0:
            cd_match = re.search(r"(\d+)\s*(?:秒|s|S)", raw_check)
            if cd_match:
                countdown_seconds = float(cd_match.group(1))

        # 决策生成与熔断响应
        if has_captcha or dialog_type == "CAPTCHA":
            # 关键约束：遇到 CAPTCHA 人机验证码立即触发熔断报警抛出 HumanInterventionRequiredError
            raise HumanInterventionRequiredError(
                f"识别到图形验证码或人机验证，立即触发熔断保护: {message or 'CAPTCHA detected'}"
            )

        action = str(parsed.get("action", "")).upper()

        if dialog_type == "RISK_DISCLOSURE":
            action = "CHECK_AND_WAIT_CONFIRM" if has_checkbox else "WAIT_AND_CONFIRM"
            if not button_coord:
                button_coord = (int(width * 0.5), int(height * 0.85))
            if has_checkbox and not checkbox_coord:
                checkbox_coord = (int(width * 0.2), int(height * 0.75))

        elif dialog_type == "PASSWORD_EXPIRY":
            action = "SKIP"
            if not button_coord:
                button_coord = (int(width * 0.65), int(height * 0.75))
            button_text = action_button_text or "稍后提醒"

        elif dialog_type == "REJECTION":
            action = "RAISE_ERROR"
            raise TradeError(f"弹窗提示交易失败: {message}")

        elif dialog_type == "CONFIRMATION":
            action = "CONFIRM"
            if not button_coord:
                button_coord = (int(width * 0.5), int(height * 0.8))

        else:
            if not action or action == "UNKNOWN":
                action = "UNRESOLVED"
            if strict:
                # 严禁在未匹配到弹窗控件时盲目静默返回 success
                raise TradeVerificationError(
                    f"未知的阻断弹窗，无法视觉仲裁闭环: {message or resp_text}",
                    result={"dialog_type": dialog_type, "message": message},
                )

        return DialogDecision(
            dialog_type=dialog_type,
            action=action,
            countdown_seconds=countdown_seconds,
            has_checkbox=has_checkbox,
            checkbox_coord=checkbox_coord,
            button_coord=button_coord,
            button_text=action_button_text,
            requires_human=(has_captcha or dialog_type == "CAPTCHA"),
            message=message,
            raw_response=resp_text,
        )


# ============================================================================
# R3: 委托终态双帧差分仲裁 (TradeReceiptDiffArbitrator)
# ============================================================================

class ArbitrationDecision:
    SUBMIT_CONFIRMED = "SUBMIT_CONFIRMED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class DualFrameReceiptResult:
    decision: str  # SUBMIT_CONFIRMED, SUBMIT_FAILED, AMBIGUOUS
    confidence: float
    form_cleared: bool
    funds_frozen: bool
    mini_flow_added: bool
    reasons: List[str] = field(default_factory=list)
    suggested_action: str = "PROCEED"  # PROCEED, RETRY, QUERY_TODAY_ENTRUSTS
    raw_response: str = ""


class TradeReceiptDiffArbitrator:
    """
    委托终态双帧差分仲裁器
    针对早盘高并发无回执阶段，对比下单前后操作区双帧局部图像，
    剥离右侧五档跳动行情的噪点，专注于左侧表单清空复位、微型流水新增与可用资金冻结，
    裁决订单是否已成功入队，防止策略重复提交买入指令导致穿仓。
    """

    def __init__(
        self,
        backend: Optional[IVLMBackend] = None,
        confidence_threshold: float = 0.85,
    ):
        self.backend = backend or MockVLMBackend()
        self.confidence_threshold = confidence_threshold

    def _isolate_operation_panel(
        self,
        img: Image.Image,
        split_ratio: float = 0.55,
    ) -> Image.Image:
        """剥离右侧五档跳动行情，仅截取或保留左侧交易操作区"""
        width, height = img.size
        ratio = max(0.1, min(0.95, float(split_ratio)))
        crop_width = int(width * ratio)
        return img.crop((0, 0, crop_width, height))

    def arbitrate(
        self,
        frame_before: Union[Image.Image, bytes, np.ndarray, str],
        frame_after: Union[Image.Image, bytes, np.ndarray, str],
        split_ratio: float = 0.55,
        confidence_threshold: Optional[float] = None,
        operation_panel_roi: Optional[Tuple[int, int, int, int]] = None,
        raise_on_ambiguous: bool = False,
    ) -> DualFrameReceiptResult:
        """
        执行双帧差分仲裁。
        剥离五档噪点后拼接双帧对比图，分析表单复位、资金扣减与流水新增。
        置信度低于阈值时强制裁决为 AMBIGUOUS，并引导降级查询 (QUERY_TODAY_ENTRUSTS)。
        """
        threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else self.confidence_threshold
        )

        pil_before = image_to_pil(frame_before)
        pil_after = image_to_pil(frame_after)

        # 剥离右侧五档跳动行情噪点
        if operation_panel_roi is not None:
            panel_before = pil_before.crop(operation_panel_roi)
            panel_after = pil_after.crop(operation_panel_roi)
        else:
            panel_before = self._isolate_operation_panel(pil_before, split_ratio)
            panel_after = self._isolate_operation_panel(pil_after, split_ratio)

        # 图像物理差分计算 (若尺寸不一致做保护对齐)
        if panel_before.size != panel_after.size:
            panel_after = panel_after.resize(panel_before.size)
        diff = ImageChops.difference(panel_before, panel_after)
        stat = ImageStat.Stat(diff)
        diff_mean = sum(stat.mean) / len(stat.mean) if stat.mean else 0.0

        # 构建发送给 VLM 的拼接双帧局部对比图
        composite = compose_dual_frames(panel_before, panel_after)
        dual_bytes = image_to_bytes(composite)
        prompt = (
            "你是一个量化交易执行审计专家。请对比同一操作区下单前后的双帧局部图像（左侧为下单前，右侧为下单后；已剥离右侧五档跳动行情噪点）。\n"
            "请评估：\n"
            "1. 表单输入区（代码、数量、价格）是否已清空复位 (form_cleared)？\n"
            "2. 可用资金是否发生扣减或冻结 (funds_frozen)？\n"
            "3. 微型委托流水是否新增一行记录 (mini_flow_added)？\n"
            "4. 是否有显式报错提示 (error_detected)？\n"
            "给出置信度 (0.0 - 1.0) 和终态裁决 (SUBMIT_CONFIRMED, SUBMIT_FAILED, AMBIGUOUS)。\n"
            "输出严格 JSON:\n"
            "{\n"
            '  "decision": "SUBMIT_CONFIRMED" | "SUBMIT_FAILED" | "AMBIGUOUS",\n'
            '  "confidence": 0.95,\n'
            '  "form_cleared": true,\n'
            '  "funds_frozen": true,\n'
            '  "mini_flow_added": true,\n'
            '  "error_detected": false,\n'
            '  "reasons": ["表单输入框已复位清空", "可用资金发生冻结扣减", "微型委托流水新增记录"],\n'
            '  "suggested_action": "PROCEED" | "RETRY" | "QUERY_TODAY_ENTRUSTS"\n'
            "}"
        )

        resp_text = self.backend.request(dual_bytes, prompt)
        parsed = extract_json_from_response(resp_text)

        decision = str(parsed.get("decision", "")).upper()
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
        form_cleared = bool(parsed.get("form_cleared", False))
        funds_frozen = bool(parsed.get("funds_frozen", False))
        mini_flow_added = bool(parsed.get("mini_flow_added", False))
        error_detected = bool(parsed.get("error_detected", False))
        reasons = parsed.get("reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]

        # 如果 VLM 没给出结构化决策，基于文本与物理差分兜底评估
        if not decision or decision not in {
            ArbitrationDecision.SUBMIT_CONFIRMED,
            ArbitrationDecision.SUBMIT_FAILED,
            ArbitrationDecision.AMBIGUOUS,
        }:
            has_negated_success = bool(
                re.search(
                    r"(?:未|没有|未见|非|并不|无)[\s]*(?:提交成功|已申报|已提交|已清空|成功|资金减少|流水增加)",
                    resp_text,
                )
            )
            # 文本启发式分析
            if any(w in resp_text for w in ["失败", "报错", "超限", "拒绝", "未提交成功"]):
                error_detected = True
                decision = ArbitrationDecision.SUBMIT_FAILED
                confidence = 0.90
                reasons.append("文本启发式判定：检测到失败或报错描述")
            elif (
                any(w in resp_text for w in ["提交成功", "已申报", "已提交", "已清空", "资金减少", "流水增加"])
                and not has_negated_success
            ):
                form_cleared = True
                funds_frozen = True
                decision = ArbitrationDecision.SUBMIT_CONFIRMED
                confidence = 0.90
                reasons.append("文本启发式判定：模型描述确认订单已提交并扣减资金")
            elif diff_mean < 0.2:
                # 没有任何像素变化，可能点击未响应或卡死
                decision = ArbitrationDecision.AMBIGUOUS
                confidence = 0.5
                reasons.append("双帧操作区无任何视觉变化，疑似点击未生效")
            elif form_cleared and (funds_frozen or mini_flow_added):
                decision = ArbitrationDecision.SUBMIT_CONFIRMED
                confidence = 0.92
                reasons.append("启发式判定：表单已清空且资金或流水已更新")
            elif error_detected:
                decision = ArbitrationDecision.SUBMIT_FAILED
                confidence = 0.90
                reasons.append("启发式判定：检测到表单显式报错")
            else:
                decision = ArbitrationDecision.AMBIGUOUS
                confidence = 0.70
                reasons.append("局部变化存在但不满足确定性提交特征")

        # 物理一致性与幻觉交叉校验（无论来自结构化 JSON 还是文本启发式推断，统一严格把关）：
        # 1. 显式报错时绝对禁止判定为提交确认
        if error_detected and decision == ArbitrationDecision.SUBMIT_CONFIRMED:
            decision = ArbitrationDecision.SUBMIT_FAILED
            reasons.append("物理交叉校验：检测到显式报错提示，覆盖确认结论")

        # 2. 图像无任何物理像素变化 (diff_mean < 0.2)，即使模型给出确认，也强制降级为 AMBIGUOUS
        if diff_mean < 0.2 and decision == ArbitrationDecision.SUBMIT_CONFIRMED:
            decision = ArbitrationDecision.AMBIGUOUS
            confidence = min(confidence, 0.5)
            if not any("无任何视觉变化" in r for r in reasons):
                reasons.append("物理交叉校验：双帧操作区无任何视觉变化，疑似点击未生效或界面卡死")

        # 3. 既无表单清空、又无资金扣减、又无流水新增，严禁返回 SUBMIT_CONFIRMED
        if decision == ArbitrationDecision.SUBMIT_CONFIRMED and not (
            form_cleared or funds_frozen or mini_flow_added
        ):
            decision = ArbitrationDecision.AMBIGUOUS
            confidence = min(confidence, 0.6)
            reasons.append("物理交叉校验：缺乏表单清空、资金冻结或流水新增等实质性依据，降级为模棱两可")

        # 核心安全准则：置信度不足时，强制降级为 AMBIGUOUS 并引导降级查询
        suggested_action = "PROCEED"
        if confidence < threshold or decision == ArbitrationDecision.AMBIGUOUS:
            decision = ArbitrationDecision.AMBIGUOUS
            suggested_action = "QUERY_TODAY_ENTRUSTS"
            if not any("低于安全阈值" in r for r in reasons):
                reasons.append(
                    f"置信度 ({confidence:.2f}) 低于安全阈值 ({threshold:.2f})，引导降级查询"
                )
        elif decision == ArbitrationDecision.SUBMIT_FAILED:
            suggested_action = "RETRY"
        elif decision == ArbitrationDecision.SUBMIT_CONFIRMED:
            suggested_action = "PROCEED"

        result = DualFrameReceiptResult(
            decision=decision,
            confidence=confidence,
            form_cleared=form_cleared,
            funds_frozen=funds_frozen,
            mini_flow_added=mini_flow_added,
            reasons=reasons,
            suggested_action=suggested_action,
            raw_response=resp_text,
        )

        if raise_on_ambiguous and decision == ArbitrationDecision.AMBIGUOUS:
            raise VisualArbitrationError(
                f"双帧视觉仲裁置信度不足或状态模棱两可: {'; '.join(reasons)}",
                result=result,
            )

        return result


# ============================================================================
# R4: 无句柄控件视觉 Grounding 与自适应点击 (VisualGroundingEngine)
# ============================================================================

@dataclass
class ControlGroundingResult:
    description: str
    found: bool
    bbox: Optional[Tuple[float, float, float, float]] = None  # [ymin, xmin, ymax, xmax]
    center: Optional[Tuple[int, int]] = None
    confidence: float = 0.0
    raw_response: str = ""


@dataclass
class RowAlignmentResult:
    nominal_y: int
    calibrated_y: int
    row_top: int
    row_bottom: int
    snapped: bool
    target_row: int
    safety_margin: int


class VisualGroundingEngine:
    """
    无句柄控件视觉 Grounding 与自适应点击引擎
    1. 支持根据语义描述（如“全撤”、“当日委托”）输出归一化 BBox 并计算点击中心；
    2. 对高分屏 DPI 缩放下硬编码累加导致的分割线点击落空问题，进行几何行高对齐校准与局部几何吸附。
    """

    def __init__(
        self,
        backend: Optional[IVLMBackend] = None,
        coord_format: Optional[str] = None,
    ):
        self.backend = backend or MockVLMBackend()
        self.coord_format = coord_format
        if self.coord_format is None:
            model_name = str(getattr(self.backend, "model", "")).lower()
            if "qwen" in model_name:
                self.coord_format = "1000"

    def ground_control(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        target_description: str,
        coord_format: Optional[str] = None,
    ) -> ControlGroundingResult:
        """
        根据语义描述在界面中定位无句柄控件
        """
        img_pil = image_to_pil(image)
        width, height = img_pil.size
        img_bytes = image_to_bytes(image)

        prompt = (
            f"请在截图中定位目标控件：“{target_description}”（如全撤按钮、撤单按钮、当日委托菜单项等）。\n"
            "返回目标归一化边界框 [ymin, xmin, ymax, xmax]（范围 0.0 到 1.0）及置信度。\n"
            "输出严格 JSON:\n"
            "{\n"
            '  "found": true,\n'
            f'  "description": "{target_description}",\n'
            '  "bbox": [ymin, xmin, ymax, xmax],\n'
            '  "confidence": 0.95\n'
            "}"
        )

        resp_text = self.backend.request(img_bytes, prompt)
        parsed = extract_json_from_response(resp_text)

        found = bool(parsed.get("found", False))
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
        fmt = coord_format or self.coord_format
        norm_bbox, center = normalize_bbox_and_center(
            parsed.get("bbox"), width, height, coord_format=fmt
        )
        if center is None:
            found = False

        return ControlGroundingResult(
            description=target_description,
            found=found,
            bbox=norm_bbox,
            center=center,
            confidence=confidence,
            raw_response=resp_text,
        )

    def calibrate_grid_row_click(
        self,
        grid_image: Union[Image.Image, bytes, np.ndarray, str],
        target_row: int,
        nominal_x: int,
        nominal_y: int,
        first_row_height: int = 30,
        row_height: int = 16,
        safety_margin: int = 3,
    ) -> RowAlignmentResult:
        """
        网格行定位几何吸附校准。
        针对硬编码行高累加导致的分割线点击落空与相邻行误触，通过局部几何分析进行边界吸附。
        若属于无边框自绘平铺网格，回退至几何行高边界估算与物理中心吸附。
        """
        if target_row < 0:
            raise ValueError(f"target_row 必须为非负整数，收到: {target_row}")

        img_pil = image_to_pil(grid_image)
        width, height = img_pil.size

        # 将图像转换为灰度矩阵，探测水平分割线
        gray_arr = np.array(img_pil.convert("L"))

        # 寻找水平分割线：计算每一行的平均灰度并探测分割线峰值
        dividers = []
        if height > 20:
            row_means = gray_arr.mean(axis=1)
            bg_mean = float(np.median(row_means))
            dev = np.abs(row_means - bg_mean)
            max_dev = float(np.max(dev))
            if max_dev > 10.0:
                dev_thresh = max(10.0, max_dev * 0.4)
                start_y = max(1, first_row_height - 5)
                end_y = height - 1
                for y_idx in range(start_y, end_y):
                    if dev[y_idx] >= dev_thresh:
                        prev_dev = dev[y_idx - 1]
                        next_dev = dev[y_idx + 1] if y_idx + 1 < len(dev) else 0.0
                        if dev[y_idx] >= prev_dev and dev[y_idx] >= next_dev:
                            if not dividers or (y_idx - dividers[-1]) >= 8:
                                dividers.append(y_idx)

        # 规范化分割线列表
        # 若探测到的第一条分割线显著高于表头底部（例如在第一行与第二行之间），补全表头底部分割线
        if dividers:
            if dividers[0] > first_row_height + int(row_height * 0.4):
                dividers.insert(0, first_row_height)
            elif abs(dividers[0] - first_row_height) <= 6:
                dividers[0] = dividers[0]

        # 确定目标行的物理上下边界 [row_top, row_bottom]
        if target_row + 1 < len(dividers):
            row_top = dividers[target_row]
            row_bottom = dividers[target_row + 1]
        elif target_row < len(dividers):
            row_top = dividers[target_row]
            spacing = (
                (dividers[-1] - dividers[0]) / max(1, len(dividers) - 1)
                if len(dividers) > 1
                else row_height
            )
            row_bottom = int(row_top + spacing)
        else:
            # 几何推算回退（针对无边框平铺网格或未能识别出足够分割线的情形）
            row_top = first_row_height + target_row * row_height
            row_bottom = row_top + row_height

        # 校验 nominal_y 与 row_top/row_bottom 的关联性，严禁双击至错误相邻行
        expected_top = first_row_height + target_row * row_height
        expected_center = first_row_height + int((target_row + 0.5) * row_height)
        row_center_y = int(row_top + (row_bottom - row_top) / 2.0)

        # 若探测到的行上下边界严重偏离理论中心或行高严重变形（偏差超过35%），说明分割线错位，必须回退至理论几何边界
        if (
            abs(row_center_y - expected_center) > int(row_height * 0.4)
            or abs(row_top - expected_top) > int(row_height * 0.4)
            or abs((row_bottom - row_top) - row_height) > int(row_height * 0.35)
        ):
            row_top = expected_top
            row_bottom = row_top + row_height
            row_center_y = int(row_top + (row_bottom - row_top) / 2.0)

        # 确保行边界在有效图片范围内 (边界钳位保护)
        row_top = max(0, min(row_top, height - 2))
        row_bottom = max(row_top + 4, min(row_bottom, height))
        row_center_y = int(row_top + (row_bottom - row_top) / 2.0)

        # 检验 nominal_y 是否处于危险边界区（距 top 或 bottom 小于 safety_margin）或落在外部
        snapped = False
        calibrated_y = nominal_y
        if (
            nominal_y < row_top + safety_margin
            or nominal_y > row_bottom - safety_margin
            or abs(nominal_y - row_top) <= 2
            or abs(nominal_y - row_bottom) <= 2
        ):
            # 执行吸附：修正至真实垂直几何中心
            calibrated_y = row_center_y
            snapped = True

        return RowAlignmentResult(
            nominal_y=nominal_y,
            calibrated_y=calibrated_y,
            row_top=row_top,
            row_bottom=row_bottom,
            snapped=snapped,
            target_row=target_row,
            safety_margin=safety_margin,
        )


# ============================================================================
# R5: 客户端存活与状态视觉看门狗 (ClientVisualLivenessWatchdog)
# ============================================================================

@dataclass
class LivenessReport:
    is_alive: bool
    state: str  # HEALTHY, BLACK_SCREEN, OFFLINE, MASK_LOCKED, MESSAGE_PUMP_HUNG
    issues: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    recovery_action: str = "NONE"  # NONE, RECONNECT, DISMISS_MASK, RESTART_PROCESS


class ClientVisualLivenessWatchdog:
    """
    客户端存活与状态视觉看门狗
    非侵入式界面视觉健康巡检：
    - DWM 远程桌面断开/锁屏黑屏 (BLACK_SCREEN)
    - 通讯指示灯红灯脱机 (OFFLINE)
    - 全屏遮罩锁死/模态卡死 (MASK_LOCKED)
    - Win32 消息泵死锁 (MESSAGE_PUMP_HUNG)
    """

    def __init__(self, backend: Optional[IVLMBackend] = None):
        self.backend = backend or MockVLMBackend()

    def inspect(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        hwnd: Optional[int] = None,
        trader: Optional[Any] = None,
    ) -> LivenessReport:
        """
        巡检客户端当前视觉状态与窗口存活性。
        """
        issues = []
        details = {}
        img_pil = image_to_pil(image)
        width, height = img_pil.size

        # 尝试从 trader 提取 hwnd
        if (hwnd is None or hwnd <= 0) and trader is not None:
            for attr in ("_main", "_app"):
                obj = getattr(trader, attr, None)
                if obj is not None:
                    try:
                        wrapper = obj.wrapper_object() if hasattr(obj, "wrapper_object") else obj
                        if hasattr(wrapper, "handle") and wrapper.handle and wrapper.handle > 0:
                            hwnd = wrapper.handle
                            break
                    except Exception:
                        pass
                    try:
                        if hasattr(obj, "top_window"):
                            top = obj.top_window()
                            wrapper = top.wrapper_object() if hasattr(top, "wrapper_object") else top
                            if hasattr(wrapper, "handle") and wrapper.handle and wrapper.handle > 0:
                                hwnd = wrapper.handle
                                break
                    except Exception:
                        pass

        # 1. 检查 Win32 消息泵是否死锁挂起 (安全兼容非 Windows 运行环境)
        if hwnd is not None and isinstance(hwnd, int) and hwnd > 0:
            try:
                if hasattr(ctypes, "windll"):
                    is_hung = ctypes.windll.user32.IsHungAppWindow(hwnd)
                    details["is_hung_app_window"] = bool(is_hung)
                    if is_hung:
                        issues.append("Win32 消息泵死锁挂起 (IsHungAppWindow)")
                        return LivenessReport(
                            is_alive=False,
                            state="MESSAGE_PUMP_HUNG",
                            issues=issues,
                            details=details,
                            recovery_action="RESTART_PROCESS",
                        )
            except Exception as e:
                details["hung_check_error"] = str(e)

        # 2. 检查黑屏 / 锁屏断开 (DWM 锁屏或远程连接断开时整幅图像灰度近 0)
        img_arr = np.array(img_pil)
        mean_brightness = float(np.mean(img_arr))
        std_brightness = float(np.std(img_arr))
        details["mean_brightness"] = mean_brightness
        details["std_brightness"] = std_brightness

        # 计算图像梯度与边缘方差，用于联合甄别：真实正常界面、半透明磨砂遮罩与完全单色空白
        gray_arr = (
            np.mean(img_arr, axis=2).astype(np.float32)
            if len(img_arr.shape) == 3
            else img_arr.astype(np.float32)
        )
        gy, gx = np.gradient(gray_arr)
        grad_mag = np.hypot(gx, gy)
        edge_variance = float(np.var(grad_mag))
        edge_mean = float(np.mean(grad_mag))
        details["edge_variance"] = edge_variance
        details["edge_mean"] = edge_mean

        if mean_brightness < 8.0 and std_brightness < 5.0:
            issues.append("远程桌面断开或锁屏黑屏 (均值与方差接近0)")
            return LivenessReport(
                is_alive=False,
                state="BLACK_SCREEN",
                issues=issues,
                details=details,
                recovery_action="RECONNECT",
            )

        # 2.1 检查单色纯白未渲染或无内容空白截屏 (如空持仓页白底、窗口尚未完成绘制)
        # 特征：标准差与边缘方差近零 (std < 0.5 且 edge_variance < 0.5)
        # 严禁将纯白/单色未渲染空表误报为全屏磨砂遮罩锁死 (MASK_LOCKED)
        if mean_brightness >= 200.0 and std_brightness < 0.5 and edge_variance < 0.5:
            issues.append("检测到单色纯白或未渲染完成界面 (std < 0.5, 无边缘特征)")
            return LivenessReport(
                is_alive=False,
                state="BLANK_SCREEN",
                issues=issues,
                details=details,
                recovery_action="RETRY_CAPTURE",
            )

        # 3. 检查通讯指示灯红灯脱机
        # 通讯指示灯位于底部状态栏左下角/右下角指示托盘，或右上角托盘区域
        # 严禁将状态栏中央的红色行情指数（如“上证 3350.21 +1.50%”）误判为脱机红灯
        sb_left = img_arr[int(height * 0.85) :, : int(width * 0.20)]
        sb_right = img_arr[int(height * 0.85) :, int(width * 0.80) :]
        tr_crop = img_arr[: int(height * 0.15), int(width * 0.80) :]

        def _detect_compact_red_blob(region, loc_name):
            if region.size == 0 or len(region.shape) < 3:
                return 0, loc_name
            r = region[:, :, 0].astype(np.float32)
            g = region[:, :, 1].astype(np.float32)
            b = region[:, :, 2].astype(np.float32)
            red_mask = (r > 180) & (g < 80) & (b < 80)
            red_count = int(np.sum(red_mask))
            if red_count < 12:
                return 0, loc_name
            y_indices, x_indices = np.where(red_mask)
            if len(y_indices) == 0:
                return 0, loc_name
            h_span = int(np.max(y_indices) - np.min(y_indices) + 1)
            w_span = int(np.max(x_indices) - np.min(x_indices) + 1)
            density = red_count / float(max(1, h_span * w_span))
            # 真实通讯指示灯为紧凑几何色块 (宽度与高度受限，像素密集度高)
            # 行情文本字符呈水平分散、细笔画特征 (w_span 宽且 density 低)
            if w_span <= 40 and h_span <= 40 and density >= 0.25:
                return red_count, loc_name
            return 0, loc_name

        c_left, loc_l = _detect_compact_red_blob(sb_left, "底部状态栏左侧")
        c_right, loc_r = _detect_compact_red_blob(sb_right, "底部状态栏右侧")
        c_tr, loc_tr = _detect_compact_red_blob(tr_crop, "右上角")

        best_count, best_loc = max(
            [(c_left, loc_l), (c_right, loc_r), (c_tr, loc_tr)],
            key=lambda x: x[0],
        )
        loc_key = "status_bar" if "底部状态栏" in best_loc else "top_right"
        details["red_indicator_pixels"] = best_count
        details["red_indicator_location"] = loc_key

        if best_count >= 15:
            loc_str = "底部状态栏" if loc_key == "status_bar" else "右上角"
            issues.append(f"通讯指示灯红灯脱机 (检测到{loc_str}红色脱机指示)")
            return LivenessReport(
                is_alive=False,
                state="OFFLINE",
                issues=issues,
                details=details,
                recovery_action="RECONNECT",
            )

        # 4. 检查全屏遮罩锁死 (半透明全屏蒙层 DirectUI 遮罩)
        # 遮罩特征：对比度急剧降低，整体变暗或磨砂高亮泛白，且全屏高频边缘几乎被完全抹平
        # 联合判定机制：
        # - 下界防线：std_brightness >= 0.5 (排除纯白空白)
        # - 上界防线：std_brightness < 12.0 (磨砂模糊后的残余灰度波动)
        # - 边缘防线：edge_variance < 5.0 (排除具有锐利文字或网格线的正常稀疏白底表格界面)
        is_dark_mask = 8.0 <= mean_brightness <= 50.0 and std_brightness < 18.0 and edge_variance < 8.0
        is_frosted_white_mask = (
            mean_brightness >= 215.0
            and 0.5 <= std_brightness < 12.0
            and edge_variance < 5.0
        )
        if is_dark_mask or is_frosted_white_mask:
            mask_type = "暗色蒙层" if is_dark_mask else "高亮磨砂蒙层"
            issues.append(f"界面全屏遮罩锁死 (全局{mask_type}低对比度卡死)")
            return LivenessReport(
                is_alive=False,
                state="MASK_LOCKED",
                issues=issues,
                details=details,
                recovery_action="DISMISS_MASK",
            )

        # 5. 状态正常
        return LivenessReport(
            is_alive=True,
            state="HEALTHY",
            issues=[],
            details=details,
            recovery_action="NONE",
        )

    def trigger_recovery(
        self,
        report: LivenessReport,
        trader: Optional[Any] = None,
        callback: Optional[Callable[[LivenessReport], Any]] = None,
    ) -> bool:
        """
        触发自愈机制
        """
        if callback:
            callback(report)
            return True

        if report.recovery_action == "NONE":
            return True

        logger.warning(
            "触发视觉看门狗自愈操作: action=%s, issues=%s",
            report.recovery_action,
            report.issues,
        )

        if report.recovery_action == "DISMISS_MASK":
            if trader and hasattr(trader, "close_pop_dialog"):
                trader.close_pop_dialog()
                return True
        elif report.recovery_action == "RECONNECT":
            if trader and hasattr(trader, "refresh"):
                trader.refresh()
                return True

        return False


# ============================================================================
# VLMVisualOracle: 统一门面 (Unified Facade)
# ============================================================================

class VLMVisualOracle:
    """
    基于 VLM（视觉多模态大模型）的交易 GUI 全流程视觉安全卫士体系
    """

    def __init__(
        self,
        backend: Optional[IVLMBackend] = None,
        enable_dual_frame: bool = False,
        coord_format: Optional[str] = None,
    ):
        self.backend = backend or MockVLMBackend()
        self.enable_dual_frame = enable_dual_frame
        self.coord_format = coord_format
        self.status_verifier = StatusBarToastVerifier(self.backend)
        self.dialog_arbitrator = ModalDialogVisualArbitrator(
            self.backend, coord_format=self.coord_format
        )
        self.receipt_arbitrator = TradeReceiptDiffArbitrator(self.backend)
        self.grounding_engine = VisualGroundingEngine(
            self.backend, coord_format=self.coord_format
        )
        self.liveness_watchdog = ClientVisualLivenessWatchdog(self.backend)

    # R1
    def verify_status_bar_and_toast(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        status_bar_roi: Optional[Tuple[int, int, int, int]] = None,
        toast_roi: Optional[Tuple[int, int, int, int]] = None,
        raise_on_reject: bool = True,
    ) -> StatusBarToastResult:
        """自绘状态栏与浮动 Toast 拒单捕获"""
        return self.status_verifier.verify(
            image=image,
            status_bar_roi=status_bar_roi,
            toast_roi=toast_roi,
            raise_on_reject=raise_on_reject,
        )

    # R2
    def arbitrate_modal_dialog(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        strict: bool = True,
        coord_format: Optional[str] = None,
    ) -> DialogDecision:
        """未知与多态阻断弹窗视觉智能仲裁"""
        return self.dialog_arbitrator.arbitrate(
            image, strict=strict, coord_format=coord_format or self.coord_format
        )

    # R3
    def arbitrate_trade_receipt(
        self,
        frame_before: Union[Image.Image, bytes, np.ndarray, str],
        frame_after: Union[Image.Image, bytes, np.ndarray, str],
        split_ratio: float = 0.55,
        confidence_threshold: Optional[float] = None,
        operation_panel_roi: Optional[Tuple[int, int, int, int]] = None,
        raise_on_ambiguous: bool = False,
    ) -> DualFrameReceiptResult:
        """委托终态双帧差分仲裁"""
        return self.receipt_arbitrator.arbitrate(
            frame_before=frame_before,
            frame_after=frame_after,
            split_ratio=split_ratio,
            confidence_threshold=confidence_threshold,
            operation_panel_roi=operation_panel_roi,
            raise_on_ambiguous=raise_on_ambiguous,
        )

    # R4
    def ground_control(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        target_description: str,
        coord_format: Optional[str] = None,
    ) -> ControlGroundingResult:
        """无句柄控件视觉 Grounding"""
        return self.grounding_engine.ground_control(
            image, target_description, coord_format=coord_format or self.coord_format
        )

    def calibrate_grid_row(
        self,
        grid_image: Union[Image.Image, bytes, np.ndarray, str],
        target_row: int,
        nominal_x: int,
        nominal_y: int,
        first_row_height: int = 30,
        row_height: int = 16,
        safety_margin: int = 3,
    ) -> RowAlignmentResult:
        """网格行定位几何吸附校准"""
        return self.grounding_engine.calibrate_grid_row_click(
            grid_image=grid_image,
            target_row=target_row,
            nominal_x=nominal_x,
            nominal_y=nominal_y,
            first_row_height=first_row_height,
            row_height=row_height,
            safety_margin=safety_margin,
        )

    # R5
    def inspect_liveness(
        self,
        image: Union[Image.Image, bytes, np.ndarray, str],
        hwnd: Optional[int] = None,
        trader: Optional[Any] = None,
    ) -> LivenessReport:
        """客户端存活与状态视觉看门狗巡检"""
        return self.liveness_watchdog.inspect(image, hwnd=hwnd, trader=trader)

    def trigger_recovery(
        self,
        report: LivenessReport,
        trader: Optional[Any] = None,
        callback: Optional[Callable[[LivenessReport], Any]] = None,
    ) -> bool:
        """自愈触发"""
        return self.liveness_watchdog.trigger_recovery(report, trader, callback)
