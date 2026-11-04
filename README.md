# Trade Journal Adapters

网页交易复盘系统的可信 Adapter 在线更新仓库。

## 规则
- `registry.json` 是 V2.6.51+ 使用的稳定更新索引。
- Adapter 安装包必须通过 HTTPS 下载。
- 每个在线包必须提供 SHA-256。
- Adapter ZIP 内的 `manifest.json` 必须通过复盘系统已经导入的可信签名公钥验证。
- 本仓库禁止提交任何 API Key、Secret、Token、账户资料或签名私钥。
- 在线更新只负责“检查 + 手工安装”，不会自动安装。

## 目录
- `gate/`
- `bybit/`
- `ibkr/`
- `bitget/`
- `binance/`
- `okx/`

当前稳定索引补齐 2026-10-23 R3 签名包：Bybit 1.1.5、Gate 1.2.3，最低 Bridge 2.10.0。已经安装同版本时不会显示升级按钮。

维护源码见 `sources/`；签名打包与离线校验工具见 `tools/adapter_release.py`；具体步骤见[发布流程](发布流程.md)。工具不会联网发布或自动安装，API 变化仍需先评估、修改和测试。

```bash
python3 tools/adapter_release.py verify
python3 -m unittest discover -s tools -p 'test_adapter_release.py'
```


