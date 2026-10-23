# Adapter 兼容性检查与第一轮修复

检查日期：2026-10-23（北京时间）。基准：`9f97d7a4c65522d96e5b2e9351e75bee81eb2f77`。

## 已确认的发布状态

稳定注册表仍为 Bybit 1.1.2、Gate 1.2.2，最低桥接版本 2.6.51。最新提交为 2026-09-20 的保护性止损监听稳定性修复。仓库查询无开放 Issue，不代表没有缺陷。

展开前核验：

| 包 | SHA-256 |
| --- | --- |
| Bybit 1.1.2 | `8a0380a5364202ba15c1b3ad4f7609a62203dfd7bf271b53097274f3fefc2a94` |
| Gate 1.2.2 | `0f816b5474c5b334d3cff48deea341f6819854481866fc6ce73534893df30963` |

两包与注册表一致，包内 adapter.py 和 CHANGELOG.md 与 manifest 的哈希一致。未将这些哈希检查冒充 RSA 数字签名验证。

## 官方约束核实

| 官方来源 | 本次用于实现的约束 |
| --- | --- |
| [Gate 现货成交](https://www.gate.com/docs/developers/apiv4/en/spot/#query-personal-trading-records) | `/spot/my_trades` 支持 `from/to`、`page`，limit 最大 1000；`limit*(page-1)` 不超过 100000；时间过滤按订单结束时间处理。 |
| [Gate 永续文档](https://www.gate.com/docs/developers/apiv4/en/futures/)：Query personal trading records by time range | `/futures/{settle}/my_trades_timerange` 支持 `from/to/limit/offset`；响应使用 `trade_id`、`create_time`。 |
| [Bybit 成交历史](https://bybit-exchange.github.io/docs/v5/order/execution) | 按 execTime 降序，使用 nextPageCursor；limit 最大 100；每次查询时间跨度最多七天。同毫秒成交顺序需额外考虑 execId、orderId、leavesQty。 |
| [Bybit 开放与已关闭订单](https://bybit-exchange.github.io/docs/v5/order/open-order) | 独立订单接口和游标存在；这不能证明 position.stopLoss 覆盖所有条件订单。 |

本轮确认的是接口查询规则，不宣称发现某次新公告导致现有系统故障。

## 第一轮已实现（候选源码，未发布）

| 优先级 | 问题 | 处理 |
| --- | --- | --- |
| P0 | Gate 只取成交第一页 | 现货分页、永续偏移分页；手动历史同步复用；固定窗口完整读取后排序。 |
| P0 | Gate 请求前推进水位 | 全窗口成功解析并被 emit 接受后推进；失败重试原范围。 |
| P0 | Bybit 重连没有补拉 | 订阅后先 REST 补拉，再处理实时消息；保留确认前收到的 execution。 |
| P1 | Bybit 各页内部排序导致跨页逆序 | 收齐游标页后全局排序，再推送。 |
| P1 | 错误分页或响应可能被视为成功 | 重复页、循环游标、异常列表、缺少 ID/时间等报错并保留恢复位置。 |

## 发布前必须完成

1. 取得与实际部署版本对应的桥接源码，核对 AdapterBase、emit 持久化确认、去重键、历史重排及仓位重建。
2. 回放：开仓→断网/停机→加仓→部分平仓→完全平仓→恢复。验证全生命周期一条交易、每次成交独立、完全平仓后再开仓生成新编号；同时覆盖跨天和跨月。
3. 验证 REST/WS 重叠、同毫秒多笔成交、多账户相同 symbol、双向持仓不会重复或混并。检查当前 schema 版本和字段是否由桥接层正确解析。
4. 验证启动七天回看造成的历史重放不会覆盖人工复盘、初始止损或已有杠杆数据。
5. 使用现有受信任发布流程签名；测试客户端 R2 信任、未知签名者拒绝、哈希不符拒绝、最低版本拦截、断网安装、旧版保留、重启后加载版本与回滚。当前源码模板不应进入稳定注册表。

## 仍未解决的风险

| 优先级 | 项目 | 需要的证据或下一步 |
| --- | --- | --- |
| P0 | Bybit 普通条件止损未覆盖 | 现有 watcher 仅读取 USDT linear 持仓 stopLoss。需核对 realtime/history/order WS 的 stopOrderType、triggerDirection、triggerBy、positionIdx、订单方向与数量；针对未启用 reduceOnly/closeOnTrigger 的订单，必须区分普通反向开仓、止盈和保护性止损，不能只看反向订单就写入初始 R。 |
| P1 | 两家启动时止损基线和四秒轮询盲区 | 已有止损不发出事件是刻意防止将移动后止损回填为初始值。需要桥接层保存生命周期与首次观测证据；未知继续标记未知，保留人工输入。 |
| P1 | Gate 多个止损重复事件与分页 | 现有 setdefault 未阻止逐条事件发送；需先聚合候选再选取一次，补价格订单分页，按官方 order_type/trigger.rule 和订单样本验证。 |
| P1 | Gate 费用 abs 和其他费用币种 | 官方字段仅说明 trade fee，不足以单凭此决定该 API 的费用符号转换。需官方符号说明、实际返佣/积分/GT 抵扣样本和桥接层费用约定；原始字段保留。 |
| P1 | 无效时间回退为当前时间 | 本轮历史/轮询路径增加检查；实时路径仍需隔离坏记录，不能制造当天成交。 |
| P1 | HTTP 429/5xx 与限流 | Gate 网络错误会重试而 HTTP 错误直接失败；Bybit主要处理时间戳重签。需按官方错误码、响应头和桥接并发预算设计有界退避，不能把认证错误无限重试。 |
| P1 | 连接状态与同步健康混用 | WS 连通、REST 可读、止损可用、历史完整性应分别报告，避免止损恢复覆盖成交故障状态。 |
| P1 | 永久水位及对账 | 本轮内存水位不代替数据库 checkpoint；进程重启、迟到成交、超出历史范围均需桥接层持久恢复和周期对账。 |

## 验证范围

离线回放覆盖：超过 1000 笔成交、两类 Gate 分页、秒时间字段、第二页失败、首次失败水位锚定、emit 失败、水位成功推进、重复页、异常 ID/时间、手动补同步、Bybit 全局排序、游标循环、分类失败、长缺口拆段、订阅确认前成交、异常成功响应和运行循环断线补拉。

没有使用交易所凭据，没有连接真实账户，没有修改部署、稳定注册表、旧 ZIP 或可信公钥。
