import re

import requests
from PIL import Image

from easytrader import exceptions
from easytrader.log import logger


def captcha_recognize(img_path):
    """
    识别同花顺拷贝数据等验证码图片。
    优先使用纯 Python/ONNX 的 ddddocr（无需外部依赖，识别率高）；
    若未安装则降级至 pytesseract；
    两者均不可用时抛出异常由上层清理弹窗并降级。
    """
    try:
        import ddddocr

        ocr = ddddocr.DdddOcr(show_ad=False)
        with open(img_path, "rb") as f:
            img_bytes = f.read()
        res = ocr.classification(img_bytes)
        return str(res).strip() if res else ""
    except ImportError:
        pass
    except Exception as e:
        logger.debug("ddddocr 识别异常: %s，尝试 pytesseract", e)

    try:
        import pytesseract

        im = Image.open(img_path).convert("L")
        threshold = 200
        table = [0 if i < threshold else 1 for i in range(256)]
        out = im.point(table, "1")
        num = pytesseract.image_to_string(out)
        return str(num).strip()
    except ImportError:
        raise exceptions.TradeError(
            "未检测到验证码识别组件（ddddocr 或 pytesseract）。"
            "建议执行 `pip install ddddocr` 或切换至 `ScreenshotOCR` 策略彻底免疫剪贴板反爬验证码。"
        )
    except Exception as e:
        raise exceptions.TradeError(f"验证码识别失败: {e}")


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
