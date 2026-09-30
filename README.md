# easytrader

[![Package](https://img.shields.io/pypi/v/easytrader.svg)](https://pypi.python.org/pypi/easytrader)
[![License](https://img.shields.io/github/license/shidenggui/easytrader.svg)](https://github.com/shidenggui/easytrader/blob/master/LICENSE)

* 进行股票量化交易
* 通用的同花顺客户端模拟操作与实操加固
* **[新特性]** 基于视觉大模型（VLM）的多模态交易安全卫士与自适应降级体系
* 支持券商的 [miniqmt](https://easytrader.readthedocs.io/zh-cn/master/miniqmt/) 官方量化接口
* 支持雪球组合调仓和跟踪
* 支持远程操作客户端
* 支持跟踪 `joinquant`, `ricequant` 的模拟交易

---

## 🔥 同花顺客户端实战加固与视觉先知 (THS Hardening & VLM Oracle)

针对同花顺 MFC 自绘老旧架构（`CVirtualGridCtrl`）在实盘交易中频发的**自绘控件假下单、空表假撤单、高分屏文字截断、无弹窗静默废单、高并发重复挂单及阻断弹窗卡死**等顽疾，本项目深度加固了底层通信与容错契约，并引入了基于多模态大模型的视觉安全卫士系统。

### 1. 核心加固特性一览

| 模块 | 传统版本隐患 | 本分支（fix/ths-gui-fixes）加固机制 |
|---|---|---|
| **委托输入** | 内存级 `set_edit_text` 对自绘控件偶发假成功 | 默认启用可靠的键盘消息仿真 `_editor_need_type_keys = True` |
| **撤单验证** | 撤单超时返回软字典 `{"message": "unconfirmed"}`，空表被误判为撤单成功 | 强类型 `TradeVerificationError(TradeError)` 契约，**严禁空表与假核验假成功** |
| **资金市值** | 缺少市值控件 ID，带千分位逗号时解析崩溃 | 补齐 `股票市值: 1014`，数字清洗千分位与异常保护 |
| **持仓表格** | 表格末尾“汇总/合计”行被混入实际持仓干扰对账 | 引入 `_filter_summary_rows` 自动剔除统计行 |
| **网格异常** | 临时文件解析失败宽泛吞异常返回 `[]` | 异常显式传播，临时 `.xls` 文件安全 `finally` 释放防锁死 |
| **网格降级** | 剪贴板死锁或文件损坏时无路可退 | **多级责任链 `FallbackChain`**：剪贴板 $\rightarrow$ 本地OCR $\rightarrow$ 本地VLM |
| **质检守恒** | 依赖外部单据，无法甄别模型幻觉或错位 | **`AdaptiveSchemaValidator`**：市值联动防假空表，守恒律校验（可用 $\le$ 余额） |
| **视觉安全** | 闪电交易无弹窗静默拒单、未知倒计时弹窗卡死 | **`VLMVisualOracle` 视觉先知**：状态栏废单捕获、弹窗倒计时智能决策、双帧差分防重买 |

---

### 2. 安装与环境依赖

```bash
# 推荐：直接从本强化版分支源码安装
pip install git+https://github.com/LemonZzZzZzZz/easytrader.git@fix/ths-gui-fixes

# 或者本地克隆开发模式安装
git clone -b fix/ths-gui-fixes https://github.com/LemonZzZzZzZz/easytrader.git
cd easytrader
pip install -e .
```

> **可选多模态大模型依赖**：
> 本库设计坚持**零强制新增第三方 pip 依赖**（完全复用自带的 `requests` 与 `pillow` 与本地/远程 Ollama REST API 通信）。若需使用视觉多模态大模型能力，仅需本机安装并启动 [Ollama](https://ollama.com/) 即可。
> 
> **推荐模型与启动参数配置（实测 8GB 显卡流畅运行）**：
> ```bash
> # 拉取实盘验证推荐的视觉模型（二选一）：
> ollama pull qwen3.6:35b      # Q4_K_M 量化，支持 Vision/Thinking，实测热请求仅 8.9s
> ollama pull qwen2.5-vl:7b    # 约 5GB 显存，极速轻量
> ```

---

### 3. 快速上手

#### 基础初始化与使用

```python
import easytrader

# 初始化通用同花顺客户端
user = easytrader.use('universal_client')
user.prepare('ths.json')

# 正常调用：底层已默认享受输入仿真、千分位容错、汇总行过滤与 Fail-Loud 撤单验证
balance = user.balance
positions = user.position
print("资金情况:", balance)
print("实际持仓:", positions)
```

---

#### 进阶 1：一键启用网格数据 VLM 多级降级通道

当遇上 2x 高分屏模糊、剪贴板占用或特殊字体导致本地读取失败时，自动启动无感降级：

```python
from easytrader.log import logger

# 一键启用三级降级链路 (Primary -> ScreenshotOCR -> OllamaVLM)
user.enable_vlm_fallback(
    model="qwen3.6:35b",               # 支持本地 Ollama 部署的模型（如 qwen3.6:35b / qwen2.5-vl:7b）
    host="http://localhost:11434",
    timeout=300.0,                     # 默认 300s，适应超大模型冷加载
    think=False,                       # 禁用思考链，提取耗时从 27s 降至 8.9s 并防截断
    circuit_breaker=True,              # 开启线程安全熔断器
    on_fallback=lambda info: logger.warning("⚠️ 触发降级通道告警: %s", info)
)

# 读取持仓（主通道正常时毫秒级返回，异常时自动大模型兜底并过市值对账闸门）
positions = user.position
```

---

#### 进阶 2：一键启用全流程视觉安全卫士体系 (VLMVisualOracle)

在交易与撤单全链路中提供 5 大视觉安全兜底（状态栏废单捕获、未知弹窗倒计时处理、双帧差分防重发等）：

```python
# 启用全流程视觉安全卫士
user.enable_vlm_visual_oracle(
    model="qwen3.6:35b",
    host="http://localhost:11434",
    enable_status_bar_verification=True,        # 捕获无弹窗下单状态栏/Toast废单原因与合同号
    enable_dialog_arbitration=True,             # 智能处理非标弹窗 (倒计时确认/改密跳过/验证码熔断)
    enable_dual_frame_trade_arbitration=True,   # 开盘高并发下单前后双帧差分，杜绝重复挂单穿仓
)

# 正常下单：自动全程受视觉安全护航
result = user.buy('300434', price=12.92, amount=100)

# 7x24 无人值守巡检：一键检测客户端消息泵、通讯指示灯与渲染状态
health = user.check_liveness()
if not health["is_healthy"]:
    logger.error("客户端异常警报: %s", health["issues"])
```

---

### 4. 自动化测试与质量保障

本项目包含严格的单元测试与端到端模拟测试体系，通过 Mock 离线图像、故障注入与边界探测确保 100% 稳定性：

```bash
# 运行全部单测（含 73 项视觉安全卫士单测 + 44 项降级责任链单测 + 基础套件）
python -m unittest discover tests

# 结果：238 项单测全部通过（0 失败，0 错误）
```

---

## 微信群以及公众号

欢迎大家扫码关注公众号「食灯鬼」，一起交流。进群可通过菜单加我好友，备注量化。

![公众号二维码](https://camo.githubusercontent.com/6fad032c27b30b68a9d942ae77f8cc73933b95cea58e684657d31b94a300afd5/68747470733a2f2f67697465652e636f6d2f73686964656e676775692f6173736574732f7261772f6d61737465722f755069632f6d702d71722e706e67)

若二维码因 Github 网络无法打开，请点击[公众号二维码](https://camo.githubusercontent.com/6fad032c27b30b68a9d942ae77f8cc73933b95cea58e684657d31b94a300afd5/68747470733a2f2f67697465652e636f6d2f73686964656e676775692f6173736574732f7261772f6d61737465722f755069632f6d702d71722e706e67)直接打开图片。

### Author

> Blog [@shidenggui](https://shidenggui.com) · Weibo [@食灯鬼](https://www.weibo.com/u/1651274491) · Twitter [@shidenggui](https://twitter.com/shidenggui)

### 相关

* [easyquotation 实时获取全市场股票行情](https://github.com/shidenggui/easyquotation)
* [easyquant 简单的量化框架](https://github.com/shidenggui/easyquant)

### 模拟交易

* 雪球组合 by @[haogefeifei](https://github.com/haogefeifei)（[说明](docs/xueqiu.md)）

### 使用文档

[中文文档](https://easytrader.readthedocs.io/)

