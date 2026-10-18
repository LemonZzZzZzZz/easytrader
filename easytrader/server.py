import functools

from flask import Flask, jsonify, request

from . import api
from .log import logger
from .utils.validation import ensure_positive_number

app = Flask(__name__)

global_store = {}


def error_handle(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        # pylint: disable=broad-except
        except Exception as e:
            logger.exception("server error")
            message = "{}: {}".format(e.__class__, e)
            return jsonify({"error": message}), 400

    return wrapper


def _validate_trade_payload(json_data):
    """远端交易接口入口校验（审查标准 5.2 远端接口注入面）。

    只校验各 trader 通用的不变量：security 为非空字符串，价格 / 数量
    （若提供）必须为正数；证券代码格式校验由各 trader 自行负责
    （同花顺客户端为 6 位代码，雪球为带交易所前缀形式）。
    """
    if not isinstance(json_data, dict):
        raise ValueError("请求体必须为 JSON 对象")
    security = json_data.get("security")
    if not isinstance(security, str) or not security.strip():
        raise ValueError("security 不能为空且必须为字符串")
    volume = None
    if "volume" in json_data:
        volume = ensure_positive_number(json_data["volume"], "volume")
    if "price" in json_data:
        price = json_data["price"]
        if volume is not None and not isinstance(price, bool) and price == 0:
            pass
        else:
            ensure_positive_number(price, "price")
    if "amount" in json_data:
        amount = json_data["amount"]
        if volume is not None and not isinstance(amount, bool) and amount == 0:
            pass
        else:
            ensure_positive_number(amount, "amount")
    if not any(k in json_data for k in ("price", "volume")):
        # 仅有 amount 而无价格 / 成交量对同花顺（缺 price）与雪球
        # （volume = price * amount = 0）均为无效请求
        raise ValueError("buy/sell 至少需要提供 price 或 volume 参数")


def _validate_cancel_payload(json_data):
    """远端撤单接口入口校验：entrust_no 不能为空（与 clienttrader 侧一致）。"""
    if not isinstance(json_data, dict):
        raise ValueError("请求体必须为 JSON 对象")
    entrust_no = json_data.get("entrust_no")
    if entrust_no is None or not str(entrust_no).strip():
        raise ValueError("entrust_no 不能为空")


@app.route("/prepare", methods=["POST"])
@error_handle
def post_prepare():
    json_data = request.get_json(force=True)

    user = api.use(json_data.pop("broker"))
    user.prepare(**json_data)

    global_store["user"] = user
    return jsonify({"msg": "login success"}), 201


@app.route("/balance", methods=["GET"])
@error_handle
def get_balance():
    user = global_store["user"]
    balance = user.balance

    return jsonify(balance), 200


@app.route("/position", methods=["GET"])
@error_handle
def get_position():
    user = global_store["user"]
    position = user.position

    return jsonify(position), 200


@app.route("/auto_ipo", methods=["GET"])
@error_handle
def get_auto_ipo():
    user = global_store["user"]
    res = user.auto_ipo()

    return jsonify(res), 200


@app.route("/today_entrusts", methods=["GET"])
@error_handle
def get_today_entrusts():
    user = global_store["user"]
    today_entrusts = user.today_entrusts

    return jsonify(today_entrusts), 200


@app.route("/today_trades", methods=["GET"])
@error_handle
def get_today_trades():
    user = global_store["user"]
    today_trades = user.today_trades

    return jsonify(today_trades), 200


@app.route("/cancel_entrusts", methods=["GET"])
@error_handle
def get_cancel_entrusts():
    user = global_store["user"]
    cancel_entrusts = user.cancel_entrusts

    return jsonify(cancel_entrusts), 200


@app.route("/buy", methods=["POST"])
@error_handle
def post_buy():
    json_data = request.get_json(force=True)
    user = global_store["user"]
    _validate_trade_payload(json_data)
    res = user.buy(**json_data)

    return jsonify(res), 201


@app.route("/sell", methods=["POST"])
@error_handle
def post_sell():
    json_data = request.get_json(force=True)

    user = global_store["user"]
    _validate_trade_payload(json_data)
    res = user.sell(**json_data)

    return jsonify(res), 201


@app.route("/cancel_entrust", methods=["POST"])
@error_handle
def post_cancel_entrust():
    json_data = request.get_json(force=True)

    user = global_store["user"]
    _validate_cancel_payload(json_data)
    res = user.cancel_entrust(**json_data)

    return jsonify(res), 201


@app.route("/exit", methods=["GET"])
@error_handle
def get_exit():
    user = global_store["user"]
    user.exit()

    return jsonify({"msg": "exit success"}), 200


def run(port=1430):
    app.run(host="0.0.0.0", port=port)
