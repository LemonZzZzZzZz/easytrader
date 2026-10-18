# 远端服务模式

远端服务模式是交易服务端和量化策略端分离的模式。

**交易服务端**通常是有固定`IP`地址的云服务器，该服务器上运行着`easytrader`交易服务。而**量化策略端**可能是`JoinQuant、RiceQuant、Vn.Py`，物理上与交易服务端不在同一台电脑上。交易服务端被动或主动获取交易信号，并驱动**交易软件**（交易软件包括运行在同一服务器上的下单软件，比如同花顺`xiadan.exe`，或者运行在另一台服务器上的雪球`xq`）。


## 交易服务端——启动服务

```python
from easytrader import server

server.run(port=1430) # 默认端口为 1430
```

## 量化策略端——调用服务

```python
from easytrader import remoteclient

user = remoteclient.use('使用客户端类型，可选 yh_client, ht_client, ths, xq等', host='服务器ip', port='服务器端口，默认为1430')

user.buy(......)

user.sell(......)
```

远端买卖和撤单请求的客户端等待上限为 30 秒。若发生超时，服务端可能仍在处理，
委托结果属于未知状态；不要直接重复提交。应先查询 `today_entrusts`、
`today_trades` 或 `cancel_entrusts` 确认状态，再决定后续操作。

