# coding:utf-8
import json

# 网络请求默认超时（秒）——审查标准 5.2：所有 requests 调用必须带 timeout，
# 防止券商 / 数据平台无响应时交易跟踪线程无限挂起
HTTP_TIMEOUT = 10


def parse_cookies_str(cookies):
    """
    parse cookies str to dict
    :param cookies: cookies str
    :type cookies: str
    :return: cookie dict
    :rtype: dict
    """
    cookie_dict = {}
    for record in cookies.split(";"):
        key, value = record.strip().split("=", 1)
        cookie_dict[key] = value
    return cookie_dict


def file2dict(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def grep_comma(num_str):
    return num_str.replace(",", "")


def str2num(num_str, convert_type="float"):
    num = float(grep_comma(num_str))
    return num if convert_type == "float" else int(num)
