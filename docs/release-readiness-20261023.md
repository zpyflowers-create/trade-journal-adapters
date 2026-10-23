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

## R3 候选交付

原 R2 私钥无法确认保留，因此已准备独立 R3 发布身份。没有改动用户系统的可信列表；仍需用户手动导入新公钥后才能安装新包。旧 R2 公钥可以保留。

- 新签名者：trade-journal-adapter-publisher-20261023-r3。
- 公钥指纹：8d7cdc99cd503bfb5f0bc288a3b7ef70ea3231ca78f69647d94dd1802ec312ba。
- 已签名候选包：Bybit 1.1.5、Gate 1.2.3；只适用于 V2.10.0 或后续另行验证版本。
- 两包通过真实 V2.10.0 校验器：旧可信列表拒绝，导入 R3 后接受；导入保留 R2；安装到临时目录后，新建 Bridge 加载源码哈希与候选一致。
- SHA-256：Bybit b34b4230d3c3bcf29ba79ba6d225597f2d7159d0dedb5018439e73a3d0852184；Gate 13d6c35a901b37624e55856389a03f15f2ce80ea0477788bbdaed466913b9aa6。

公钥 JSON、更新合集 ZIP 和独立私钥备份已经分别交付。更新合集不含私钥，私钥不进入 Git 仓库。稳定 registry、旧包和实际 VPS/NAS 均未更改，PR 保持草稿，候选仍待真实账户安装验收。

安装顺序：保存私钥备份→备份现有系统→导入 R3 公钥→按当前使用平台一次安装一个内层 Adapter ZIP→检查通道/历史补同步/队列并核对已知成交。无需为测试新增交易。**本地安装分支没有远程安装的自动回滚检查**，出现问题应使用 Adapter 回滚按钮；不可把“安装成功”当作真实同步健康结论。
