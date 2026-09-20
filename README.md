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

当前 registry 可以先为空；发布第一个已签名 Adapter 后，再把对应版本写入 `registry.json`。
