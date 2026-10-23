# Adapter 检查及修复候选

检查日期：2026-10-23（北京时间）。

## 关键更正：仓库包与完整系统不同

| 来源 | 核实结果 |
| --- | --- |
| 仓库基准 9f97d7a4c65522d96e5b2e9351e75bee81eb2f77 | 注册表为 Bybit 1.1.2、Gate 1.2.2；最低桥接版本 2.6.51；最后提交为 2026-09-20，查询无开放 Issue。 |
| 最新可找到的 trade-journal-nas-v2.10.0-docker.zip，2026-10-21 生成 | 内置 Bybit 1.1.4 已有全局历史排序、订单/仓位 WS 和普通条件单人工确认候选。Bridge 已调度启动和断线历史恢复。不能认定用户实际部署必然就是此包。 |

原先“Bybit 缺少补拉、条件单覆盖和全局排序”的结论只适用于旧仓库 Adapter，不能推广到 V2.10.0 系统。最终候选保留新功能，移除第一版额外加入的 Adapter 恢复循环，避免双重调度。

V2.10.0 优先使用版本更高的内置实现：旧 Bybit 插件 1.1.2 不覆盖内置 1.1.4。候选使用 1.1.5-rc1；未来发布最低桥接版本至少为经过联调的 2.10.0，不能沿用 2.6.51。

## 包及签名

| 包 | SHA-256 |
| --- | --- |
| Bybit 1.1.2 | 8a0380a5364202ba15c1b3ad4f7609a62203dfd7bf271b53097274f3fefc2a94 |
| Gate 1.2.2 | 0f816b5474c5b334d3cff48deea341f6819854481866fc6ce73534893df30963 |

两个包和注册表一致，包内 adapter.py/CHANGELOG.md 与 manifest 哈希一致。用真实 V2.10.0 security_utils 验证两包 RSA 签名：仓库 R2 公钥通过，未知签名者和篡改 manifest 被拒绝。不证明部署已经导入该公钥。

## 官方依据

| 来源 | 用于检查的规则 |
| --- | --- |
| [Gate 现货文档](https://www.gate.com/docs/developers/apiv4/en/spot/)，Query personal trading records | my_trades 支持 from/to/page，limit 最大 1000，limit*(page-1) 不超过 100000；时间过滤按订单结束时间。 |
| [Gate 永续文档](https://www.gate.com/docs/developers/apiv4/en/futures/)，Query personal trading records by time range | my_trades_timerange 支持 from/to/limit/offset；响应使用 trade_id、create_time。 |
| [Bybit 成交历史](https://bybit-exchange.github.io/docs/v5/order/execution) | execTime 降序、nextPageCursor、limit 最大 100、每次时间跨度最多七天；同毫秒排序还需考虑 execId/orderId/leavesQty。 |
| [Bybit 订单查询](https://bybit-exchange.github.io/docs/v5/order/open-order) | 条件订单有独立字段，不能只看持仓 stopLoss 判断覆盖。 |

没有认定某条新公告已造成线上故障。

## 已实现及验证

- P0：Gate 两类成交分页、固定窗口完整读取和成功后推进水位；首次请求失败不滑动开始点。
- P1：Gate 重复页/冲突 ID/无效时间/异常响应明确报错；Bybit 保留全局排序并加强响应校验。
- 保留最新 Bybit 订单/仓位/保证金 WS、异步成交工作队列和止损候选，合入旧稳定包的时间中点校准及重签。
- Gate 合入旧稳定包的 TLS 网络重试与止损连续失败判定/恢复。

14 项 Adapter 离线回放通过：1505 条成交、现货 page/永续 offset、秒时间字段、分页失败、首次失败锚定、emit 失败、水位推进、重复页、缺失 ID/时间、手动补同步、Bybit 跨页顺序与游标循环。

5 项真实 V2.10.0 模块兼容测试通过：
1. 跨月开仓→加仓→减仓→平仓为一个生命周期；完全平仓再开仓生成第二条，重复历史不增加 fills，手填止损及复盘备注保留。
2. 无 reduceOnly/closeOnTrigger 的普通条件单产生 stop_candidate，需人工确认。
3. 只减仓止损产生 protective_stop。
4. 两个旧发布包的 R2 验签通过；未知公钥与篡改被拒绝。
5. 旧 Bybit 插件不覆盖较新内置版本。

Bridge.emit 调用 ExecutionQueue.enqueue 后 SQLite 已 commit；归并失败保留 pending。去重键为 provider:account:execution_id，未包括市场/symbol。以上是源码与离线证据，不是线上验证。

## 剩余风险与下一步

| 优先级 | 待办与证据缺口 |
| --- | --- |
| P0 | 核实当前实际部署版本，回放匿名真实订单：单双向、部分平仓、反向开仓、普通条件单及初始 R。候选分流测试不能证明所有订单类型均覆盖。 |
| P0 | 真实 Bridge 启动只补一天，断线补 1–7 天；需要数据库永久 checkpoint 和周期对账。Gate 按订单结束时间过滤，长期订单部分成交与迟到数据需单独验证。 |
| P1 | Gate 多止损仍可能重复事件：setdefault 未阻止逐条 emit；价格订单仅取 100 条，枚举/分页及候选选取待官方文档和订单样本核对。 |
| P1 | Gate abs(fee) 的返佣符号、积分/GT 抵扣及费用币种待官方说明和桥接约定，不盲改符号。 |
| P1 | 实时坏时间仍可能回退当前时间；本轮只加强历史/轮询数据检查。 |
| P1 | HTTP 429/5xx、响应头和并发预算需要有界退避；网络重试不等于全部错误自动恢复。 |
| P1 | 多市场相同成交 ID 与同毫秒排序需进一步验证，当前去重键未包括市场。 |
| P1 | 下载验签已见实现，最低版本拦截、安装中断、旧版保留、重启加载与回滚仍需临时部署验收。 |
| P1 | 通道状态仍混用 connected/warning，止损恢复可能覆盖其他通道故障。 |

未使用交易所凭据，未连接真实账户，未更改实际部署、稳定注册表、旧 ZIP 或可信公钥。候选仅提供未签名模板。正式发布必须由现有受信任流程签名，不关闭验签，也不提交私钥。
