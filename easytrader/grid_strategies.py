# -*- coding: utf-8 -*-
import abc
import io
import os
import re
import tempfile
from io import StringIO
from typing import TYPE_CHECKING, Dict, List, Optional

import pandas as pd
import pywinauto.keyboard
import pywinauto
import pywinauto.clipboard

from easytrader.log import logger
from easytrader.utils.captcha import captcha_recognize
from easytrader.utils.win_gui import SetForegroundWindow, ShowWindow, win32defines

if TYPE_CHECKING:
    # pylint: disable=unused-import
    from easytrader import clienttrader


class IGridStrategy(abc.ABC):
    @abc.abstractmethod
    def get(self, control_id: int) -> List[Dict]:
        """
        获取 grid 数据并格式化返回

        :param control_id: grid 的 control id
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
    def get(self, control_id: int) -> List[Dict]:
        """
        :param control_id: grid 的 control id
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

    def get(self, control_id: int) -> List[Dict]:
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

    def get(self, control_id: int) -> List[Dict]:
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

    def get(self, control_id: int) -> List[Dict]:
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

    def get(self, control_id: int) -> List[Dict]:
        grid = self._get_grid(control_id)
        # capture_as_image 直接在 Win32 句柄层面截屏，无需置顶或争抢剪贴板
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
