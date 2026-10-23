# V2.10.0 发布准备

用户已确认当前系统版本为 V2.10.0。此前 14 项 Adapter 回放和 5 项真实桥接模块测试通过；本轮增加签名打包和安装回滚隔离测试。

## 本轮验证

运行：

```bash
python -m pip install -r requirements-release.txt
python scripts/verify_installation.py /path/to/v2.10.0/sync
```

测试只使用临时 RSA 测试密钥、临时插件目录及模拟下载/启动状态，未连接真实服务，没有向系统加入新签名者。测试私钥和测试安装包在结束时删除，不能作为正式发布材料。

12 项用例覆盖：

- 同一内容/密钥重复打包得到一致字节；Bybit 和 Gate 均通过真实包校验器。
- 私钥不匹配所选公钥时拒绝打包。
- 包 SHA-256 错误、未知签名者、源码被篡改均拒绝安装。
- 安装写入 ACTIVE 后，新建 Bridge 可加载候选源码，保留订单止损功能。
- 模拟下载中断不更改 ACTIVE；最低桥接版本不符时不下载。
- 安装后立即报错可恢复原 ACTIVE，旧插件文件保留。
- 手动回滚可移除 ACTIVE，保留候选文件供审计并恢复内置选择。
- 记录一个现有局限：状态仍为 connecting 时，V2.10.0 会报告安装成功。该测试通过仅证明已复现此行为，不代表已通过真实连接健康验收。

## 仍需真实验收

V2.10.0 远程安装流程只等待 1.5 秒，且只将 status=error 视为启动失败。DNS/TLS/认证/订阅错误如果较晚出现，可能错过自动回滚检查。不能将“安装成功”直接解释为已连接或成交已同步；上线后需检查各 WS 通道、一次手动历史补同步、队列 pending 和实际已知成交。

这些测试覆盖 Python 模块和临时文件操作，不覆盖真实 HTTPS、Docker 重启、磁盘写满/掉电或 VPS 网络异常。自动回滚仍依赖旧版本配置和可启动性。

## 正式签名工具已准备

scripts/build_signed_adapter.py 只接受本机现有 RSA 私钥及匹配的可信公钥记录，直接调用 V2.10.0 security_utils 的 canonical_manifest_payload 和 verify_manifest_signature。它不会生成新密钥、修改 registry.json 或上传包。

示例（路径按本机实际情况替换，私钥不发送到聊天或仓库）：

```bash
python scripts/build_signed_adapter.py src/bybit \
  --bridge-sync /path/to/v2.10.0/sync \
  --version 1.1.5 \
  --description '保留 V2.10.0 WebSocket 功能，增强历史成交校验与时间戳重签' \
  --signer trusted-signers/trade-journal-adapter-signer-20260920-r2.json \
  --private-key /private/local/path/publisher.pem \
  --output-dir /private/local/path/bybit-release
```

加密 PEM 可加 --password-prompt 在本机隐藏输入口令。输出 ZIP、SHA-256 和含待填写最终 HTTPS URL/日期的注册表草稿。必须由原有可信私钥完成正式签名，再审查最终包及注册表；候选源码不是可直接安装包。

当前没有获得现有 R2 发布私钥的本地路径，因此尚未制作正式可信安装包，也未合并或更新稳定注册表。若原私钥已不可用，需要另行决定新签名者和公钥导入方案，不能直接关闭验签。
