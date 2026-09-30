# -*- coding: utf-8 -*-
import abc
import io
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
        try:
            if not data or not data.strip():
                return []
            df = pd.read_csv(
                io.StringIO(data),
                delimiter="\t",
                dtype=self._trader.config.GRID_DTYPE,
                na_filter=False,
            )
            return self._filter_summary_rows(df.to_dict("records"))
        except Exception:
            Copy._need_captcha_reg = True
            return []

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
        while count > 0:
            try:
                return pywinauto.clipboard.GetData()
            # pylint: disable=broad-except
            except Exception as e:
                count -= 1
                logger.exception("%s, retry ......", e)


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

        return self._format_grid_data(temp_path)

    def _format_grid_data(self, data: str) -> List[Dict]:
        try:
            with open(data, encoding="gbk", errors="replace") as f:
                content = f.read()

            if not content or not content.strip():
                return []

            df = pd.read_csv(
                StringIO(content),
                delimiter="\t",
                dtype=self._trader.config.GRID_DTYPE,
                na_filter=False,
            )
            return self._filter_summary_rows(df.to_dict("records"))
        except Exception as e:
            logger.warning("解析表格文件失败: %s", e)
            return []


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
        利用 OCR 结果根据空间几何坐标重组表格行列
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
            cy = sum(p[1] for p in box) / 4.0
            cx = sum(p[0] for p in box) / 4.0
            h = abs(box[2][1] - box[0][1])
            items.append({"text": text, "cx": cx, "cy": cy, "h": h, "box": box})

        if not items:
            return []

        avg_h = sum(it["h"] for it in items) / len(items) if items else 16.0
        line_threshold = max(avg_h * 0.6, 8.0)

        items_sorted_y = sorted(items, key=lambda x: x["cy"])
        rows = []
        current_row = [items_sorted_y[0]]
        current_cy = items_sorted_y[0]["cy"]

        for it in items_sorted_y[1:]:
            if abs(it["cy"] - current_cy) < line_threshold:
                current_row.append(it)
            else:
                rows.append(sorted(current_row, key=lambda x: x["cx"]))
                current_row = [it]
                current_cy = it["cy"]
        if current_row:
            rows.append(sorted(current_row, key=lambda x: x["cx"]))

        if len(rows) < 2:
            return []

        headers = [it["text"] for it in rows[0]]
        data_records = []
        for r in rows[1:]:
            record = {}
            for col_idx, cell in enumerate(r):
                if col_idx < len(headers):
                    record[headers[col_idx]] = cell["text"]
                else:
                    record[f"col_{col_idx}"] = cell["text"]
            if record:
                data_records.append(record)

        return data_records
