# -*- coding: utf-8 -*-
import abc
import ast
import base64
import inspect
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from io import StringIO
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Type, Union

import pandas as pd
import pywinauto.keyboard
import pywinauto
import pywinauto.clipboard

from easytrader.exceptions import CircuitBreakerOpenError, SchemaValidationError
from easytrader.log import logger
from easytrader.utils.captcha import captcha_recognize
from easytrader.utils.win_gui import SetForegroundWindow, ShowWindow, win32defines

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from easytrader import clienttrader


class PipelineContext:
    """
    单帧截图缓存与跨策略执行追踪上下文 (Shared across strategies in a chain)
    """

    def __init__(self, control_id: Optional[int] = None, metadata: Optional[Dict] = None):
        self.control_id = control_id
        self.metadata = metadata or {}
        self.screenshot = None
        self.traces: List[Dict] = []
        self._lock = threading.Lock()

    def get_screenshot(self, grid=None):
        """
        获取缓存的单帧截图；若尚未截图且传入了控件对象，则仅截取一次并缓存
        """
        with self._lock:
            if self.screenshot is not None:
                return self.screenshot
            if grid is not None:
                if hasattr(grid, "capture_as_image"):
                    self.screenshot = grid.capture_as_image()
                else:
                    self.screenshot = grid
            return self.screenshot

    def set_screenshot(self, img):
        """
        显式设置单帧截图
        """
        with self._lock:
            self.screenshot = img

    def log_trace(
        self,
        strategy_name: str,
        status: str,
        result_len: int = 0,
        error: Optional[str] = None,
        duration: Optional[float] = None,
        **kwargs,
    ) -> Dict:
        """
        记录策略执行链路详情
        """
        with self._lock:
            trace_entry = {
                "strategy": strategy_name,
                "status": status,
                "result_len": result_len,
                "error": str(error) if error else None,
                "duration": round(duration, 4) if duration is not None else None,
                "timestamp": time.time(),
                **kwargs,
            }
            self.traces.append(trace_entry)
            return trace_entry


class IGridStrategy(abc.ABC):
    @abc.abstractmethod
    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        """
        获取 grid 数据并格式化返回

        :param control_id: grid 的 control id
        :param context: 可选的单帧上下文
        :return: grid 数据
        """
        pass

    @abc.abstractmethod
    def set_trader(self, trader: "clienttrader.IClientTrader"):
        pass


class BaseStrategy(IGridStrategy):
    def __init__(self):
        self._trader = None

    def set_trader(self, trader: "clienttrader.IClientTrader"):
        self._trader = trader

    @abc.abstractmethod
    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        """
        :param control_id: grid 的 control id
        :param context: 可选的单帧上下文
        :return: grid 数据
        """
        pass

    def _get_grid(self, control_id: int):
        grid = self._trader.main.child_window(
            control_id=control_id, class_name="CVirtualGridCtrl"
        )
        return grid

    def _set_foreground(self, grid=None):
        try:
            if grid is None:
                grid = self._trader.main
            if grid.has_style(win32defines.WS_MINIMIZE):  # if minimized
                ShowWindow(grid.wrapper_object(), 9)  # restore window state
            else:
                SetForegroundWindow(grid.wrapper_object())  # bring to front
        except:
            pass

    @staticmethod
    def _filter_summary_rows(records: List[Dict]) -> List[Dict]:
        """
        过滤表格底部的「汇总/合计/总计/小计」统计行
        """
        if not records:
            return records
        cleaned = []
        summary_keywords = ("汇总", "合计", "总计", "小计")
        for row in records:
            if not isinstance(row, dict):
                continue
            row_text = "".join(str(v) for v in row.values() if v is not None)
            if any(kw in row_text for kw in summary_keywords):
                raw_code = row.get("证券代码")
                code = str(raw_code).strip() if raw_code is not None else ""
                raw_name = row.get("证券名称")
                name = str(raw_name).strip() if raw_name is not None else ""
                if any(kw in code for kw in summary_keywords) or \
                   any(kw in name for kw in summary_keywords) or \
                   not code or not code.isdigit():
                    logger.info("过滤表格统计汇总行: %s", row)
                    continue
            cleaned.append(row)
        return cleaned


class Copy(BaseStrategy):
    """
    通过复制 grid 内容到剪切板再读取来获取 grid 内容
    """

    _need_captcha_reg = True

    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        grid = self._get_grid(control_id)
        self._set_foreground(grid)
        grid.type_keys("^A^C", set_foreground=False, pause=0.2)
        content = self._get_clipboard_data()
        return self._format_grid_data(content)

    def _format_grid_data(self, data: str) -> List[Dict]:
        if not data or not data.strip():
            return []
        try:
            dtype = self._trader.config.GRID_DTYPE if self._trader and hasattr(self._trader, "config") else None
            df = pd.read_csv(
                io.StringIO(data),
                delimiter="\t",
                dtype=dtype,
                na_filter=False,
            )
        except pd.errors.EmptyDataError:
            return []
        except Exception as e:
            Copy._need_captcha_reg = True
            logger.warning("解析剪贴板表格数据异常: %s", e)
            raise
        return self._filter_summary_rows(df.to_dict("records"))

    def _get_clipboard_data(self) -> str:
        if Copy._need_captcha_reg:
            if (
                    self._trader.app.top_window().window(class_name="Static", title_re="验证码").exists(timeout=1)
            ):
                file_path = "tmp.png"
                count = 5
                found = False
                while count > 0:
                    self._trader.app.top_window().window(
                        control_id=0x965, class_name="Static"
                    ).capture_as_image().save(
                        file_path
                    )  # 保存验证码

                    captcha_num = captcha_recognize(file_path).strip()  # 识别验证码
                    captcha_num = "".join(captcha_num.split())
                    logger.info("captcha result-->" + captcha_num)
                    if len(captcha_num) == 4:
                        editor = self._trader.app.top_window().window(
                            control_id=0x964, class_name="Edit"
                        ) # 验证码输入框
                        editor.set_focus() # 焦点移到验证码输入框 (也可不聚焦防止键盘误触输入，不聚焦type_edit_control_keys也可正常输入)
                        self._trader.wait(0.1) # 输入前短暂等待
                        self._trader.type_edit_control_keys(
                            editor,
                            captcha_num
                        )  # 模拟输入验证码

                        self._trader.wait(0.1) # 输完后短暂等待
                        self._trader.app.top_window().type_keys("{ENTER}", pause=0.1)  # 模拟发送enter，点击确定
                        if not editor.exists(timeout=1):  # 窗体消失
                            logger.info("验证码验证成功-->" + captcha_num)
                            found = True
                            break
                    count -= 1
                    self._trader.wait(0.1)
                    self._trader.app.top_window().window(
                        control_id=0x965, class_name="Static"
                    ).click()
                if not found:
                    self._trader.app.top_window().Button2.click()  # 点击取消
            else:
                pass
                # 不要将 Copy._need_captcha_reg 置为 False, 因为它是类方法, 一旦置为 False, 后续操作都不再进行验证码识别
                # Copy._need_captcha_reg = False
        count = 5
        last_error = None
        while count > 0:
            try:
                return pywinauto.clipboard.GetData()
            # pylint: disable=broad-except
            except Exception as e:
                last_error = e
                count -= 1
                logger.exception("%s, retry ......", e)
        raise IOError(f"获取剪贴板数据失败: {last_error}") from last_error


class WMCopy(Copy):
    """
    通过复制 grid 内容到剪切板再读取来获取 grid 内容
    """

    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        grid = self._get_grid(control_id)
        grid.post_message(win32defines.WM_COMMAND, 0xE122, 0)
        self._trader.wait(0.1)
        content = self._get_clipboard_data()
        return self._format_grid_data(content)


class Xls(BaseStrategy):
    """
    通过将 Grid 另存为 xls 文件再读取的方式获取 grid 内容
    """

    def __init__(self, tmp_folder: Optional[str] = None):
        """
        :param tmp_folder: 用于保持临时文件的文件夹
        """
        super().__init__()
        self.tmp_folder = tmp_folder

    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        grid = self._get_grid(control_id)

        # ctrl+s 保存 grid 内容为 xls 文件
        self._set_foreground(grid)  # setFocus buggy, instead of SetForegroundWindow
        grid.type_keys("^s", set_foreground=False)
        count = 10
        while count > 0:
            if self._trader.is_exist_pop_dialog():
                break
            self._trader.wait(0.2)
            count -= 1

        temp_path = tempfile.mktemp(suffix=".xls", dir=self.tmp_folder)
        self._set_foreground(self._trader.app.top_window())

        # alt+s保存，alt+y替换已存在的文件
        self._trader.app.top_window().Edit1.set_edit_text(temp_path)
        self._trader.wait(0.1)
        self._trader.app.top_window().type_keys("%{s}%{y}", set_foreground=False)
        # Wait until file save complete otherwise pandas can not find file
        self._trader.wait(0.2)
        if self._trader.is_exist_pop_dialog():
            self._trader.app.top_window().Button2.click()
            self._trader.wait(0.2)

        try:
            return self._format_grid_data(temp_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def _format_grid_data(self, data: str) -> List[Dict]:
        with open(data, encoding="gbk", errors="replace") as f:
            content = f.read()

        if not content or not content.strip():
            return []

        try:
            dtype = self._trader.config.GRID_DTYPE if self._trader and hasattr(self._trader, "config") else None
            df = pd.read_csv(
                StringIO(content),
                delimiter="\t",
                dtype=dtype,
                na_filter=False,
            )
        except pd.errors.EmptyDataError:
            return []

        return self._filter_summary_rows(df.to_dict("records"))


class ScreenshotOCR(BaseStrategy):
    """
    通过后台截取 Grid 控件图像 + OCR 识别提取表格数据
    完全不操作系统剪贴板，彻底杜绝剪贴板并发竞争与柜台验证码
    """

    def __init__(self, ocr_engine=None):
        super().__init__()
        self._ocr_engine = ocr_engine

    def _get_ocr_engine(self):
        if self._ocr_engine is not None:
            return self._ocr_engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr_engine = RapidOCR()
            return self._ocr_engine
        except ImportError:
            raise ImportError(
                "使用 ScreenshotOCR 策略需要安装 rapidocr_onnxruntime 依赖。\n"
                "请运行: pip install rapidocr_onnxruntime"
            )

    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        grid = self._get_grid(control_id)
        # capture_as_image 直接在 Win32 句柄层面截屏，无需置顶或争抢剪贴板
        if context is not None:
            img = context.get_screenshot(grid)
        else:
            img = grid.capture_as_image()
        records = self._parse_image_records(img)
        return self._filter_summary_rows(records)

    def _parse_image_records(self, img) -> List[Dict]:
        """
        利用 OCR 结果根据空间几何坐标重组表格行列，
        基于表头物理列区间进行投影对齐，杜绝错位塌陷与数值粘连
        """
        engine = self._get_ocr_engine()
        import numpy as np

        img_np = np.array(img)
        result, _ = engine(img_np)
        if not result:
            return []

        items = []
        for box, text, score in result:
            text = str(text).strip()
            if not text:
                continue
            num_pts = float(len(box)) if box else 0.0
            if num_pts == 0.0:
                continue
            cy = sum(p[1] for p in box) / num_pts
            cx = sum(p[0] for p in box) / num_pts
            x_min = min(p[0] for p in box)
            x_max = max(p[0] for p in box)
            y_min = min(p[1] for p in box)
            y_max = max(p[1] for p in box)
            h = abs(y_max - y_min)
            w = abs(x_max - x_min)
            items.append({
                "text": text,
                "cx": cx,
                "cy": cy,
                "x_min": x_min,
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
                "h": h,
                "w": w,
                "box": box,
            })

        if not items:
            return []

        avg_h = sum(it["h"] for it in items) / len(items) if items else 16.0
        line_threshold = max(avg_h * 0.45, 6.0)

        items_sorted_y = sorted(items, key=lambda x: x["cy"])
        rows = []
        current_row = [items_sorted_y[0]]

        for it in items_sorted_y[1:]:
            row_y_min = min(x["y_min"] for x in current_row)
            row_y_max = max(x["y_max"] for x in current_row)
            row_cy = sum(x["cy"] for x in current_row) / len(current_row)
            row_h = max(row_y_max - row_y_min, 1.0)
            overlap = min(it["y_max"], row_y_max) - max(it["y_min"], row_y_min)
            min_h = min(it["h"], row_h)
            overlap_ratio = overlap / min_h if min_h > 0 else 0

            # 属于同行的条件：垂直投影重叠比例大于 35% 或 (垂直重叠 > 0 且中心点纵坐标偏差在行阈值内)
            if (overlap_ratio >= 0.35) or (abs(it["cy"] - row_cy) < line_threshold and overlap > 0):
                current_row.append(it)
            else:
                rows.append(sorted(current_row, key=lambda x: x["cx"]))
                current_row = [it]
        if current_row:
            rows.append(sorted(current_row, key=lambda x: x["cx"]))

        if len(rows) < 2:
            return []

        def _split_concatenated_floats(text):
            # 排除标准日期格式（如 2026.09.30），严禁误拆日期
            if re.match(r"^\d{4}[./-]\d{1,2}[./-]\d{1,2}$", text):
                return [text.strip()]

            dot_indices = [i for i, ch in enumerate(text) if ch == "."]
            if len(dot_indices) < 2:
                return [text.strip()]

            d0 = dot_indices[0]
            d1 = dot_indices[1]

            candidates = []
            expected_ratio = 1.0 / len(dot_indices)
            for p in range(d0 + 2, d1):
                s1 = text[:p].strip()
                s2 = text[p:].strip()
                try:
                    float(s1.replace(",", ""))
                except ValueError:
                    continue

                int1 = s1.replace(",", "").split(".")[0].strip().lstrip("-+")
                if int1.startswith("0") and len(int1) > 1:
                    continue

                int2 = text[p:d1].strip().lstrip("-+").replace(",", "")
                if not int2 or not int2.isdigit() or (int2.startswith("0") and len(int2) > 1):
                    continue

                dec1_len = len(s1.replace(",", "").split(".")[1])
                dec_penalty = 0
                if dec1_len not in (2, 3):
                    dec_penalty += 1

                if len(dot_indices) == 2:
                    try:
                        float(s2.replace(",", ""))
                        dec2_len = len(s2.replace(",", "").split(".")[1])
                        if dec2_len not in (2, 3):
                            dec_penalty += 1
                    except ValueError:
                        continue

                ratio_dist = abs(p / len(text) - expected_ratio)
                candidates.append((dec_penalty, ratio_dist, s1, s2))

            if candidates:
                candidates.sort(key=lambda c: (c[0], c[1]))
                best_s1 = candidates[0][2]
                best_s2 = candidates[0][3]
                rest = _split_concatenated_floats(best_s2)
                return [best_s1] + rest

            return [text.strip()]

        def _make_sub_items(item, parts):
            parts = [p.strip() for p in parts if p.strip()]
            total_len = sum(len(p) for p in parts)
            if total_len == 0 or len(parts) <= 1:
                return [item]
            sub_items = []
            curr_x = item["x_min"]
            total_w = item["x_max"] - item["x_min"]
            for p in parts:
                part_w = total_w * (len(p) / total_len)
                p_min = curr_x
                p_max = curr_x + part_w
                p_cx = (p_min + p_max) / 2.0
                curr_x = p_max
                sub_items.append({
                    "text": p,
                    "cx": p_cx,
                    "cy": item["cy"],
                    "x_min": p_min,
                    "x_max": p_max,
                    "y_min": item["y_min"],
                    "y_max": item["y_max"],
                    "h": item["h"],
                    "w": p_max - p_min,
                    "box": [[p_min, item["y_min"]], [p_max, item["y_min"]], [p_max, item["y_max"]], [p_min, item["y_max"]]],
                })
            return sub_items

        def _decompose_adhered_item(item):
            # 1. 优先按任意空白字符（空格、制表符等）切分多子段
            raw_text = item["text"]
            parts = [p.strip() for p in raw_text.split() if p.strip()]
            if len(parts) > 1:
                sub_items = _make_sub_items(item, parts)
            else:
                sub_items = [item]

            # 2. 级联检查各子元素是否存在多个浮点数数值粘连（如 13.15413.150 或三连浮点数）
            final_items = []
            for sub in sub_items:
                if sub["text"].count(".") >= 2:
                    f_parts = _split_concatenated_floats(sub["text"])
                    if len(f_parts) > 1:
                        final_items.extend(_make_sub_items(sub, f_parts))
                    else:
                        final_items.append(sub)
                else:
                    final_items.append(sub)

            return final_items

        # 表头物理列区间分析（级联分解表头粘连多列）
        expanded_header_items = []
        for it in rows[0]:
            expanded_header_items.extend(_decompose_adhered_item(it))
        header_items = sorted(expanded_header_items, key=lambda x: x["cx"])
        headers = [it["text"] for it in header_items]
        num_cols = len(headers)
        if num_cols == 0:
            return []

        # 计算各列物理分界线（相邻表头中心点中值）及表格左右边界保护
        boundaries = []
        for i in range(num_cols - 1):
            mid = (header_items[i]["cx"] + header_items[i + 1]["cx"]) / 2.0
            boundaries.append(mid)

        if num_cols > 1:
            left_bound = min(header_items[0]["cx"] - (header_items[1]["cx"] - header_items[0]["cx"]) / 2.0, header_items[0]["x_min"] - 10.0)
            right_bound = max(header_items[-1]["cx"] + (header_items[-1]["cx"] - header_items[-2]["cx"]) / 2.0, header_items[-1]["x_max"] + 10.0)
        else:
            left_bound = header_items[0]["x_min"] - 20.0
            right_bound = header_items[0]["x_max"] + 20.0

        def _get_col_index(cx):
            if cx < left_bound or cx > right_bound:
                return -1
            for k, b in enumerate(boundaries):
                if cx < b:
                    return k
            return len(boundaries)

        data_records = []
        for r in rows[1:]:
            expanded_cells = []
            for cell in r:
                expanded_cells.extend(_decompose_adhered_item(cell))

            # 几何物理投影：初始化全部表头列为空字符串，缺失单元格为空
            record = {h: "" for h in headers}
            col_assigned = {k: [] for k in range(num_cols)}

            for cell in expanded_cells:
                col_idx = _get_col_index(cell["cx"])
                if 0 <= col_idx < num_cols:
                    col_assigned[col_idx].append(cell)

            for col_idx, h in enumerate(headers):
                cells_in_col = col_assigned[col_idx]
                if cells_in_col:
                    cells_in_col.sort(key=lambda c: c["cx"])
                    record[h] = "".join(c["text"].strip() for c in cells_in_col).strip()

            if any(v != "" for v in record.values()):
                data_records.append(record)

        return data_records


class CircuitBreaker:
    """
    线程安全的状态机熔断器 (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
    """
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self._state = self.CLOSED
        self._failure_count = 0
        self._half_open_success_count = 0
        self._last_failure_time = 0.0
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            self._check_state_transition()
            return self._state

    def _check_state_transition(self):
        if self._state == self.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._state = self.HALF_OPEN
                self._half_open_success_count = 0
                logger.info("CircuitBreaker 状态自动转换: OPEN -> HALF_OPEN (进入探活模式)")

    def can_execute(self) -> bool:
        with self._lock:
            self._check_state_transition()
            return self._state in (self.CLOSED, self.HALF_OPEN)

    def record_success(self):
        with self._lock:
            self._check_state_transition()
            if self._state == self.HALF_OPEN:
                self._half_open_success_count += 1
                if self._half_open_success_count >= self.half_open_success_threshold:
                    self._state = self.CLOSED
                    self._failure_count = 0
                    self._half_open_success_count = 0
                    logger.info("CircuitBreaker 探活成功并恢复: HALF_OPEN -> CLOSED")
            elif self._state == self.CLOSED:
                self._failure_count = 0

    def record_failure(self):
        with self._lock:
            self._last_failure_time = time.time()
            if self._state == self.HALF_OPEN:
                self._state = self.OPEN
                self._half_open_success_count = 0
                logger.warning("CircuitBreaker 在 HALF_OPEN 探活失败，重新熔断: -> OPEN")
            elif self._state == self.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._state = self.OPEN
                    logger.warning(
                        "CircuitBreaker 连续失败达到阈值 (%d)，熔断开启: CLOSED -> OPEN",
                        self._failure_count,
                    )

    def reset(self):
        with self._lock:
            self._state = self.CLOSED
            self._failure_count = 0
            self._half_open_success_count = 0
            self._last_failure_time = 0.0


class ValidationResult:
    def __init__(
        self,
        is_valid: bool,
        error: Optional[str] = None,
        table_type: Optional[str] = None,
    ):
        self.is_valid = is_valid
        self.error = error
        self.table_type = table_type

    def __bool__(self):
        return self.is_valid

    def __repr__(self):
        return f"<ValidationResult is_valid={self.is_valid} error={self.error} table_type={self.table_type}>"


class AdaptiveSchemaValidator:
    """
    网格数据自适应多层契约验证器
    1. 空表假成功漏报防护：空表但静态控件 "股票市值" > 0 判定为假空表 (leak)
    2. 模式规则匹配：根据特征列自动识别 position / entrusts / trades 表格类型并校验必选列
    3. 字段格式检查：6位纯数字股票代码、浮点数解析、非负合理性校验
    4. 金融业务守恒律：可用余额 <= 股票余额、成交数量 <= 委托数量
    """

    NUMERIC_COLUMNS = {
        "股票余额", "可用余额", "可赎回数量", "冻结数量", "证券数量",
        "可用股份", "参考市值", "成本价", "市价", "参考市价", "浮动盈亏",
        "最新价", "委托数量", "委托价格", "成交数量", "成交价格", "成交金额",
        "撤单数量", "买入金额", "卖出金额", "发生金额", "手续费", "印花税",
        "当前持仓", "买入均价", "保本价",
    }

    PNL_COLUMNS = {"浮动盈亏", "盈亏比例", "参考盈亏", "累计盈亏", "浮动盈亏%"}

    def __init__(self, trader=None):
        self._trader = trader

    def detect_table_type(self, records: List[Dict]) -> str:
        if not records:
            return "unknown"
        all_keys = set().union(*(r.keys() for r in records if isinstance(r, dict)))
        if any(k in all_keys for k in ("成交编号", "成交时间")) or (
            "成交数量" in all_keys and "成交价格" in all_keys and "委托数量" not in all_keys
        ):
            return "trades"
        if any(k in all_keys for k in ("委托编号", "合同编号", "委托数量", "买卖标志", "操作")):
            return "entrusts"
        if any(k in all_keys for k in ("股票余额", "证券数量", "可用余额", "可用股份", "参考市值", "持仓数量", "股份余额")):
            return "position"
        return "unknown"

    def validate(
        self,
        records: Any,
        trader=None,
        table_type: Optional[str] = None,
        market_value: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> ValidationResult:
        res = self._do_validate(records, trader=trader, table_type=table_type, market_value=market_value)
        if not res.is_valid and raise_on_error:
            raise SchemaValidationError(res.error)
        return res

    def _do_validate(
        self,
        records: Any,
        trader=None,
        table_type: Optional[str] = None,
        market_value: Optional[Union[float, str]] = None,
    ) -> ValidationResult:
        if not isinstance(records, list):
            return ValidationResult(False, f"Expected list of dicts, got {type(records).__name__}")

        active_trader = trader or self._trader

        # 1. 空表假成功漏报防护 (Empty table vs false-empty check)
        if len(records) == 0:
            target_mv = market_value
            if target_mv is None and active_trader is not None:
                try:
                    if hasattr(active_trader, "_get_balance_from_statics"):
                        balance = active_trader._get_balance_from_statics()
                        target_mv = balance.get("股票市值")
                    elif hasattr(active_trader, "balance") and isinstance(active_trader.balance, dict):
                        target_mv = active_trader.balance.get("股票市值")
                except Exception as e:
                    logger.debug("Failed to query market value from trader statics: %s", e)

            # 若持股市值大于 0，且当前表格是持仓表（或未指定 table_type），则空表判定为假成功漏报
            if target_mv is not None:
                try:
                    target_mv_num = float(str(target_mv).replace(",", "").strip())
                    if target_mv_num > 0:
                        if table_type in (None, "position"):
                            return ValidationResult(
                                False,
                                f"False-empty table detected: records is empty but 股票市值 is {target_mv} > 0",
                                table_type="position",
                            )
                except (ValueError, TypeError):
                    pass
            return ValidationResult(True, table_type=table_type or "empty")

        # 2. 模式规则匹配 (Schema rule matching)
        all_keys = set().union(*(r.keys() for r in records if isinstance(r, dict)))
        detected_type = table_type or self.detect_table_type(records)

        # 检查是否包含股票代码字段
        code_keys = [k for k in ("证券代码", "代码", "stock_code") if k in all_keys]
        if not code_keys:
            return ValidationResult(
                False,
                f"Missing required security code column in table of type '{detected_type}'",
                table_type=detected_type,
            )

        if detected_type == "position":
            qty_keys = [k for k in ("股票余额", "证券数量", "持仓数量", "股份余额", "可用余额", "可用股份") if k in all_keys]
            if not qty_keys:
                return ValidationResult(
                    False,
                    "Position table missing quantity columns (e.g. 股票余额, 证券数量)",
                    table_type=detected_type,
                )
        elif detected_type == "entrusts":
            entrust_keys = [k for k in ("委托编号", "合同编号", "委托数量", "委托价格", "操作", "买卖标志") if k in all_keys]
            if not entrust_keys:
                return ValidationResult(
                    False,
                    "Entrusts table missing entrust columns (e.g. 委托编号, 委托数量)",
                    table_type=detected_type,
                )
        elif detected_type == "trades":
            trade_keys = [k for k in ("成交编号", "成交数量", "成交价格", "成交金额") if k in all_keys]
            if not trade_keys:
                return ValidationResult(
                    False,
                    "Trades table missing trade columns (e.g. 成交编号, 成交数量)",
                    table_type=detected_type,
                )

        PLACEHOLDER_VALUES = {"--", "-", "N/A", "n/a", "nan", "NaN", "None", "null", ""}

        # 3. 字段格式检查与合理性检查 (Field formats & Sanity checks)
        for idx, row in enumerate(records):
            if not isinstance(row, dict):
                return ValidationResult(False, f"Row {idx} is not a dict: {row}", table_type=detected_type)

            raw_code = None
            for ck in ("证券代码", "代码", "stock_code"):
                if ck in row and row[ck] is not None:
                    raw_code = row[ck]
                    break

            if raw_code is None:
                return ValidationResult(False, f"Row {idx} missing stock code: {row}", table_type=detected_type)
            code_str = str(raw_code).strip()
            # 6位或5位纯数字证券代码 (A股6位, 港股通5位)
            if not (code_str.isdigit() and len(code_str) in (5, 6)):
                return ValidationResult(
                    False,
                    f"Row {idx} contains invalid stock code '{code_str}' (must be 5 or 6 digits)",
                    table_type=detected_type,
                )

            # 浮点数字段格式与非负校验
            for col_name, val in row.items():
                if val is None or val == "":
                    continue
                if isinstance(val, float) and pd.isna(val):
                    continue
                val_str = str(val).replace(",", "").strip()
                if val_str in PLACEHOLDER_VALUES:
                    continue
                if col_name in self.NUMERIC_COLUMNS:
                    try:
                        num = float(val_str)
                    except (ValueError, TypeError):
                        return ValidationResult(
                            False,
                            f"Row {idx} column '{col_name}' has non-numeric value '{val}'",
                            table_type=detected_type,
                        )
                    if col_name not in self.PNL_COLUMNS and num < 0:
                        return ValidationResult(
                            False,
                            f"Row {idx} column '{col_name}' has negative value {num}",
                            table_type=detected_type,
                        )

            # 4. 金融业务守恒律 (Financial invariants)
            # 持仓: 可用余额 <= 股票余额
            total_key = next((k for k in ("股票余额", "证券数量", "持仓数量", "股份余额") if k in row), None)
            avail_key = next((k for k in ("可用余额", "可用股份", "可用数量") if k in row), None)
            if total_key and avail_key:
                t_val = row.get(total_key)
                a_val = row.get(avail_key)
                if t_val not in (None, "") and a_val not in (None, ""):
                    t_str = str(t_val).replace(",", "").strip()
                    a_str = str(a_val).replace(",", "").strip()
                    if t_str not in PLACEHOLDER_VALUES and a_str not in PLACEHOLDER_VALUES:
                        try:
                            t_num = float(t_str)
                            a_num = float(a_str)
                            if a_num > t_num + 1e-6:
                                return ValidationResult(
                                    False,
                                    f"Financial invariant violated in row {idx}: {avail_key} ({a_num}) > {total_key} ({t_num})",
                                    table_type=detected_type,
                                )
                        except (ValueError, TypeError):
                            pass

            # 委托: 成交数量 <= 委托数量, (成交数量 + 撤单数量 <= 委托数量)
            deal_key = "成交数量" if "成交数量" in row else None
            entrust_key = "委托数量" if "委托数量" in row else None
            if deal_key and entrust_key:
                d_val = row.get(deal_key)
                e_val = row.get(entrust_key)
                if d_val not in (None, "") and e_val not in (None, ""):
                    d_str = str(d_val).replace(",", "").strip()
                    e_str = str(e_val).replace(",", "").strip()
                    if d_str not in PLACEHOLDER_VALUES and e_str not in PLACEHOLDER_VALUES:
                        try:
                            d_num = float(d_str)
                            e_num = float(e_str)
                            if d_num > e_num + 1e-6:
                                return ValidationResult(
                                    False,
                                    f"Financial invariant violated in row {idx}: 成交数量 ({d_num}) > 委托数量 ({e_num})",
                                    table_type=detected_type,
                                )
                            # 如果同时存在撤单数量
                            c_val = row.get("撤单数量")
                            if c_val not in (None, ""):
                                c_str = str(c_val).replace(",", "").strip()
                                if c_str not in PLACEHOLDER_VALUES:
                                    c_num = float(c_str)
                                    if (d_num + c_num) > e_num + 1e-6:
                                        return ValidationResult(
                                            False,
                                            f"Financial invariant violated in row {idx}: 成交数量 ({d_num}) + 撤单数量 ({c_num}) > 委托数量 ({e_num})",
                                            table_type=detected_type,
                                        )
                        except (ValueError, TypeError):
                            pass

        return ValidationResult(True, table_type=detected_type)


class IVLMBackend(abc.ABC):
    @abc.abstractmethod
    def request(self, image_bytes: bytes, prompt: str) -> str:
        """
        发送图像和 prompt 给 VLM 模型，返回模型生成的文本内容
        :param image_bytes: 图像 PNG/JPEG 原始字节
        :param prompt: 提示词
        :return: 文本回复
        """
        pass


class OllamaHttpBackend(IVLMBackend):
    def __init__(
        self,
        model: str = "qwen2.5-vl:7b",
        host: str = "http://localhost:11434",
        timeout: float = 60.0,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def request(self, image_bytes: bytes, prompt: str) -> str:
        url = f"{self.host}/api/generate"
        b64_img = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64_img],
            "stream": False,
            "format": "json",
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
                    raise IOError(f"OpenAI compatible API returned error: {resp_json['error']}")
                choices = resp_json.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
                raise IOError(f"OpenAI compatible API returned empty choices: {resp_json}")
        except Exception as e:
            logger.error("OpenAICompatibleBackend request failed: %s", e)
            raise IOError(f"OpenAI compatible request error: {e}") from e


class MockVLMBackend(IVLMBackend):
    def __init__(self, response_text: str = "[]", side_effect=None):
        self.response_text = response_text
        self.side_effect = side_effect
        self.calls: List[Dict] = []

    def request(self, image_bytes: bytes, prompt: str) -> str:
        self.calls.append({"image_bytes": image_bytes, "prompt": prompt})
        if self.side_effect is not None:
            if isinstance(self.side_effect, Exception):
                raise self.side_effect
            if callable(self.side_effect):
                return self.side_effect(image_bytes, prompt)
        return self.response_text


DEFAULT_VLM_PROMPT = (
    "你是一个金融交易软件表格识别专家。请识别截图中显示的证券交易表格（如资金股票、当日委托、当日成交等）。"
    "请提取表格的所有数据行并转换为严格的 JSON 数组（Array of Objects），每个 Object 的键为列名表头，值为对应的单元格内容（字符串或数字）。"
    "注意：\n"
    "1. 只输出标准的 JSON 数组格式，不要包含任何 markdown 标记之外的解释性文字。\n"
    "2. 保持数值准确，如股票代码、股票余额、可用余额、参考市值等。\n"
    "3. 如果表格没有数据（空表），请返回空数组 []。\n"
    "4. 忽略底部汇总/合计行。"
)


class OllamaVLM(BaseStrategy):
    """
    通过 Vision-Language Model (VLM) 多模态视觉大模型直接端到端提取表格结构与内容
    作为 OCR 几何投影降级后的第二道终极防线
    """

    def __init__(
        self,
        backend: Optional[IVLMBackend] = None,
        model: str = "qwen2.5-vl:7b",
        host: str = "http://localhost:11434",
        prompt: Optional[str] = None,
    ):
        super().__init__()
        self.backend = backend or OllamaHttpBackend(model=model, host=host)
        self.prompt = prompt or DEFAULT_VLM_PROMPT

    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        grid = self._get_grid(control_id)
        if context is not None:
            img = context.get_screenshot(grid)
        else:
            img = grid.capture_as_image()

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        raw_output = self.backend.request(image_bytes, self.prompt)
        records = self._parse_json_output(raw_output)
        return self._filter_summary_rows(records)

    @classmethod
    def _parse_json_output(cls, raw_output: str) -> List[Dict]:
        if not raw_output or not raw_output.strip():
            return []
        text = raw_output.strip()

        # 1. 尝试剔除 Markdown 代码块标记 (```json ... ```)
        code_fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if code_fence_match:
            text = code_fence_match.group(1).strip()

        # 2. 清理末尾逗号并尝试反序列化
        candidate_text = re.sub(r",\s*([\]}])", r"\1", text)
        try:
            data = json.loads(candidate_text)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
            if isinstance(data, dict):
                for k in ("data", "records", "rows", "items", "table"):
                    if isinstance(data.get(k), list):
                        return [r for r in data[k] if isinstance(r, dict)]
        except Exception:
            pass

        # 3. 正则贪婪抓取 JSON 数组 ([ { ... } ])
        array_match = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", candidate_text)
        if array_match:
            try:
                data = json.loads(array_match.group(1))
                if isinstance(data, list):
                    return [r for r in data if isinstance(r, dict)]
            except Exception:
                pass

        # 4. 判断是否返回空数组字符串 []
        if re.search(r"\[\s*\]", text):
            return []

        # 5. 尝试 ast.literal_eval 处理单引号 Python 字面量
        try:
            val = ast.literal_eval(text)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
            if isinstance(val, dict):
                for k in ("data", "records", "rows", "items", "table"):
                    if isinstance(val.get(k), list):
                        return [r for r in val[k] if isinstance(r, dict)]
        except Exception:
            pass

        raise ValueError(f"Failed to parse VLM response as JSON table: {raw_output[:200]}")


class FallbackChain(BaseStrategy):
    """
    责任链组合网格策略 (Fallback Chain of Responsibility)
    支持：
    1. 多级降级阶梯 (默认: Copy -> ScreenshotOCR -> OllamaVLM)
    2. 线程安全熔断器 CircuitBreaker 快速失败与恢复
    3. 自适应契约验证 AdaptiveSchemaValidator 门禁拦截
    4. 单帧截图共享 PipelineContext 避免多策略重采样竞争
    5. 故障现场制品转储 _dump_artifact (PNG + Trace JSON)
    6. on_fallback 事件回调通知
    """

    def __init__(
        self,
        strategies: Optional[List[IGridStrategy]] = None,
        validator: Optional[AdaptiveSchemaValidator] = None,
        circuit_breaker: bool = True,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        on_fallback: Optional[Callable[[Dict], None]] = None,
        artifact_dir: Optional[str] = None,
        max_artifacts: int = 200,
    ):
        super().__init__()
        if strategies is None:
            strategies = [Copy(), ScreenshotOCR()]
        self.strategies = list(strategies)
        if not self.strategies:
            raise ValueError("FallbackChain strategies list cannot be empty")
        self.validator = validator or AdaptiveSchemaValidator()
        self.circuit_breaker_enabled = circuit_breaker
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.on_fallback = on_fallback
        self.artifact_dir = artifact_dir
        self.max_artifacts = max_artifacts

        self._circuit_breakers: Dict[IGridStrategy, CircuitBreaker] = {}
        if self.circuit_breaker_enabled:
            for s in self.strategies:
                self._circuit_breakers[s] = CircuitBreaker(
                    failure_threshold=self.failure_threshold,
                    recovery_timeout=self.recovery_timeout,
                )

    def get_circuit_breaker(self, strategy: IGridStrategy) -> CircuitBreaker:
        if strategy not in self._circuit_breakers:
            self._circuit_breakers[strategy] = CircuitBreaker(
                failure_threshold=self.failure_threshold,
                recovery_timeout=self.recovery_timeout,
            )
        return self._circuit_breakers[strategy]

    def set_trader(self, trader: "clienttrader.IClientTrader"):
        super().set_trader(trader)
        if hasattr(self.validator, "_trader"):
            self.validator._trader = trader
        for s in self.strategies:
            s.set_trader(trader)

    def get(self, control_id: int, context: Optional[PipelineContext] = None) -> List[Dict]:
        if context is None:
            context = PipelineContext(control_id=control_id)

        errors = []
        for idx, strategy in enumerate(self.strategies):
            strat_name = strategy.__class__.__name__

            # 熔断器检查
            if self.circuit_breaker_enabled:
                cb = self.get_circuit_breaker(strategy)
                if not cb.can_execute():
                    logger.warning("Strategy %s skipped because circuit breaker is OPEN", strat_name)
                    context.log_trace(
                        strat_name,
                        status="SKIPPED_CIRCUIT_OPEN",
                        error="Circuit breaker is OPEN",
                    )
                    continue

            # 尝试执行策略
            start_time = time.time()
            try:
                sig = inspect.signature(strategy.get)
                if "context" in sig.parameters:
                    raw_records = strategy.get(control_id, context=context)
                else:
                    raw_records = strategy.get(control_id)
            except Exception as e:
                duration = time.time() - start_time
                err_msg = f"{type(e).__name__}: {str(e)}"
                logger.warning("Strategy %s execution failed: %s", strat_name, err_msg)
                if self.circuit_breaker_enabled:
                    self.get_circuit_breaker(strategy).record_failure()
                context.log_trace(strat_name, status="EXECUTION_ERROR", error=err_msg, duration=duration)
                errors.append((strat_name, err_msg))
                continue

            duration = time.time() - start_time

            # 自适应契约验证
            val_result = self.validator.validate(raw_records, trader=self._trader)
            if not val_result.is_valid:
                err_msg = f"Validation failed: {val_result.error}"
                logger.warning("Strategy %s output validation failed: %s", strat_name, err_msg)
                if self.circuit_breaker_enabled:
                    self.get_circuit_breaker(strategy).record_failure()
                context.log_trace(strat_name, status="VALIDATION_FAILED", error=err_msg, duration=duration)
                errors.append((strat_name, err_msg))
                continue

            # 策略验证通过，执行成功
            if self.circuit_breaker_enabled:
                self.get_circuit_breaker(strategy).record_success()
            context.log_trace(
                strat_name,
                status="SUCCESS",
                result_len=len(raw_records),
                duration=duration,
            )

            # 如果不是第一梯队策略成功，说明触发了降级
            if idx > 0:
                logger.warning(
                    "FallbackChain succeeded using fallback strategy %s (tier %d)",
                    strat_name,
                    idx,
                )
                self._dump_artifact(context, status=f"fallback_to_{strat_name}")
                if self.on_fallback:
                    fallback_info = {
                        "from_strategy": self.strategies[0].__class__.__name__,
                        "to_strategy": strat_name,
                        "tier": idx,
                        "failed_strategies": [e[0] for e in errors],
                        "trace": context.traces,
                    }
                    try:
                        self.on_fallback(fallback_info)
                    except Exception as cb_err:
                        logger.error("Error in on_fallback callback: %s", cb_err)

            return raw_records

        # 所有策略均失败，全链路阻断
        self._dump_artifact(context, status="total_failure")
        if self.circuit_breaker_enabled and all(
            self.get_circuit_breaker(s).state == CircuitBreaker.OPEN for s in self.strategies
        ):
            raise CircuitBreakerOpenError(
                f"All strategies in FallbackChain have circuit breaker OPEN. Traces: {context.traces}"
            )
        raise IOError(f"FallbackChain exhausted all strategies without valid result. Errors: {errors}")

    def _cleanup_old_artifacts(self, target_dir: str, max_files: int = 200):
        try:
            files = [
                os.path.join(target_dir, f)
                for f in os.listdir(target_dir)
                if f.startswith("fallback_") and (f.endswith(".png") or f.endswith(".json"))
            ]
            if len(files) > max_files:
                files.sort(key=lambda p: os.path.getmtime(p))
                for f in files[: len(files) - max_files]:
                    try:
                        os.remove(f)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Failed to cleanup old artifacts: %s", e)

    def _dump_artifact(self, context: PipelineContext, status: str):
        try:
            target_dir = self.artifact_dir or os.path.expanduser("~/.easytrader/fallback_artifacts")
            os.makedirs(target_dir, exist_ok=True)
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = uuid.uuid4().hex[:6]
            file_base = f"fallback_{status}_{timestamp_str}_{unique_id}"

            # 尝试保存截图
            screenshot = context.screenshot
            if screenshot is None and self._trader and context.control_id:
                try:
                    grid = self._get_grid(context.control_id)
                    screenshot = context.get_screenshot(grid)
                except Exception:
                    pass

            if screenshot is not None:
                img_path = os.path.join(target_dir, f"{file_base}.png")
                try:
                    screenshot.save(img_path)
                except Exception as e:
                    logger.debug("Failed to save screenshot artifact: %s", e)

            # 保存 trace JSON
            json_path = os.path.join(target_dir, f"{file_base}.json")
            trace_data = {
                "timestamp": timestamp_str,
                "status": status,
                "control_id": context.control_id,
                "traces": context.traces,
                "metadata": context.metadata,
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(trace_data, f, ensure_ascii=False, indent=2)
            logger.info("Fallback artifact dumped to %s", target_dir)

            self._cleanup_old_artifacts(target_dir, max_files=self.max_artifacts)
        except Exception as e:
            logger.error("Failed to dump fallback artifact: %s", e)

