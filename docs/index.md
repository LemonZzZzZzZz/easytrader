# 简介

* 通用的同花顺客户端模拟操作与实操加固
* **[新特性]** 基于视觉大模型（VLM）的多模态交易安全卫士与自适应降级体系
* 支持券商的 [miniqmt](miniqmt.md) 官方量化接口
* 支持雪球组合调仓和跟踪
* 支持远程操作客户端
* 支持跟踪 `joinquant`, `ricequant` 的模拟交易

---

## 🔥 同花顺客户端实操加固与视觉安全卫士

针对同花顺自绘老旧架构（`CVirtualGridCtrl`）在实盘中常见的**假下单、空表假撤单、高分屏截断、无弹窗静默废单、高并发重复挂单及阻断弹窗卡死**等顽疾，本项目深度加固了底层通信与容错契约，并引入了基于多模态大模型的视觉安全卫士系统。

### 核心功能与使用示例

```python
import easytrader

user = easytrader.use('universal_client')
user.prepare('ths.json')

# 1. 一键启用网格数据三级视觉降级通道 (Copy -> OCR -> VLM)
user.enable_vlm_fallback(model="qwen3.6:35b", host="http://localhost:11434")

# 2. 一键启用交易全流程视觉安全卫士 (废单捕获、弹窗倒计时决策、双帧差分防重买)
user.enable_vlm_visual_oracle(
    model="qwen3.6:35b",
    host="http://localhost:11434",
    enable_status_bar_verification=True,
    enable_dialog_arbitration=True,
    enable_dual_frame_trade_arbitration=True,
)

# 正常交易操作
positions = user.position
user.buy('300434', price=12.92, amount=100)

# 运维巡检
health = user.check_liveness()
```

### 加微信群以及公众号

欢迎大家扫码关注公众号"食灯鬼"，通过菜单加我好友，备注量化进群

![JDRUhz](https://camo.githubusercontent.com/6fad032c27b30b68a9d942ae77f8cc73933b95cea58e684657d31b94a300afd5/68747470733a2f2f67697465652e636f6d2f73686964656e676775692f6173736574732f7261772f6d61737465722f755069632f6d702d71722e706e67)


### 支持券商


* 海通客户端(海通网上交易系统独立委托)
* 华泰客户端(网上交易系统（专业版Ⅱ）)
* 国金客户端(全能行证券交易终端PC版)
* 通用同花顺客户端(同花顺免费版)
* 其他券商专用同花顺客户端(需要手动登陆)


### 模拟交易

* 雪球组合 by @[haogefeifei](https://github.com/haogefeifei)（[说明](xueqiu.md)）



### 作者

> Blog [@shidenggui](https://shidenggui.com) · Weibo [@食灯鬼](https://www.weibo.com/u/1651274491) · Twitter [@shidenggui](https://twitter.com/shidenggui)
>

**其他作品**

* [easyquotation 实时获取全市场股票行情](https://github.com/shidenggui/easyquotation)
* [easyquant 简单的量化框架](https://github.com/shidenggui/easyqutant)


