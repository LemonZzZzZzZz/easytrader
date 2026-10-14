import re

import requests
from PIL import Image

from easytrader import exceptions


def captcha_recognize(img_path):
    import pytesseract

    im = Image.open(img_path).convert("L")
    # 1. threshold the image
    threshold = 200
    table = []
    for i in range(256):
        if i < threshold:
            table.append(0)
        else:
            table.append(1)

    out = im.point(table, "1")
    # 2. recognize with tesseract
    num = pytesseract.image_to_string(out)
    return num


def recognize_verify_code(image_path, broker="ths"):
    """识别验证码，返回识别后的字符串，使用 tesseract 实现
    :param image_path: 图片路径
    :param broker: 券商标识
    :return recognized: verify code string"""
    # 调用 tesseract 识别
    return default_verify_code_detect(image_path)


def default_verify_code_detect(image_path):
    img = Image.open(image_path)
    return invoke_tesseract_to_recognize(img)


def invoke_tesseract_to_recognize(img):
    import pytesseract

    try:
        res = pytesseract.image_to_string(img)
    except FileNotFoundError:
        raise Exception(
            "tesseract 未安装，请至 https://github.com/tesseract-ocr/tesseract/wiki 查看安装教程"
        )
    valid_chars = re.findall("[0-9a-z]", res, re.IGNORECASE)
    return "".join(valid_chars)
