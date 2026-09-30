# -*- coding: utf-8 -*-
import abc
import functools
import logging
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional, Type, Union

import hashlib, binascii

import easyutils
from pywinauto import findwindows, timings

from easytrader import grid_strategies, pop_dialog_handler, refresh_strategies
from easytrader.config import client
from easytrader.exceptions import TradeError, TradeVerificationError
from easytrader.grid_strategies import IGridStrategy
from easytrader.log import logger
from easytrader.refresh_strategies import IRefreshStrategy
from easytrader.utils.misc import file2dict
from easytrader.utils.perf import perf_clock
from easytrader.utils.win_gui import get_window_dpi_scale

if not sys.platform.startswith("darwin"):
    import pywinauto
    import pywinauto.clipboard

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

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @property
    def today_entrusts(self):
        self._switch_left_menus(["查询[F4]", "当日委托"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @property
    def today_trades(self):
        self._switch_left_menus(["查询[F4]", "当日成交"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @property
    def cancel_entrusts(self):
        self.refresh()
        self._switch_left_menus(["撤单[F3]"])

        return self._get_grid_data(self._config.COMMON_GRID_CONTROL_ID)

    @perf_clock
    def cancel_entrust(self, entrust_no, max_retries=2, verify_timeout=2.0):
        if entrust_no is None or not str(entrust_no).strip():
            raise ValueError("entrust_no 不能为空")

        self.refresh()
        target_idx = None
        for i, entrust in enumerate(self.cancel_entrusts):
            val = str(entrust.get(self._config.CANCEL_ENTRUST_ENTRUST_FIELD, "")).strip() if isinstance(entrust, dict) else ""
            if val == str(entrust_no).strip():
                target_idx = i
                break

        if target_idx is None:
            return {"message": "委托单状态错误不能撤单, 该委托单可能已经成交或者已撤"}

        self._cancel_entrust_by_double_click(target_idx)

        # 点击确认弹窗，并增加重试机制避免首次点击未响应
        retry = 0
        while retry <= max_retries:
            if self.is_exist_pop_dialog():
                w = self._app.top_window()
                clicked = False
                for btn_title in ["是(Y)", "确定", "是(&Y)", "是"]:
                    try:
                        btn = w[btn_title]
                        if btn.exists():
                            btn.click()
                            clicked = True
                            break
                    except Exception:
                        pass
                self.wait(0.2)
                if clicked and not self.is_exist_pop_dialog():
                    break
            else:
                break
            retry += 1

        pop_res = self._handle_pop_dialogs()

        # 如果弹窗明确提示撤单失败或状态异常（如已成交、废单、不可撤单等），严禁误判为成功，必须立即抛出 TradeVerificationError
        pop_msg = str(pop_res.get("message", "")).strip() if isinstance(pop_res, dict) else ""
        error_keywords = ("失败", "不能", "无法", "不可", "错误", "已成交", "已成", "废单", "已撤", "不存在", "异常", "拒绝")
        if any(kw in pop_msg for kw in error_keywords):
            raise TradeVerificationError(
                f"委托单 {entrust_no} 撤单状态异常: {pop_msg}",
                result={"message": "rejected", "entrust_no": str(entrust_no), "verified": False, "pop_result": pop_res}
            )

        # 异步状态二次核对：短轮询确认目标单是否已从待撤列表中消失
        if verify_timeout > 0:
            start_t = time.time()
            last_grid_error = None
            while time.time() - start_t < verify_timeout:
                self.refresh()
                try:
                    curr_entrusts = self.cancel_entrusts
                except Exception as e:
                    last_grid_error = e
                    logger.warning("获取待撤列表异常: %s", e)
                    self.wait(0.3)
                    continue

                if not curr_entrusts:
                    # 严禁将空网格误判为订单已被撤销
                    last_grid_error = TradeVerificationError(
                        f"待撤列表为空，无法确认委托单 {entrust_no} 是否已被成功撤销",
                        result={"message": "empty_grid", "entrust_no": str(entrust_no), "verified": False, "pop_result": pop_res}
                    )
                    self.wait(0.3)
                    continue

                # 显式断言表格获取有效（非空且包含有效合同编号），严禁将缺少字段的异常网格误判为撤单成功
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

                # 表格获取有效后，清除历史瞬态异常并核对委托单是否已消失
                last_grid_error = None
                if str(entrust_no).strip() not in remaining:
                    return {"message": "success", "entrust_no": str(entrust_no), "verified": True}
                self.wait(0.3)

            logger.warning("委托单 %s 撤单后未能在 %ss 内确认从待撤列表中移除", entrust_no, verify_timeout)
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
        try:
            btn_cancel_all = self._app.top_window().child_window(
                control_id=self._config.TRADE_CANCEL_ALL_ENTRUST_CONTROL_ID,
                class_name="Button",
                title_re="""全撤.*""",
            )
            if btn_cancel_all.exists(timeout=1):
                btn_cancel_all.click()
        except Exception as e:
            logger.warning("点击全撤按钮异常: %s", e)
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
                    except Exception:
                        pass
                self.wait(0.2)
                if clicked and not self.is_exist_pop_dialog():
                    break
            else:
                break
            retry += 1

        self.close_pop_dialog()

    @perf_clock
    def repo(self, security, price, amount, **kwargs):
        self._switch_left_menus(["债券回购", "融资回购（正回购）"])

        return self.trade(security, price, amount)

    @perf_clock
    def reverse_repo(self, security, price, amount, **kwargs):
        self._switch_left_menus(["债券回购", "融劵回购（逆回购）"])

        return self.trade(security, price, amount)

    @perf_clock
    def buy(self, security, price, amount, **kwargs):
        self._switch_left_menus(["买入[F1]"])

        return self.trade(security, price, amount)

    @perf_clock
    def sell(self, security, price, amount, **kwargs):
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
        code = security[-6:]
        self._type_edit_control_keys(self._config.TRADE_SECURITY_CONTROL_ID, code)
        if ttype is not None:
            retry = 0
            retry_max = 10
            while retry < retry_max:
                try:
                    self._set_market_trade_type(ttype)
                    break
                except:
                    retry += 1
                    self.wait(0.1)
        self._set_market_trade_params(security, amount, limit_price=limit_price)
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
            pass

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
                logging.info("close window %s" % title)
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
        self._set_trade_params(security, price, amount)

        self._submit_trade()

        return self._handle_pop_dialogs(
            handler_class=pop_dialog_handler.TradePopDialogHandler
        )

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

    def _get_grid_data(self, control_id):
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
        self._get_left_menus_handle().get_item(path).select()
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
        self._app.top_window().child_window(
            control_id=self._config.COMMON_GRID_CONTROL_ID,
            class_name="CVirtualGridCtrl",
        ).double_click(coords=(x, y))

    def refresh(self):
        self.refresh_strategy.set_trader(self)
        self.refresh_strategy.refresh()

    @perf_clock
    def _handle_pop_dialogs(self, handler_class=pop_dialog_handler.PopDialogHandler):
        handler = handler_class(self._app)

        while self.is_exist_pop_dialog():
            try:
                title = self._get_pop_dialog_title()
            except pywinauto.findwindows.ElementNotFoundError:
                return {"message": "success"}

            result = handler.handle(title)
            if result:
                return result
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
