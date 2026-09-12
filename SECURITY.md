# Security Policy

## Supported version

当前公开开发分支只维护最新的 Ripple 0.2.x 状态。安全修复优先落到最新提交。

## Report a vulnerability

请优先使用 GitHub 的 **Private vulnerability reporting / Security advisory** 联系维护者。不要把以下内容放到公开 Issue、Discussion、PR 或截图中：

- API Key、OAuth code/token、AppSecret、Cookie、SESSDATA；
- `.ripple-private/`、Browser Profile、state.json；
- 真实用户素材、未公开稿件或账号身份数据；
- 可直接用于接管公开 Ripple Server 的会话/初始化凭据。

报告请包含受影响版本、最小复现步骤、影响边界和建议修复方向。测试 PoC 应使用合成数据与本地/Mock 服务。

## Deployment guidance

- 默认 Local 模式只监听本机回环地址。
- Server 模式必须使用 HTTPS 反向代理，并配置 `RIPPLE_PUBLIC_ORIGIN`、`RIPPLE_TRUSTED_HOSTS` 与高熵 `RIPPLE_BOOTSTRAP_CODE`。
- 首个 Owner 建立后应移除初始化码并重启服务。
- Secret Store 在 Windows 使用 DPAPI；Linux/macOS 使用机器本地 AES-256-GCM 密钥（默认 `~/.config/ripple/secret.key`，权限必须为 0600）。密文不应跨机器复用。
- 不要直接把 Uvicorn 开发服务暴露公网。

## Secret handling

`.env*`、`.ripple-private/`、Cookie、Browser Profile 和真实 outputs 都应留在本机并被 Git 忽略。提交前请使用专用 secret scanner 扫描当前树和拟公开 Git 历史。

发现真实凭据已经进入 Git 历史时，先吊销/轮换凭据，再处理历史；仅删除当前文件无法使历史中的秘密失效。