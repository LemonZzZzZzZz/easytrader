# coding:utf-8
"""资金路径入口参数校验。

对应《代码审查标准》5.2「资金路径双向校验」的入口侧：
证券代码、价格、数量的合法性在进入 GUI 点击 / 网络请求之前完成校验，
非法参数直接拒绝，避免把错误数据敲进交易客户端或发给远端接口。
"""
import math
import re

# 6 位数字证券代码（A 股 / 债券回购等），兼容 sh / sz 前缀形式
SECURITY_CODE_RE = re.compile(r"^[0-9]{6}$")


def extract_security_code(security):
    """提取 6 位证券代码，兼容 "600519" / "sh600519" / "SZ000001" 形式。

    :raises ValueError: security 不是字符串
    """
    if not isinstance(security, str):
        raise ValueError(
            "证券代码不合法: {!r}，应为字符串（如 '600519' 或 'sh600519'）".format(
                security
            )
        )
    code = security.strip()
    if code[:2].lower() in ("sh", "sz"):
        code = code[2:]
    return code


def validate_security_code(security):
    """校验证券代码合法性，返回规范化后的 6 位代码字符串。

    :raises ValueError: 代码不是 6 位数字
    """
    code = extract_security_code(security)
    if not SECURITY_CODE_RE.match(code):
        raise ValueError(
            "证券代码不合法: {!r}，应为 6 位数字代码（可带 sh/sz 前缀）".format(
                security
            )
        )
    return code


def ensure_positive_number(value, name):
    """校验为正有限数值并返回 float，接受 int / float / 数字字符串。

    :raises ValueError: 非数值、NaN、inf 或小于等于 0
    """
    if isinstance(value, bool):
        raise ValueError("{}不合法: {!r}，应为数值".format(name, value))
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError) as ex:
        raise ValueError(
            "{}不合法: {!r}，应为数值".format(name, value)
        ) from ex
    if math.isnan(num) or math.isinf(num):
        raise ValueError("{}不合法: {!r}，应为有限数值".format(name, value))
    if num <= 0:
        raise ValueError("{}必须大于 0: {!r}".format(name, value))
    return num


def ensure_positive_quantity(value, name):
    """校验数量取整后仍大于零，避免小于 1 的数量被截成零下单。"""
    quantity = ensure_positive_number(value, name)
    if int(quantity) <= 0:
        raise ValueError("{}取整后必须大于 0: {!r}".format(name, value))
    return quantity


def validate_trade_params(security, price, amount):
    """限价交易入口校验（buy / sell / repo / reverse_repo）。

    数量允许小数（由调用方按需取整，与既有 _set_trade_params 行为一致），
    但必须为正。

    :return: (code, price, amount)，code 为 6 位字符串，price/amount 为正数
    :raises ValueError: 任一参数不合法
    """
    code = validate_security_code(security)
    price_num = ensure_positive_number(price, "价格")
    amount_num = ensure_positive_quantity(amount, "数量")
    return code, price_num, amount_num


def validate_market_trade_params(security, amount, limit_price=None):
    """市价交易入口校验（market_buy / market_sell，无价格参数）。

    :return: (code, amount)，code 为 6 位字符串，amount 为正数
    :raises ValueError: 任一参数不合法
    """
    code = validate_security_code(security)
    amount_num = ensure_positive_quantity(amount, "数量")
    if limit_price is not None:
        ensure_positive_number(limit_price, "限价")
    return code, amount_num
