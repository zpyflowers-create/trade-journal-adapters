# Adapter 候选源码

本目录是未签名、未发布的候选实现，不能直接安装。

基于最新可找到的完整包 trade-journal-nas-v2.10.0-docker.zip（2026-10-21 生成）的内置 Adapter，合入仓库旧稳定包的重试改进及本轮成交完整性修复。仓库基准为 9f97d7a4c65522d96e5b2e9351e75bee81eb2f77。

Bybit 内置已为 1.1.4，候选模板使用 1.1.5-rc1；Gate 候选为 1.2.3-rc1。未来发布最低桥接版本应至少为经过联调的 2.10.0，不能照搬旧注册表的 2.6.51。模板移除了旧签名和文件哈希，稳定 registry、旧 ZIP、公钥记录均未改动。

## 最终改动

- Gate 现货按 page、永续按 offset 完整拉取固定窗口，全局按时间排序；手动历史补同步复用。兼容秒级 create_time 和 trade_id。
- Gate 在全部请求、解析、有效性检查及 emit 成功后推进水位；首次请求失败也锚定原窗口。重复页、无效 ID/时间、冲突 ID 和分页超限明确失败。
- Bybit 保留 V2.10.0 的订单/仓位 WebSocket、WS 保证金快照、异步成交工作队列和普通条件单人工确认候选。
- Bybit 保留 V2.10.0 已有跨页全局排序与 200000 条安全上限，增强响应结构、ID/时间、价格数量及循环游标检查。
- Bybit 合入稳定包的服务器时间中点校准与时间戳重签；Gate 合入稳定包的网络/TLS 重试、止损连续失败判定与恢复。
- 不新增 Adapter 恢复循环：真实 V2.10.0 Bridge 已有启动/断线 history_sync 调度，第一版候选中的额外恢复循环已移除。

## 验证

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python scripts/verify_bridge_compatibility.py /path/to/v2.10.0/sync
```

14 项 Adapter 离线回放、5 项使用真实 V2.10.0 模块和临时 SQLite 的兼容测试通过。覆盖分页/失败水位、跨月生命周期、重放去重、人工复盘字段保护、条件止损分流、旧发布包 RSA 验签与版本优先规则。没有真实账户或部署验收。

## 边界

Bridge.emit 在 ExecutionQueue.enqueue 提交 SQLite 后才进行归并；归并失败保留 pending。因此水位代表进入持久队列，不代表全部成功归并。去重键是 provider:account:execution_id，未包括市场或 symbol，跨市场 ID 唯一性仍待核实。

V2.10.0 启动默认补一天，断线按时长补 1–7 天，没有永久恢复水位，长停机和迟到成交仍需额外对账。Gate 超过 100000 偏移的窗口明确失败，需缩小时间范围，尚无自动拆窗。

所找到的 V2.10.0 文件不证明当前实际部署的版本或可信公钥配置。其他待办见 docs/adapter-review-20261023.md。正式发布必须重新生成文件哈希、manifest 签名和 ZIP SHA-256，并验证安装/重启/回滚。不要提交签名私钥。
