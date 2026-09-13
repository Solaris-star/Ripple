**中文** | [English](README_EN.md)

# Ripple

Ripple 是面向自媒体创作者的本地优先内容工作台，当前版本 **0.2.7**。

它把选题、母版内容、平台版本、素材、Agent 协作、账号连接、审核和发布回执放在同一条工作流里。真实发布、登录确认和互动写操作都需要用户明确触发。

## 当前能力

- **选题与热点**：热点雷达、选题库、账号画像辅助推荐。
- **内容工作台**：Mother 内容、素材/成品、平台版本、AI 协作。
- **发布管理**：预检、版本审核、执行或排期、回执核对；结果不明确时禁止盲目重发。
- **Agent**：检测并复用用户已有的 OpenCode、Claude Code、Codex；Hermes 在满足本机运行条件时可接入。安装 Ripple 不会覆盖这些 Agent 的现有配置。
- **平台连接**：X、微信公众号、小红书、快手、微信视频号、知乎、Bilibili 等提供已实现的本地/官方接口路径。首个公开版本不内置抖音登录/直连发布器（旧实现有 AGPL 血缘，已从发行树排除）；抖音趋势与已有本地 Profile 的只读数据兼容仍可使用。TikTok 仍处于接入规划状态，界面不会把规划能力伪装成可用连接。
- **Blog Connector**：通过受控 OpenAPI 连接兼容 Blog，也支持 Markdown/素材导出。

## 快速开始

### Windows

需要 Python 3.10+、Node.js 22.19+。视频/音频处理建议另外安装 FFmpeg。

```powershell
git clone https://github.com/Solaris-star/Ripple.git
cd Ripple
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-Ripple.ps1
```

浏览器打开 `http://127.0.0.1:7860`。

`setup.ps1` 只在仓库内创建 `.venv`、安装项目依赖并构建前端；它不会全局安装或修改 OpenCode / Claude Code / Codex / Hermes，也不会预装第三方 ACP。检测到本机 Agent 后，Ripple 才在设置页展示已验证的适配选项；外部 ACP 仅在用户明确点击后安装到 Workspace 私有目录（默认 `.ripple-private/outputs/agent-adapters/`）。

### Linux / macOS

```bash
git clone https://github.com/Solaris-star/Ripple.git
cd Ripple
bash setup.sh
.venv/bin/python -X utf8 web/app.py
```

Ripple 的私密凭据按机器本地加密保存：Windows 使用 DPAPI；Linux/macOS 使用 AES-256-GCM，并把 0600 权限的机器密钥放在 `~/.config/ripple/secret.key`（可用绝对路径 `RIPPLE_SECRET_KEY_FILE` 覆盖）。密文不会设计成跨机器可迁移；迁移 Server 后应重新连接平台账号。

当前发布基线以 **Windows 本地运行** 为主要验证目标。Linux/macOS 已提供核心安装、Web 服务与 Secret Store 路径，并进入 CI 验证；涉及真实浏览器登录、桌面 Agent、媒体工具或平台风控的集成仍需在目标主机逐项验证。`setup.sh` 可执行不等同于所有平台连接已完成 Linux/macOS Server 端到端认证。

## Local 与 Server 模式

默认是 `local`：监听回环地址，本机单用户免登录。

Server 模式必须放在 HTTPS 反向代理后，并配置可信来源、可信 Host 和部署者初始化码：

```text
RIPPLE_DEPLOYMENT_MODE=server
RIPPLE_PUBLIC_ORIGIN=https://ripple.example.com
RIPPLE_TRUSTED_HOSTS=ripple.example.com
RIPPLE_BOOTSTRAP_CODE=<至少 24 个字符的一次性高熵值>
```

首次 Owner 创建后应从运行环境移除 `RIPPLE_BOOTSTRAP_CODE` 并重启服务。Server 模式使用 HttpOnly Session Cookie、CSRF 校验和 Workspace 访问边界；当前版本只开放一个默认 Workspace，不宣称完整 SaaS 多租户隔离。

## 数据与隐私

运行时数据默认分为：

- `outputs/`：用户内容、素材与任务产物；
- `.ripple-private/`：账号凭据、浏览器 Profile、私有连接与操作状态；
- `.env`：本机模型/API 配置。

这些目录/文件都不应提交 Git。仓库已提供 `.gitignore`，发布前仍建议执行独立密钥扫描。

不要把真实 Cookie、Token、AppSecret、浏览器 Profile 或个人素材放进 issue、测试夹具、日志或 Pull Request。

## 开发与验证

```powershell
python -m pytest -q
cd web\frontend
npm ci
npm run build
npm run lint
npm audit
```

更完整的开发、Server 配置、平台限制和发布前检查见：

- `docs/ripple/DEVELOPMENT.md`
- `docs/ripple/RELEASE_CHECKLIST.md`
- `SECURITY.md`
- `CONTRIBUTING.md`

## 许可与第三方来源

Ripple 自研代码按根目录 `LICENSE` 中的 Apache License 2.0 发布。仓库包含或改编了部分第三方 Skill / 参考实现；其来源、许可和保留声明集中在 `THIRD_PARTY_NOTICES.md` 与 `LICENSES/`。

第三方名称仅用于兼容性、来源说明或真实集成对象，不代表其对 Ripple 的背书。
