# -*- coding: utf-8 -*-
def create(broker):
    if broker == "ths":
        return CommonConfig
    if broker == "universal":
        return UNIVERSAL
    raise NotImplementedError(f"不支持的客户端配置类型: {broker}")


class CommonConfig:
    DEFAULT_EXE_PATH: str = ""
    TITLE = "网上股票交易系统5.0"

    # 交易所类型。 深圳A股、上海A股
    TRADE_STOCK_EXCHANGE_CONTROL_ID = 1003

    # 撤销界面上， 全部撤销按钮
    TRADE_CANCEL_ALL_ENTRUST_CONTROL_ID = 30001

    TRADE_SECURITY_CONTROL_ID = 1032
    TRADE_PRICE_CONTROL_ID = 1033
    TRADE_AMOUNT_CONTROL_ID = 1034

    TRADE_SUBMIT_CONTROL_ID = 1006

    TRADE_MARKET_TYPE_CONTROL_ID = 1541

    COMMON_GRID_CONTROL_ID = 1047

    COMMON_GRID_LEFT_MARGIN = 10
    COMMON_GRID_FIRST_ROW_HEIGHT = 30
    COMMON_GRID_ROW_HEIGHT = 16

    BALANCE_MENU_PATH = ["查询[F4]", "资金股票"]
    POSITION_MENU_PATH = ["查询[F4]", "资金股票"]
    TODAY_ENTRUSTS_MENU_PATH = ["查询[F4]", "当日委托"]
    TODAY_TRADES_MENU_PATH = ["查询[F4]", "当日成交"]

    BALANCE_CONTROL_ID_GROUP = {
        "资金余额": 1012,
        "可用金额": 1016,
        "可取金额": 1017,
        "股票市值": 1014,
        "总资产": 1015,
    }

    POP_DIALOD_TITLE_CONTROL_ID = 1365

    GRID_DTYPE = {
        "操作日期": str,
        "委托编号": str,
        "申请编号": str,
        "合同编号": str,
        "证券代码": str,
        "股东代码": str,
        "资金帐号": str,
        "资金帐户": str,
        "发生日期": str,
    }

    CANCEL_ENTRUST_ENTRUST_FIELD = "合同编号"
    CANCEL_ENTRUST_GRID_LEFT_MARGIN = 50
    CANCEL_ENTRUST_GRID_FIRST_ROW_HEIGHT = 30
    CANCEL_ENTRUST_GRID_ROW_HEIGHT = 16

    AUTO_IPO_SELECT_ALL_BUTTON_CONTROL_ID = 1098
    AUTO_IPO_BUTTON_CONTROL_ID = 1006
    AUTO_IPO_MENU_PATH = ["新股申购", "批量新股申购"]
    AUTO_IPO_NUMBER = '申购数量'





class UNIVERSAL(CommonConfig):
    DEFAULT_EXE_PATH = r"c:\\ths\\xiadan.exe"

    BALANCE_CONTROL_ID_GROUP = {
        "资金余额": 1012,
        "可用金额": 1016,
        "可取金额": 1017,
        "股票市值": 1014,
        "总资产": 1015,
    }

    AUTO_IPO_NUMBER = '可申购数量'
