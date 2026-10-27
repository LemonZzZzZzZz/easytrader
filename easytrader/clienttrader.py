# -*- coding: utf-8 -*-
import abc
import datetime
import functools
import logging
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import inspect
import numpy as np
import hashlib, binascii

import easyutils
from pywinauto import findwindows, timings

from easytrader import grid_strategies, pop_dialog_handler, refresh_strategies
from easytrader.config import client
from easytrader.exceptions import (
    CircuitBreakerOpenError,
    HumanInterventionRequiredError,
    TradeError,
    TradeVerificationError,
)
from easytrader.grid_strategies import IGridStrategy
from easytrader.log import logger
from easytrader.refresh_strategies import IRefreshStrategy
from easytrader.utils.misc import file2dict
from easytrader.utils.perf import perf_clock
from easytrader.utils.validation import (
    validate_market_trade_params,
    validate_trade_params,
)
from easytrader.utils.win_gui import get_window_dpi_scale

if not sys.platform.startswith("darwin"):
    import pywinauto
    import pywinauto.clipboard

# 弹窗「同标题无进展」提前熔断阈值（env 可覆盖，越界收敛到 [1, max_attempts]）
MAX_STAGNANT_DIALOG_ROUNDS_ENV = "EASYTRADER_MAX_STAGNANT_DIALOG_ROUNDS"
DEFAULT_MAX_STAGNANT_DIALOG_ROUNDS = 4


def _resolve_max_stagnant(max_attempts):
    """解析「同标题连续无进展」提前熔断阈值。

    2026-10-27 真机事故：未知内容弹窗使 _handle_pop_dialogs 空转满 10 轮，
    耗时 18.3s（正常委托基线 6.4s）后委托被丢弃。阈值收紧可显著缩短空转，
    但过小可能误伤响应偏慢的客户端，故保留 env 覆盖与上下界收敛。
    """
    raw = os.environ.get(MAX_STAGNANT_DIALOG_ROUNDS_ENV, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = DEFAULT_MAX_STAGNANT_DIALOG_ROUNDS
    return max(1, min(value, int(max_attempts)))


class IClientTrader(abc.ABC):
    @property
    @abc.abstractmethod
    def app(self):
        """Return current app instance"""
        pass

    @property
    @abc.abstractmethod
    def main(self):
        """Return current main window instance"""
        pass

    @property
    @abc.abstractmethod
    def config(self):
        """Return current config instance"""
        pass

    @abc.abstractmethod
    def wait(self, seconds: float):
        """Wait for operation return"""
        pass

    @abc.abstractmethod
    def refresh(self):
        """Refresh data"""
        pass

    @abc.abstractmethod
    def is_exist_pop_dialog(self):
        pass


class _GridStrategyDescriptor(property):
    def __init__(self):
        super().__init__(self._fget, self._fset)

    def _fget(self, instance):
        if instance is None:
            return grid_strategies.Copy
        if getattr(instance, "_grid_strategy", None) is not None:
            return instance._grid_strategy
        return getattr(instance, "_default_grid_strategy", grid_strategies.Copy)

    def _fset(self, instance, value):
        instance._grid_strategy = value
        instance._grid_strategy_instance = None
        if isinstance(value, IGridStrategy):
            instance._grid_strategy_instance = value
            instance._grid_strategy_instance.set_trader(instance)

    def __get__(self, instance, owner=None):
        if instance is None:
            return getattr(owner, "_default_grid_strategy", grid_strategies.Copy)
        return self._fget(instance)


class ClientTrader(IClientTrader):
    _editor_need_type_keys = True
    _default_grid_strategy: Union[IGridStrategy, Type[IGridStrategy]] = grid_strategies.Copy
    # The strategy to use for getting grid data
    grid_strategy = _GridStrategyDescriptor()
    _grid_strategy_instance: Optional[IGridStrategy] = None
    refresh_strategy: IRefreshStrategy = refresh_strategies.Switch()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "grid_strategy" in cls.__dict__ and not isinstance(cls.__dict__["grid_strategy"], property):
            cls._default_grid_strategy = cls.__dict__["grid_strategy"]
            delattr(cls, "grid_strategy")

    def enable_type_keys_for_editor(self):
        """
        有些客户端无法通过 set_edit_text 方法输入内容，可以通过使用 type_keys 方法绕过
        """
        self._editor_need_type_keys = True

    @property
    def grid_strategy_instance(self):
        if self._grid_strategy_instance is None:
            strat = self.grid_strategy
            self._grid_strategy_instance = (
                strat
                if isinstance(strat, IGridStrategy)
                else strat()
            )
            self._grid_strategy_instance.set_trader(self)
        return self._grid_strategy_instance

    def enable_vlm_fallback(
        self,
        model: str = "qwen2.5-vl:7b",
        host: str = "http://localhost:11434",
        primary_strategy=None,
        circuit_breaker: bool = True,
        on_fallback: Optional[Callable[[Dict], None]] = None,
        timeout: float = 300.0,
        think: bool = False,
        options: Optional[Dict] = None,
        **kwargs,
    ):
        """
        启用多级视觉降级通道 (Primary -> ScreenshotOCR -> OllamaVLM)

        :param model: Ollama 模型名称，默认 "qwen2.5-vl:7b"
        :param host: Ollama HTTP 服务地址，默认 "http://localhost:11434"
        :param primary_strategy: 首选网格策略，若为 None 则使用当前 grid_strategy_instance
        :param circuit_breaker: 是否启用熔断器，默认 True
        :param on_fallback: 触发降级时的回调函数 callback(fallback_info)
        :param timeout: VLM 请求超时时间 (秒)，默认 300.0s (适应大模型冷加载)
        :param think: 是否启用大模型思考链，默认 False (针对表格提取任务关闭以防截断并提速)
        :param options: Ollama 选项参数 (如 {"num_predict": 2048, "temperature": 0})
        :param kwargs: 传递给 FallbackChain 的其他可选参数 (如 failure_threshold, recovery_timeout, artifact_dir, validator 等)
        :return: FallbackChain 实例
        """
        if primary_strategy is None:
            curr = self.grid_strategy_instance
            if isinstance(curr, grid_strategies.FallbackChain) and curr.strategies:
                primary = curr.strategies[0]
            else:
                primary = curr
        elif isinstance(primary_strategy, type):
            primary = primary_strategy()
        else:
            primary = primary_strategy

        ocr_strat = grid_strategies.ScreenshotOCR()
        vlm_strat = grid_strategies.OllamaVLM(
            model=model, host=host, timeout=timeout, think=think, options=options
        )

        # 构建策略阶梯: [primary, ScreenshotOCR, OllamaVLM]
        strategies = [primary]
        if not any(isinstance(s, grid_strategies.ScreenshotOCR) for s in strategies):
            strategies.append(ocr_strat)
        if not any(isinstance(s, grid_strategies.OllamaVLM) for s in strategies):
            strategies.append(vlm_strat)

        chain = grid_strategies.FallbackChain(
            strategies=strategies,
            circuit_breaker=circuit_breaker,
            on_fallback=on_fallback,
            **kwargs,
        )
        self.grid_strategy = chain
        return chain

    def __init__(self):
        self._config = client.create(self.broker_type)
        self._app = None
        self._main = None
        self._toolbar = None
        self._grid_strategy = None
        self._grid_strategy_instance = None

    @property
    def app(self):
        return self._app

    @property
    def main(self):
        return self._main

    @property
    def config(self):
        return self._config

    def connect(self, exe_path=None, **kwargs):
        """
        直接连接登陆后的客户端
        :param exe_path: 客户端路径类似 r'C:\\htzqzyb2\\xiadan.exe', 默认 r'C:\\htzqzyb2\\xiadan.exe'
        :return:
        """
        connect_path = exe_path or self._config.DEFAULT_EXE_PATH
        if connect_path is None:
            raise ValueError(
                "参数 exe_path 未设置，请设置客户端对应的 exe 地址,类似 C:\\客户端安装目录\\xiadan.exe"
            )

        self._app = pywinauto.Application().connect(path=connect_path, timeout=10)
        self._close_prompt_windows()
        # 优先通过配置的 TITLE 或默认标题匹配主窗口，若未匹配到再回退至 top_window
        title_target = getattr(self._config, "TITLE", "网上股票交易系统5.0")
        try:
            main_window = self._app.window(title_re=f".*{title_target}.*")
            if main_window.exists(timeout=1):
                self._main = main_window
            else:
                self._main = self._app.top_window()
        except Exception:
            self._main = self._app.top_window()
        try:
            from easytrader.utils.win_gui import ShowWindow, win32defines
            hwnd = None
            if hasattr(self._main, "handle") and isinstance(self._main.handle, int):
                hwnd = self._main.handle
            elif hasattr(self._main, "wrapper_object"):
                w = self._main.wrapper_object()
                if hasattr(w, "handle") and isinstance(w.handle, int):
                    hwnd = w.handle
            if hwnd and isinstance(hwnd, int):
                ShowWindow(hwnd, win32defines.SW_MAXIMIZE)
        except Exception as ex:
            logger.debug("Maximize main window failed: %s", ex)
        self._init_toolbar()

    @property
    def broker_type(self):
        return "ths"

    @property
    def balance(self):
        self._switch_left_menus(["查询[F4]", "资金股票"])

        return self._get_balance_from_statics()

    def _init_toolbar(self):
        self._toolbar = self._main.child_window(class_name="ToolbarWindow32")

    def _get_balance_from_statics(self):
        result = {}
        for key, control_id in self._config.BALANCE_CONTROL_ID_GROUP.items():
            try:
                val_text = (
                    self._main.child_window(
                        control_id=control_id, class_name="Static"
                    )
                    .window_text()
                    .strip()
                    .replace(",", "")
                )
                result[key] = float(val_text)
            except Exception as e:
                logger.warning(
                    "获取资金静态控件 %s (ID: %s) 失败: %s", key, control_id, e
                )
        return result

    @property
    def position(self):
        self._switch_left_menus(["查询[F4]", "资金股票"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID, table_type="position")

    @property
    def today_entrusts(self):
        self._switch_left_menus(["查询[F4]", "当日委托"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID, table_type="entrusts")

    @property
    def today_trades(self):
        self._switch_left_menus(["查询[F4]", "当日成交"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID, table_type="trades")

    @property
    def cancel_entrusts(self):
        self.refresh()
        self._switch_left_menus(["撤单[F3]"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID, table_type="cancellable")

    def _locate_entrust_visually(self, entrust_no, stock_code=None, amount=None):
        """
        通过后台截屏与 OCR 视觉定位目标委托在 Grid 控件中的物理坐标 (x, y)
        用于当结构化网格导出失败（如遇到剪贴板验证码、文件锁死）时的平滑视觉降级
        """
        try:
            grid_ctrl = self._app.top_window().child_window(
                control_id=self._config.COMMON_GRID_CONTROL_ID,
                class_name="CVirtualGridCtrl",
            )
            wrapper = grid_ctrl.wrapper_object() if hasattr(grid_ctrl, "wrapper_object") else grid_ctrl
            if not hasattr(wrapper, "capture_as_image"):
                return None
            grid_img = wrapper.capture_as_image()
        except Exception as e:
            logger.debug("Visual grid capture failed: %s", e)
            return None

        # 获取 OCR 引擎 (优先复用已有的 ScreenshotOCR 引擎)
        ocr_engine = None
        if hasattr(self, "grid_strategy_instance") and hasattr(self.grid_strategy_instance, "_get_ocr_engine"):
            try:
                ocr_engine = self.grid_strategy_instance._get_ocr_engine()
            except Exception:
                pass
        if ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                ocr_engine = RapidOCR()
            except Exception:
                return None

        try:
            img_np = np.array(grid_img)
            ocr_result, _ = ocr_engine(img_np)
        except Exception as e:
            logger.debug("Visual OCR run failed: %s", e)
            return None

        if not ocr_result:
            return None

        clean_entrust = str(entrust_no).strip()
        clean_code = str(stock_code)[-6:].strip() if stock_code else None
        clean_amount = str(int(amount)).strip() if amount is not None else None

        # 1. 优先根据合同编号精确/子串匹配
        for box, text, score in ocr_result:
            t = str(text).strip()
            if clean_entrust in t or (len(clean_entrust) >= 6 and clean_entrust[-6:] in t):
                cx = int(np.mean([pt[0] for pt in box]))
                cy = int(np.mean([pt[1] for pt in box]))
                return (cx, cy)

        # 2. 若合同号被折叠遮挡，按证券代码 + 数量进行行对齐匹配
        if clean_code:
            code_candidates = []
            for box, text, score in ocr_result:
                t = str(text).strip()
                if clean_code in t:
                    cy = int(np.mean([pt[1] for pt in box]))
                    code_candidates.append(cy)

            if len(code_candidates) == 1:
                match_cy = code_candidates[0]
                scale = self.get_dpi_scale_factor()
                target_x = int(self._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN * scale)
                return (target_x, match_cy)
            elif len(code_candidates) > 1 and clean_amount:
                for cand_cy in code_candidates:
                    for box, text, score in ocr_result:
                        if abs(np.mean([pt[1] for pt in box]) - cand_cy) <= 12:
                            if clean_amount == str(text).strip():
                                scale = self.get_dpi_scale_factor()
                                target_x = int(self._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN * scale)
                                return (target_x, cand_cy)

        # 3. 单单吸附启发式：若排除表头后仅存在 1 行数据
        data_ys = []
        for box, text, score in ocr_result:
            cy = int(np.mean([pt[1] for pt in box]))
            if cy > 35:
                data_ys.append(cy)
        if data_ys:
            min_y, max_y = min(data_ys), max(data_ys)
            if max_y - min_y < 20:
                single_cy = int(np.mean(data_ys))
                scale = self.get_dpi_scale_factor()
                target_x = int(self._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN * scale)
                return (target_x, single_cy)

        return None

    def _handle_cancel_pop_dialog(self, max_retries=2):
        """专门处理撤单确认与拒单弹窗，优先提取文本再点击确认/关闭"""
        if not self.is_exist_pop_dialog():
            return {"message": "success", "is_rejected": False}

        dialog_text = ""
        w = self._app.top_window()
        try:
            for static in w.children(class_name="Static"):
                t = static.window_text().strip()
                if t and t not in ("确定", "取消", "是", "否", "&Y", "&N"):
                    dialog_text += t + " "
        except Exception:
            try:
                dialog_text = w.window_text()
            except Exception:
                pass

        error_keywords = ("失败", "不能", "无法", "不可", "错误", "已成交", "已成", "废单", "已撤", "不存在", "异常", "拒绝")
        is_rejected = any(kw in dialog_text for kw in error_keywords)

        retry = 0
        while retry <= max_retries and self.is_exist_pop_dialog():
            w = self._app.top_window()
            clicked = False
            for btn_title in ["是(Y)", "确定", "是(&Y)", "是", "确认"]:
                try:
                    btn = w[btn_title]
                    if btn.exists():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                try:
                    w.type_keys("%Y", set_foreground=False)
                except Exception:
                    pass
            self.wait(0.2)
            if not self.is_exist_pop_dialog():
                break
            retry += 1

        return {"message": dialog_text.strip() if dialog_text else "success", "is_rejected": is_rejected}

    @perf_clock
    def cancel_entrust(
        self,
        entrust_no,
        stock_code=None,
        order_type=None,
        amount=None,
        max_retries=2,
        verify_timeout=2.0,
    ):
        if entrust_no is None or not str(entrust_no).strip():
            raise ValueError("entrust_no 不能为空")

        self.refresh()

        # 买单在撤单前记录可用金额，用于买单撤单解冻核验
        pre_avail = None
        if order_type and str(order_type).lower().startswith("b"):
            try:
                bal = self.balance
                if isinstance(bal, dict) and "可用金额" in bal:
                    pre_avail = float(str(bal["可用金额"]).replace(",", ""))
            except Exception as e:
                logger.debug("Record pre_avail balance failed: %s", e)

        # 1. 三级阶梯定位目标委托单
        target_idx = None
        target_coords = None
        grid_fetch_error = None
        curr_entrusts = None

        # Tier 1: 结构化网格数据提取
        try:
            curr_entrusts = self.cancel_entrusts
        except Exception as e:
            grid_fetch_error = e
            logger.warning("获取待撤委托网格异常: %s", e)

        if curr_entrusts is not None and isinstance(curr_entrusts, list):
            entrust_field = self._config.CANCEL_ENTRUST_ENTRUST_FIELD
            for i, entrust in enumerate(curr_entrusts):
                if not isinstance(entrust, dict):
                    continue
                for f_name in (entrust_field, "委托编号", "申请编号", "合同编号"):
                    val = str(entrust.get(f_name, "")).strip()
                    if val and val == str(entrust_no).strip():
                        target_idx = i
                        break
                if target_idx is not None:
                    break

            # 如果未通过合同号匹配到，且传入了 stock_code，尝试唯一匹配
            if target_idx is None and stock_code:
                code_matches = []
                for i, entrust in enumerate(curr_entrusts):
                    if not isinstance(entrust, dict):
                        continue
                    code_val = str(entrust.get("证券代码", "")).strip()
                    if code_val and code_val.endswith(str(stock_code)[-6:]):
                        if amount is not None:
                            amt_val = str(entrust.get("委托数量", "")).strip()
                            if amt_val == str(int(amount)):
                                code_matches.append(i)
                        else:
                            code_matches.append(i)
                if len(code_matches) == 1:
                    target_idx = code_matches[0]

        # Tier 2: 若网格失败或未匹配，平滑降级至视觉 OCR 定位
        if target_idx is None:
            target_coords = self._locate_entrust_visually(
                entrust_no, stock_code=stock_code, amount=amount
            )

        # Tier 3: 门禁与 Fail-Loud
        if target_idx is None and target_coords is None:
            if curr_entrusts is not None and len(curr_entrusts) == 0:
                return {
                    "message": "委托单状态错误不能撤单, 该委托单可能已经成交或者已撤",
                    "reason": "empty_grid",
                }
            if curr_entrusts is not None and len(curr_entrusts) > 0:
                return {
                    "message": "委托单状态错误不能撤单, 该委托单可能已经成交或者已撤",
                    "reason": "not_in_grid",
                }
            raise TradeVerificationError(
                f"获取待撤列表异常且视觉定位失败，无法执行撤单: {grid_fetch_error}",
                result={"message": "grid_error", "entrust_no": str(entrust_no), "verified": False},
            )

        # 执行双击撤单
        if target_coords is not None:
            grid_ctrl = self._app.top_window().child_window(
                control_id=self._config.COMMON_GRID_CONTROL_ID,
                class_name="CVirtualGridCtrl",
            )
            grid_ctrl.double_click(coords=target_coords)
        else:
            self._cancel_entrust_by_double_click(target_idx)

        # 处理弹窗并捕获拒单报错
        pop_res = self._handle_cancel_pop_dialog(max_retries=max_retries)
        if not pop_res.get("is_rejected"):
            try:
                legacy_pop = self._handle_pop_dialogs()
                if isinstance(legacy_pop, dict):
                    legacy_msg = str(legacy_pop.get("message", "")).strip()
                    error_keywords = ("失败", "不能", "无法", "不可", "错误", "已成交", "已成", "废单", "已撤", "不存在", "异常", "拒绝")
                    if any(kw in legacy_msg for kw in error_keywords):
                        pop_res = {"message": legacy_msg, "is_rejected": True}
                    elif any(k in legacy_pop for k in ("pop_key", "result")):
                        pop_res.update(legacy_pop)
            except Exception:
                pass

        if pop_res.get("is_rejected"):
            raise TradeVerificationError(
                f"委托单 {entrust_no} 撤单状态异常: {pop_res['message']}",
                result={
                    "message": "rejected",
                    "entrust_no": str(entrust_no),
                    "verified": False,
                    "pop_result": pop_res,
                },
            )

        # 异步状态二次核对
        if verify_timeout > 0:
            start_t = time.time()
            last_grid_error = None
            while time.time() - start_t < verify_timeout:
                self.refresh()
                curr_entrusts = None
                try:
                    curr_entrusts = self.cancel_entrusts
                except Exception as e:
                    last_grid_error = e
                    logger.warning("二次核对获取待撤列表异常: %s", e)

                if curr_entrusts is not None and isinstance(curr_entrusts, list):
                    # 1. 单笔挂单撤销清空：当前待撤列表变为空列表
                    if len(curr_entrusts) == 0:
                        return {
                            "message": "success",
                            "entrust_no": str(entrust_no),
                            "verified": True,
                            "detail": "cleared",
                        }

                    # 2. 正常核验：从待撤列表中已移除
                    entrust_field = self._config.CANCEL_ENTRUST_ENTRUST_FIELD
                    remaining = [
                        str(e.get(entrust_field, "")).strip()
                        for e in curr_entrusts
                        if isinstance(e, dict)
                    ]
                    if not any(bool(r) for r in remaining):
                        last_grid_error = TradeVerificationError(
                            f"待撤列表未包含有效 {entrust_field} 数据，无法确认委托单 {entrust_no} 是否已被成功撤销",
                            result={"message": "invalid_grid", "entrust_no": str(entrust_no), "verified": False, "pop_result": pop_res}
                        )
                        self.wait(0.3)
                        continue

                    # 表格有效，清除瞬态异常
                    last_grid_error = None
                    if str(entrust_no).strip() not in remaining:
                        return {
                            "message": "success",
                            "entrust_no": str(entrust_no),
                            "verified": True,
                        }

                # 3. 买单资金解冻辅助核验 (仅当为买单且记录了 pre_avail 时)
                if pre_avail is not None:
                    try:
                        bal = self.balance
                        if isinstance(bal, dict) and "可用金额" in bal:
                            curr_avail = float(str(bal["可用金额"]).replace(",", ""))
                            if curr_avail > pre_avail + 0.01:
                                return {
                                    "message": "success",
                                    "entrust_no": str(entrust_no),
                                    "verified": True,
                                    "detail": f"funds_unfrozen: {pre_avail} -> {curr_avail}",
                                }
                    except Exception as e:
                        logger.debug("Check unfreeze balance failed: %s", e)

                # 4. 视觉核验辅助 (当且仅当初始撤单即通过视觉定位完成且网格持续异常时)
                if target_coords is not None and last_grid_error is not None:
                    v_coords = self._locate_entrust_visually(
                        entrust_no, stock_code=stock_code, amount=amount
                    )
                    if v_coords is None:
                        return {
                            "message": "success",
                            "entrust_no": str(entrust_no),
                            "verified": True,
                            "detail": "visually_removed",
                        }

                self.wait(0.3)

            logger.warning("委托单 %s 撤单后未能在 %ss 内确认移除", entrust_no, verify_timeout)
            if last_grid_error:
                if isinstance(last_grid_error, TradeVerificationError):
                    raise last_grid_error
                raise TradeVerificationError(
                    f"委托单 {entrust_no} 撤单验证失败，网格数据异常: {last_grid_error}",
                    result={"message": "unconfirmed", "entrust_no": str(entrust_no), "verified": False, "pop_result": pop_res}
                ) from last_grid_error
            raise TradeVerificationError(
                f"委托单 {entrust_no} 撤单后未能在 {verify_timeout}s 内确认从待撤列表中移除",
                result={"message": "unconfirmed", "entrust_no": str(entrust_no), "verified": False, "pop_result": pop_res}
            )

        return pop_res

    def cancel_all_entrusts(self, max_retries=2):
        self.refresh()
        self._switch_left_menus(["撤单[F3]"])

        # 点击全部撤销控件
        clicked_cancel_all = False
        try:
            btn_cancel_all = self._app.top_window().child_window(
                control_id=self._config.TRADE_CANCEL_ALL_ENTRUST_CONTROL_ID,
                class_name="Button",
                title_re="""全撤.*""",
            )
            if btn_cancel_all.exists(timeout=1):
                btn_cancel_all.click()
                clicked_cancel_all = True
        except Exception as e:
            logger.warning("点击全撤按钮异常: %s", e)

        # 针对券商换肤版无句柄“全撤”图标，通过 VLM 视觉 Grounding 定位并点击
        if not clicked_cancel_all and getattr(self, "visual_oracle", None) is not None:
            try:
                screen_img = self._capture_screen_image()
                ground_res = self.visual_oracle.ground_control(screen_img, "全撤")
                if ground_res.found and ground_res.center:
                    self._click_coords(ground_res.center, window=self._main)
                    clicked_cancel_all = True
            except Exception as ex:
                logger.warning("视觉定位全撤按钮异常: %s", ex)
        self.wait(0.2)

        # 等待出现确认对话框并重试点击
        retry = 0
        while retry <= max_retries:
            if self.is_exist_pop_dialog():
                w = self._app.top_window()
                clicked = False
                for btn_name in ["是(Y)", "确定", "是(&Y)", "是"]:
                    try:
                        btn = w[btn_name]
                        if btn.exists():
                            btn.click()
                            clicked = True
                            break
                    except Exception as e:
                        logger.debug("点击弹窗按钮 %s 失败: %s", btn_name, e)
                self.wait(0.2)
                if clicked and not self.is_exist_pop_dialog():
                    break
            else:
                break
            retry += 1

        self.close_pop_dialog()

    @perf_clock
    def repo(self, security, price, amount, **kwargs):
        validate_trade_params(security, price, amount)
        self._switch_left_menus(["债券回购", "融资回购（正回购）"])

        return self.trade(security, price, amount)

    @perf_clock
    def reverse_repo(self, security, price, amount, **kwargs):
        validate_trade_params(security, price, amount)
        self._switch_left_menus(["债券回购", "融劵回购（逆回购）"])

        return self.trade(security, price, amount)

    @perf_clock
    def buy(self, security, price, amount, **kwargs):
        validate_trade_params(security, price, amount)
        self._switch_left_menus(["买入[F1]"])

        return self.trade(security, price, amount)

    @perf_clock
    def sell(self, security, price, amount, **kwargs):
        validate_trade_params(security, price, amount)
        self._switch_left_menus(["卖出[F2]"])

        return self.trade(security, price, amount)

    @perf_clock
    def market_buy(self, security, amount, ttype=None, limit_price=None, **kwargs):
        """
        市价买入
        :param security: 六位证券代码
        :param amount: 交易数量
        :param ttype: 市价委托类型，默认客户端默认选择，
                     深市可选 ['对手方最优价格', '本方最优价格', '即时成交剩余撤销', '最优五档即时成交剩余 '全额成交或撤销']
                     沪市可选 ['最优五档成交剩余撤销', '最优五档成交剩余转限价']
        :param limit_price: 科创板 限价

        :return: {'entrust_no': '委托单号'}
        """
        validate_market_trade_params(security, amount, limit_price)
        self._switch_left_menus(["市价委托", "买入"])

        return self.market_trade(security, amount, ttype, limit_price=limit_price)

    @perf_clock
    def market_sell(self, security, amount, ttype=None, limit_price=None, **kwargs):
        """
        市价卖出
        :param security: 六位证券代码
        :param amount: 交易数量
        :param ttype: 市价委托类型，默认客户端默认选择，
                     深市可选 ['对手方最优价格', '本方最优价格', '即时成交剩余撤销', '最优五档即时成交剩余 '全额成交或撤销']
                     沪市可选 ['最优五档成交剩余撤销', '最优五档成交剩余转限价']
        :param limit_price: 科创板 限价
        :return: {'entrust_no': '委托单号'}
        """
        validate_market_trade_params(security, amount, limit_price)
        self._switch_left_menus(["市价委托", "卖出"])

        return self.market_trade(security, amount, ttype, limit_price=limit_price)

    def market_trade(self, security, amount, ttype=None, limit_price=None, **kwargs):
        """
        市价交易
        :param security: 六位证券代码
        :param amount: 交易数量
        :param ttype: 市价委托类型，默认客户端默认选择，
                     深市可选 ['对手方最优价格', '本方最优价格', '即时成交剩余撤销', '最优五档即时成交剩余 '全额成交或撤销']
                     沪市可选 ['最优五档成交剩余撤销', '最优五档成交剩余转限价']

        :return: {'entrust_no': '委托单号'}
        """
        # 入口校验（审查标准 5.2）：代码与数量合法性，非法参数不进入 GUI 流程
        code, amount = validate_market_trade_params(
            security, amount, limit_price
        )

        self._type_edit_control_keys(self._config.TRADE_SECURITY_CONTROL_ID, code)
        if ttype is not None:
            retry = 0
            retry_max = 10
            while retry < retry_max:
                try:
                    self._set_market_trade_type(ttype)
                    break
                except TypeError:
                    # 市价类型不在候选项内属于确定性错误，重试无意义，立即失败
                    raise
                except Exception as e:
                    # GUI 控件未就绪等瞬时故障才重试
                    logger.debug("设置市价委托类型第 %d 次重试: %s", retry + 1, e)
                    retry += 1
                    self.wait(0.1)
            else:
                # 重试耗尽仍未设置成功：绝不在委托类型未确认时提交，
                # 防止以错误的市价类型成交（资金路径正确性）
                raise TradeError(
                    "市价委托类型设置失败: {}（已重试 {} 次），已取消提交".format(
                        ttype, retry_max
                    )
                )
        self._set_market_trade_params(code, amount, limit_price=limit_price)
        self._submit_trade()

        return self._handle_pop_dialogs(
            handler_class=pop_dialog_handler.TradePopDialogHandler
        )

    def _set_market_trade_type(self, ttype):
        """根据选择的市价交易类型选择对应的下拉选项"""
        selects = self._main.child_window(
            control_id=self._config.TRADE_MARKET_TYPE_CONTROL_ID, class_name="ComboBox"
        )
        for i, text in enumerate(selects.texts()):
            # skip 0 index, because 0 index is current select index
            if i == 0:
                if re.search(ttype, text):  # 当前已经选中
                    return
                else:
                    continue
            if re.search(ttype, text):
                selects.select(i - 1)
                return
        raise TypeError("不支持对应的市价类型: {}".format(ttype))

    def _set_stock_exchange_type(self, ttype):
        """根据选择的市价交易类型选择对应的下拉选项"""
        selects = self._main.child_window(
            control_id=self._config.TRADE_STOCK_EXCHANGE_CONTROL_ID, class_name="ComboBox"
        )

        for i, text in enumerate(selects.texts()):
            # skip 0 index, because 0 index is current select index
            if i == 0:
                if ttype.strip() == text.strip():  # 当前已经选中
                    return
                else:
                    continue
            if ttype.strip() == text.strip():
                selects.select(i - 1)
                return
        raise TypeError("不支持对应的市场类型: {}".format(ttype))

    def auto_ipo(self):
        self._switch_left_menus(self._config.AUTO_IPO_MENU_PATH)

        stock_list = self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

        if len(stock_list) == 0:
            return {"message": "今日无新股"}
        invalid_list_idx = [
            i for i, v in enumerate(stock_list) if v[self.config.AUTO_IPO_NUMBER] <= 0
        ]

        if len(stock_list) == len(invalid_list_idx):
            return {"message": "没有发现可以申购的新股"}

        self._click(self._config.AUTO_IPO_SELECT_ALL_BUTTON_CONTROL_ID)
        self.wait(0.1)

        for row in invalid_list_idx:
            self._click_grid_by_row(row)
        self.wait(0.1)

        self._click(self._config.AUTO_IPO_BUTTON_CONTROL_ID)
        self.wait(0.1)

        return self._handle_pop_dialogs()

    def get_dpi_scale_factor(self) -> float:
        """获取交易主窗口的 DPI 缩放比例因子，默认 1.0"""
        try:
            if self._main is not None:
                return get_window_dpi_scale(self._main.handle)
        except Exception:
            pass
        return get_window_dpi_scale(None)

    def _click_grid_by_row(self, row):
        scale = self.get_dpi_scale_factor()
        x = int(self._config.COMMON_GRID_LEFT_MARGIN * scale)
        y = int(
            (
                self._config.COMMON_GRID_FIRST_ROW_HEIGHT
                + self._config.COMMON_GRID_ROW_HEIGHT * (row + 0.5)
            )
            * scale
        )
        self._app.top_window().child_window(
            control_id=self._config.COMMON_GRID_CONTROL_ID,
            class_name="CVirtualGridCtrl",
        ).click(coords=(x, y))

    @perf_clock
    def is_exist_pop_dialog(self):
        self.wait(0.5)  # wait dialog display
        try:
            return (
                self._main.wrapper_object() != self._app.top_window().wrapper_object()
            )
        except (
            findwindows.ElementNotFoundError,
            timings.TimeoutError,
            RuntimeError,
        ) as ex:
            logger.exception("check pop dialog timeout")
            return False

    @perf_clock
    def close_pop_dialog(self):
        try:
            if self._main.wrapper_object() != self._app.top_window().wrapper_object():
                w = self._app.top_window()
                if w is not None:
                    w.close()
                    self.wait(0.2)
        except (
                findwindows.ElementNotFoundError,
                timings.TimeoutError,
                RuntimeError,
        ) as ex:
            logger.debug("关闭弹窗窗口失败（可能无弹窗或已关闭）: %s", ex)

    def _run_exe_path(self, exe_path):
        return os.path.join(os.path.dirname(exe_path), "xiadan.exe")

    def wait(self, seconds):
        time.sleep(seconds)

    def exit(self):
        self._app.kill()

    def _close_prompt_windows(self):
        self.wait(1)
        title_target = getattr(self._config, "TITLE", "网上股票交易系统5.0")
        for window in self._app.windows(class_name="#32770", visible_only=True):
            title = window.window_text()
            if title != self._config.TITLE and title_target not in title:
                logger.info("close window %s", title)
                window.close()
                self.wait(0.2)
        self.wait(1)

    def close_pormpt_window_no_wait(self):
        title_target = getattr(self._config, "TITLE", "网上股票交易系统5.0")
        for window in self._app.windows(class_name="#32770"):
            title = window.window_text()
            if title != self._config.TITLE and title_target not in title:
                window.close()

    def trade(self, security, price, amount):
        # 入口校验（审查标准 5.2 双向校验之入口侧）：非法参数直接拒绝，
        # 不进入 GUI 点击流程，避免把错误数据敲进交易客户端
        security, price, amount = validate_trade_params(
            security, price, amount
        )

        self._set_trade_params(security, price, amount)

        # 针对早盘高并发无回执阶段，在下单提交前预先抓取操作区双帧的前帧 (frame_before)
        frame_before = None
        if getattr(self, "visual_oracle", None) is not None:
            try:
                frame_before = self._capture_screen_image()
            except Exception as e:
                logger.debug("Pre-trade frame capture fallback: %s", e)

        self._submit_trade()

        res = self._handle_pop_dialogs(
            handler_class=pop_dialog_handler.TradePopDialogHandler
        )
        if getattr(self, "visual_oracle", None) is not None:
            try:
                screen_img = self._capture_screen_image()
                toast_res = self.visual_oracle.verify_status_bar_and_toast(screen_img)
                if toast_res.is_rejected:
                    raise TradeError(f"交易废单拒绝: {toast_res.reject_reason or toast_res.message}")
                if toast_res.entrust_no:
                    if isinstance(res, dict):
                        res["entrust_no"] = toast_res.entrust_no
                if toast_res.message and isinstance(res, dict) and "success" in str(res.get("message", "")):
                    res["message"] = toast_res.message

                # R3 落地：若未从 Toast/状态栏获取到明确的合同编号且已捕获下单前帧，或显式启用了双帧仲裁
                # 执行双帧差分仲裁，防范早盘高并发无回执阶段重复挂单与穿仓
                if frame_before is not None and (
                    not (isinstance(res, dict) and res.get("entrust_no"))
                    or getattr(self.visual_oracle, "enable_dual_frame", False)
                ):
                    from easytrader.vlm_visual_oracle import ArbitrationDecision

                    diff_res = self.visual_oracle.arbitrate_trade_receipt(
                        frame_before=frame_before,
                        frame_after=screen_img,
                    )
                    if diff_res.decision == ArbitrationDecision.SUBMIT_FAILED:
                        raise TradeError(f"双帧视觉仲裁判定下单失败: {'; '.join(diff_res.reasons)}")
                    if isinstance(res, dict):
                        res["visual_decision"] = diff_res.decision
                        res["visual_confidence"] = diff_res.confidence
                        res["suggested_action"] = diff_res.suggested_action
                        if diff_res.decision == ArbitrationDecision.AMBIGUOUS:
                            res["ambiguous"] = True
                            res["reasons"] = diff_res.reasons
                            if res.get("message") == "success":
                                res["message"] = (
                                    "ambiguous: order submission unconfirmed by visual diff, please query today entrusts"
                                )
            except TradeError:
                raise
            except Exception as e:
                logger.debug("Visual status bar/toast/dual-frame verification fallback: %s", e)
        return res

    def _click(self, control_id):
        self._app.top_window().child_window(
            control_id=control_id, class_name="Button"
        ).click()

    @perf_clock
    def _submit_trade(self):
        time.sleep(0.2)
        self._main.child_window(
            control_id=self._config.TRADE_SUBMIT_CONTROL_ID, class_name="Button"
        ).click()

    @perf_clock
    def __get_top_window_pop_dialog(self):
        return self._app.top_window().window(
            control_id=self._config.POP_DIALOD_TITLE_CONTROL_ID
        )

    @perf_clock
    def _get_pop_dialog_title(self):
        return (
            self._app.top_window()
            .child_window(control_id=self._config.POP_DIALOD_TITLE_CONTROL_ID)
            .window_text()
        )

    def _set_trade_params(self, security, price, amount):
        code = security[-6:]

        self._type_edit_control_keys(self._config.TRADE_SECURITY_CONTROL_ID, code)

        # wait security input finish
        self.wait(0.1)

        # 设置交易所
        # if security.lower().startswith("sz"):
        #     self._set_stock_exchange_type("深圳Ａ股")
        # if security.lower().startswith("sh"):
        #     self._set_stock_exchange_type("上海Ａ股")
        #
        # self.wait(0.1)

        self._type_edit_control_keys(
            self._config.TRADE_PRICE_CONTROL_ID,
            easyutils.round_price_by_code(price, code),
        )
        self._type_edit_control_keys(
            self._config.TRADE_AMOUNT_CONTROL_ID, str(int(amount))
        )

    def _set_market_trade_params(self, security, amount, limit_price=None):
        self._type_edit_control_keys(
            self._config.TRADE_AMOUNT_CONTROL_ID, str(int(amount))
        )
        self.wait(0.1)
        if str(security).startswith("68") and limit_price is not None:
            self._type_edit_control_keys(
                self._config.TRADE_PRICE_CONTROL_ID, str(limit_price)
            )

    def _get_grid_data(self, control_id, table_type=None):
        try:
            sig = inspect.signature(self.grid_strategy_instance.get)
            if "table_type" in sig.parameters:
                return self.grid_strategy_instance.get(control_id, table_type=table_type)
        except Exception:
            pass
        return self.grid_strategy_instance.get(control_id)

    def _type_keys(self, control_id, text):
        self._main.child_window(control_id=control_id, class_name="Edit").set_edit_text(
            text
        )

    def _type_edit_control_keys(self, control_id, text):
        if not self._editor_need_type_keys:
            self._main.child_window(
                control_id=control_id, class_name="Edit"
            ).set_edit_text(text)
        else:
            editor = self._main.child_window(control_id=control_id, class_name="Edit")
            editor.select()
            editor.type_keys(text)

    def type_edit_control_keys(self, editor, text):
        if not self._editor_need_type_keys:
            editor.set_edit_text(text)
        else:
            editor.select()
            editor.type_keys(text)

    def _collapse_left_menus(self):
        items = self._get_left_menus_handle().roots()
        for item in items:
            item.collapse()

    @perf_clock
    def _switch_left_menus(self, path, sleep=0.2):
        self.close_pop_dialog()
        try:
            self._get_left_menus_handle().get_item(path).select()
        except Exception as e:
            # 针对自绘折叠菜单树或无句柄树控件，回退至视觉 Grounding 定位
            if getattr(self, "visual_oracle", None) is not None:
                target_name = path[-1] if isinstance(path, (list, tuple)) else str(path)
                screen_img = self._capture_screen_image()
                ground_res = self.visual_oracle.ground_control(screen_img, target_name)
                if ground_res.found and ground_res.center:
                    self._click_coords(ground_res.center, window=self._main)
                elif isinstance(path, (list, tuple)) and len(path) > 1:
                    # 折叠状态下目标子项未展示，依次展开父级菜单层级
                    for item_name in path:
                        step_img = self._capture_screen_image()
                        step_res = self.visual_oracle.ground_control(step_img, str(item_name))
                        if step_res.found and step_res.center:
                            self._click_coords(step_res.center, window=self._main)
                            self.wait(0.2)
                        else:
                            raise
                else:
                    raise
            else:
                raise
        self._app.top_window().type_keys('{F5}')
        self.wait(sleep)

    def _switch_left_menus_by_shortcut(self, shortcut, sleep=0.5):
        self.close_pop_dialog()
        self._app.top_window().type_keys(shortcut)
        self.wait(sleep)

    @functools.lru_cache()
    def _get_left_menus_handle(self):
        count = 2
        while True:
            try:
                handle = self._main.child_window(
                    control_id=129, class_name="SysTreeView32"
                )
                if count <= 0:
                    return handle
                # sometime can't find handle ready, must retry
                handle.wait("ready", 2)
                return handle
            # pylint: disable=broad-except
            except Exception as ex:
                logger.exception("error occurred when trying to get left menus")
            count = count - 1

    @property
    def visual_oracle(self):
        return getattr(self, "_visual_oracle", None)

    @visual_oracle.setter
    def visual_oracle(self, oracle):
        self._visual_oracle = oracle

    def enable_visual_oracle(self, backend=None, enable_dual_frame: bool = False, **kwargs):
        from easytrader.vlm_visual_oracle import VLMVisualOracle
        self._visual_oracle = VLMVisualOracle(
            backend=backend, enable_dual_frame=enable_dual_frame, **kwargs
        )
        return self._visual_oracle

    def _capture_window_image(self, window=None):
        try:
            target = window if window is not None else self._app.top_window()
            if hasattr(target, "wrapper_object"):
                try:
                    target = target.wrapper_object()
                except Exception:
                    pass
            if hasattr(target, "capture_as_image"):
                return target.capture_as_image()
        except Exception:
            pass
        from PIL import Image
        return Image.new("RGB", (400, 300), color="white")

    def _capture_screen_image(self):
        try:
            target = self._main
            if hasattr(target, "wrapper_object"):
                try:
                    target = target.wrapper_object()
                except Exception:
                    pass
            if hasattr(target, "capture_as_image"):
                return target.capture_as_image()
        except Exception:
            pass
        from PIL import Image
        return Image.new("RGB", (800, 600), color="white")

    def _click_coords(self, coords, window=None):
        target = window or self._app.top_window()
        try:
            target.click_input(coords=coords)
        except Exception:
            try:
                target.click(coords=coords)
            except Exception as e:
                logger.debug("Click coords failed: %s", e)

    def _submit_dialog_confirm(self):
        w = self._app.top_window()
        for btn_title in ["确定", "是(Y)", "是(&Y)", "确认", "是"]:
            try:
                btn = w[btn_title]
                if btn.exists():
                    btn.click()
                    return
            except Exception:
                pass
        try:
            w.type_keys("%Y", set_foreground=False)
        except Exception:
            pass

    def _cancel_entrust_by_double_click(self, row):
        scale = self.get_dpi_scale_factor()
        x = int(self._config.CANCEL_ENTRUST_GRID_LEFT_MARGIN * scale)
        y = int(
            (
                self._config.CANCEL_ENTRUST_GRID_FIRST_ROW_HEIGHT
                + self._config.CANCEL_ENTRUST_GRID_ROW_HEIGHT * (row + 0.5)
            )
            * scale
        )
        if getattr(self, "visual_oracle", None) is not None:
            try:
                grid_ctrl = self._app.top_window().child_window(
                    control_id=self._config.COMMON_GRID_CONTROL_ID,
                    class_name="CVirtualGridCtrl",
                )
                wrapper = grid_ctrl
                if hasattr(grid_ctrl, "wrapper_object"):
                    try:
                        wrapper = grid_ctrl.wrapper_object()
                    except Exception:
                        pass
                if hasattr(wrapper, "capture_as_image"):
                    grid_img = wrapper.capture_as_image()
                    calibrated = self.visual_oracle.calibrate_grid_row(
                        grid_image=grid_img,
                        target_row=row,
                        nominal_x=x,
                        nominal_y=y,
                        first_row_height=int(self._config.CANCEL_ENTRUST_GRID_FIRST_ROW_HEIGHT * scale),
                        row_height=int(self._config.CANCEL_ENTRUST_GRID_ROW_HEIGHT * scale),
                    )
                    y = calibrated.calibrated_y
            except Exception as e:
                logger.debug("Visual row calibration fallback: %s", e)

        self._app.top_window().child_window(
            control_id=self._config.COMMON_GRID_CONTROL_ID,
            class_name="CVirtualGridCtrl",
        ).double_click(coords=(x, y))

    def refresh(self):
        self.refresh_strategy.set_trader(self)
        self.refresh_strategy.refresh()

    def _arbitrate_and_dismiss_modal_dialog(self, dialog_img=None):
        """利用 VLM 对阻断弹窗或 DirectUI 蒙层进行视觉智能仲裁并执行安全闭环操作"""
        if getattr(self, "visual_oracle", None) is None:
            raise TradeVerificationError(
                "未能识别弹窗控件 (ID 1365)，且无法进行视觉仲裁，严禁返回假成功",
                result={"message": "unhandled_unknown_dialog"},
            )

        if dialog_img is None:
            dialog_img = self._capture_window_image(self._app.top_window())

        decision = self.visual_oracle.arbitrate_modal_dialog(dialog_img)
        if decision.dialog_type == "CAPTCHA" or decision.requires_human:
            raise HumanInterventionRequiredError(
                f"检测到图形验证码，触发熔断: {decision.message}"
            )
        if decision.action in ("CHECK_AND_WAIT_CONFIRM", "WAIT_AND_CONFIRM"):
            if decision.has_checkbox and decision.checkbox_coord:
                self._click_coords(decision.checkbox_coord)
            if decision.countdown_seconds > 0:
                self.wait(decision.countdown_seconds + 0.2)
            if decision.button_coord:
                self._click_coords(decision.button_coord)
            else:
                self._submit_dialog_confirm()
            self.wait(0.2)
            return decision
        elif decision.action == "SKIP":
            if decision.button_coord:
                self._click_coords(decision.button_coord)
            else:
                self._submit_dialog_confirm()
            self.wait(0.2)
            return decision
        elif decision.action == "RAISE_ERROR":
            raise TradeError(decision.message)
        elif decision.action == "CONFIRM":
            if decision.button_coord:
                self._click_coords(decision.button_coord)
            else:
                self._submit_dialog_confirm()
            self.wait(0.2)
            return decision
        else:
            raise TradeVerificationError(
                f"未知阻断弹窗无法闭环: {decision.message}",
                result={"dialog_type": decision.dialog_type, "message": decision.message},
            )

    def check_liveness(self, hwnd=None):
        """非侵入式检查客户端界面存活性与健康状态"""
        if getattr(self, "visual_oracle", None) is not None:
            screen_img = self._capture_screen_image()
            return self.visual_oracle.inspect_liveness(screen_img, hwnd=hwnd, trader=self)
        from easytrader.vlm_visual_oracle import LivenessReport
        return LivenessReport(is_alive=True, state="HEALTHY")

    @perf_clock
    def _capture_dialog_evidence(self, title=None):
        """把当前弹窗截图落盘存证，返回路径（失败返回空串，绝不阻断）。"""
        shot_dir = (
            os.environ.get(pop_dialog_handler.DIALOG_SHOT_DIR_ENV)
            or pop_dialog_handler.DIALOG_SHOT_DIR_DEFAULT
        )
        try:
            os.makedirs(shot_dir, exist_ok=True)
            safe_title = re.sub(
                r"[^0-9A-Za-z\u4e00-\u9fa5]+", "_", str(title or "dialog")
            ).strip("_")[:24] or "dialog"
            filename = "{}_{}.png".format(
                safe_title,
                datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3],
            )
            path = os.path.join(shot_dir, filename)
            self._capture_window_image(self._app.top_window()).save(path)
            return path
        except Exception as ex:  # noqa: BLE001 存证失败不得影响交易
            logger.debug("弹窗存证截图失败: %s", ex)
            return ""

    @perf_clock
    def _handle_pop_dialogs(self, handler_class=pop_dialog_handler.PopDialogHandler):
        handler = handler_class(self._app)
        max_attempts = 10
        # 同标题连续「无进展」轮数上限：超过即提前熔断，不再空转满 10 轮。
        # 判据是「handler 本轮未执行任何关闭动作」，因此「委托确认」这类
        # 已发 %Y、正在等弹窗消失的合法慢路径不会被计入（见下方 acted 判定）。
        # 2026-10-27 真机实测：未知内容弹窗空转 18.3s（正常基线 6.4s）后才超时丢单。
        # 可用 env 覆盖（调试/兼容慢客户端），越界自动收敛到 [1, max_attempts]。
        max_stagnant = _resolve_max_stagnant(max_attempts)
        attempt = 0
        last_title = None
        stagnant = 0
        trace = []

        while self.is_exist_pop_dialog():
            attempt += 1
            if attempt > max_attempts:
                raise TradeVerificationError(
                    f"处理弹窗超过最大尝试次数 ({max_attempts})，防止无限循环",
                    result={
                        "message": "dialog_dismiss_timeout",
                        "dialog_title": last_title,
                        "dialog_trace": trace,
                        "screenshot": self._capture_dialog_evidence(last_title),
                    },
                )

            title = None
            try:
                title = self._get_pop_dialog_title()
            except (pywinauto.findwindows.ElementNotFoundError, Exception):
                self._arbitrate_and_dismiss_modal_dialog()
                continue

            # 若标题存在，先尝试交给 handler 处理
            result = None
            try:
                result = handler.handle(title)
            except (HumanInterventionRequiredError, CircuitBreakerOpenError):
                # ⛔ 熔断类异常必须**直接上抛**：绝不能落到下面的视觉仲裁分支
                # 被「吞掉 + continue」。否则一旦接了 visual_oracle，验证码弹窗
                # 会被反复点击（每轮都可能被券商记一次失败尝试）直到 timeout，
                # 而确定性关键词熔断反倒被概率性的 VLM 判定取代。
                trace.append({"attempt": attempt, "title": title, "result": "brake"})
                raise
            except Exception as e:
                # 先记一条轨迹：走视觉仲裁继续轮询时，超时异常要能带上现场
                trace.append(
                    {
                        "attempt": attempt,
                        "title": title,
                        "result": "raise:{}".format(type(e).__name__),
                    }
                )
                # 弹窗内控件缺失或非标弹窗抛出异常，回退至视觉智能仲裁
                if getattr(self, "visual_oracle", None) is not None:
                    self._arbitrate_and_dismiss_modal_dialog()
                    continue
                raise

            trace.append({"attempt": attempt, "title": title, "result": result})

            # 无进展检测：只有「handler 什么动作都没做，且同标题复现」才算空转。
            # ⚠️ 不能只看 `result is None`：「委托确认」的成功路径本身就是
            # return None（%Y 已发出、正在等弹窗消失），把它计入会把「慢但对」
            # 的委托误杀成确定性失败 —— 而失败发生在 %Y 已按下之后，代价不对称。
            acted = bool(getattr(handler, "last_action_taken", False))
            if result is None and not acted and title == last_title:
                stagnant += 1
            else:
                stagnant = 0
            last_title = title

            if stagnant >= max_stagnant:
                logger.error(
                    "弹窗连续 %d 轮无进展（title=%r），提前熔断避免无效空转",
                    stagnant + 1,
                    title,
                )
                raise TradeVerificationError(
                    f"弹窗连续 {stagnant + 1} 轮无进展"
                    f"（title={title!r}），提前熔断避免无效空转",
                    result={
                        "message": "dialog_stagnant",
                        "dialog_title": title,
                        "dialog_trace": trace,
                        "screenshot": self._capture_dialog_evidence(title),
                    },
                )

            if result and not ("unknown message" in str(result.get("message", ""))):
                return result

            # 若 handler 返回 None 或 unknown message，且启用了 visual_oracle，执行视觉智能仲裁闭环
            if getattr(self, "visual_oracle", None) is not None:
                self._arbitrate_and_dismiss_modal_dialog()
                continue

        # 当 is_exist_pop_dialog() 为 False 时（例如闪电交易或 DirectUI 页面内蒙层遮罩）：
        # 若启用了 visual_oracle，进行视觉存活性与全屏蒙层巡检，防止 DirectUI 蒙层静默假成功
        if getattr(self, "visual_oracle", None) is not None:
            screen_img = self._capture_screen_image()
            liveness = self.visual_oracle.inspect_liveness(screen_img, trader=self)
            if liveness.state == "MASK_LOCKED":
                self._arbitrate_and_dismiss_modal_dialog(dialog_img=screen_img)
            elif liveness.state in ("BLACK_SCREEN", "MESSAGE_PUMP_HUNG", "OFFLINE"):
                raise TradeError(
                    f"客户端视觉状态异常 [{liveness.state}]: {'; '.join(liveness.issues)}"
                )

        return {"message": "success"}


class BaseLoginClientTrader(ClientTrader):
    @abc.abstractmethod
    def login(self, user, password, exe_path, comm_password=None, **kwargs):
        """Login Client Trader"""
        pass

    def prepare(
        self,
        config_path=None,
        user=None,
        password=None,
        exe_path=None,
        comm_password=None,
        **kwargs
    ):
        """
        登陆客户端
        :param config_path: 登陆配置文件，跟参数登陆方式二选一
        :param user: 账号
        :param password: 明文密码
        :param exe_path: 客户端路径类似 r'C:\\htzqzyb2\\xiadan.exe', 默认 r'C:\\htzqzyb2\\xiadan.exe'
        :param comm_password: 通讯密码
        :return:
        """
        if config_path is not None:
            account = file2dict(config_path)
            user = account["user"]
            password = account["password"]
            comm_password = account.get("comm_password")
            exe_path = account.get("exe_path")
        self.login(
            user,
            password,
            exe_path or self._config.DEFAULT_EXE_PATH,
            comm_password,
            **kwargs
        )
        self._init_toolbar()
