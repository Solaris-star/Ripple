# Ripple 0.2.7 · 开发与运行

## 本地开发

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-Ripple.ps1
```

### Linux / macOS

```bash
bash setup.sh
.venv/bin/python -X utf8 web/app.py
```

默认访问 `http://127.0.0.1:7860`。Windows 可用 `Start-Ripple.ps1 -Port 7861` 调整端口。

安装器只创建仓库内 `.venv`、安装项目依赖、构建前端并按需安装 Playwright Chromium。它不会全局安装或修改用户已有的 OpenCode、Claude Code、Codex 或 Hermes。

## 平台支持边界

- Windows 本地运行是当前主要验证基线。
- Linux/macOS 已覆盖核心安装、Web 服务、测试与 Secret Store 路径；真实浏览器登录、桌面 Agent、媒体处理和各平台连接仍需在目标主机逐项验证。存在 `setup.sh` 只表示提供安装路径，不代表所有连接器已经完成 Linux/macOS Server 端到端认证。
- Windows 使用 DPAPI 保护平台私密凭据。
- Linux/macOS 使用 AES-256-GCM 保护私密凭据；机器密钥默认位于 `~/.config/ripple/secret.key`，必须保持 0600 权限，也可通过绝对路径 `RIPPLE_SECRET_KEY_FILE` 覆盖。
- Secret Store 是机器本地边界。迁移 Server 时应在新机器重新连接微信公众号、Blog、X 等账号，不要把密文复制过去并假定可以解密。
- 浏览器网页适配器会受平台页面、账号权限和风控变化影响；真实发布结果以回执核对为准。

## 数据目录

- `outputs/`：用户内容、素材与生成产物。以 `_` 开头的兼容/内部命名空间不能通过“素材与成品”接口读取或删除。
- `.ripple-private/`：Workspace 状态库、账号凭据、Browser Profile、OAuth、连接配置和操作状态。
- `.env`：本机模型/API 配置。
- Linux/macOS 的 `~/.config/ripple/secret.key`（或 `RIPPLE_SECRET_KEY_FILE`）：机器本地加密密钥，不属于仓库数据，也不能提交或共享。
- `profiles/`：本地账号画像；仓库只提交模板。

`.env*`、`.ripple-private/`、`.venv/`、真实 outputs、Cookie 和本地 Profile 都不能提交仓库。

## Local / Server 模式

默认：

```text
RIPPLE_DEPLOYMENT_MODE=local
```

Local 模式只适合本机回环访问，默认本机用户免登录。

Server 模式至少配置：

```text
RIPPLE_DEPLOYMENT_MODE=server
RIPPLE_PUBLIC_ORIGIN=https://ripple.example.com
RIPPLE_TRUSTED_HOSTS=ripple.example.com
RIPPLE_BOOTSTRAP_CODE=<至少 24 个字符的高熵一次性值>
```

要求：

1. 用受信 HTTPS 反向代理；不要把开发 Uvicorn 直接裸露公网。
2. 首次创建 Owner 必须同时提交部署者预先放进环境变量的初始化码。
3. Owner 创建完成后，从运行环境移除 `RIPPLE_BOOTSTRAP_CODE` 并重启。
4. Server 模式使用 HttpOnly + Secure Session Cookie、CSRF 校验、登录限速和角色边界。
5. 安装运行时、Agent Profile 修改、平台连接配置等控制面操作只允许 Owner/Admin。
6. 当前只有一个默认 Workspace。它提供访问边界，但还不是完整 SaaS 多租户实现。

## Agent

设置页检测本机已有的 OpenCode、Claude Code、Codex 和 Hermes。Ripple 不复制用户的全局 Agent 配置，也不会在安装阶段改写它们。

Agent 适配采用**检测后按需供应**：

1. `setup.ps1` / `setup.sh` 不预装任何第三方 ACP。
2. 只有当前服务进程确实检测到本机 Agent，设置页才显示该 Runtime 的适配选项。
3. 外部适配器的包名、精确版本、完整性摘要、入口与来源固定在 `ripple/agent_adapters.json`；浏览器不能提交 npm 包名、命令、URL 或安装路径。
4. 用户明确点击后，适配器才下载到 Workspace 私有目录；默认路径是 `.ripple-private/outputs/agent-adapters/`。安装完成还必须通过 ACP 初始化握手，才会标为可用。
5. Ripple 只卸载带有效 receipt 的受管目录，不会删除全局 Agent、外部 ACP、登录态或用户配置。
6. OpenCode 使用 Ripple 内置适配，无需下载 ACP；Claude ACP 与 Codex ACP 都按用户选择安装。Codex ACP 运行时使用隔离 `CODEX_HOME`，不继承全局 MCP/插件，并在启用前执行模型工具面探针。新版 Hermes 自带原生 ACP，Ripple 通过 restricted shim 复用它，只开放当前会话的 Ripple MCP，不下载安装第二份 Hermes。

通用控制面接口为：

```text
GET  /api/agent/runtimes
POST /api/agent/runtimes/{runtime_id}/adapters/{adapter_id}/actions
```

动作仅接受服务端当前状态允许的 `install`、`verify`、`repair`、`uninstall`。Server 模式下该接口只允许 Owner/Admin，并要求 Session 与 CSRF 校验。

Agent 获得业务能力时通过受控 Skill / MCP / Tool 租约。租约有能力白名单和过期时间，不提供任意 shell/file/network 权限。

Python 包、CLI、Skill 路径和公开发行元信息统一使用 Ripple 命名。OpenCode、Claude Code、Codex、Hermes 仅作为可插拔第三方 Agent Runtime 集成出现。

## 内容与发布

主流程：

```text
Mother content
→ 平台版本
→ 选择账号 / Blog
→ 保存版本
→ 预检
→ 审核
→ 执行或排期
→ 查询 / 回执核对
```

未知远端结果进入待核对状态，禁止盲目重发。微信公众号草稿写入与公开发布是不同动作；Blog Token 只绑定原 OpenAPI Origin，修改 Origin 时必须重新输入 Token。

## 测试

Python：

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

前端：

```bash
cd web/frontend
npm ci
npm run build
npm run lint
npm audit
```

小红书分析 Skill 自带 Node 工具链时：

```bash
cd skills/ripple/skill-xhs-analyzer
npm ci --ignore-scripts
npm run build
npm audit
```

通用浏览器 smoke：

```bash
python scripts/ripple_smoke.py
```

测试必须使用隔离目录、Mock HTTP 或本地替身，不允许使用真实账号执行公开发布/评论/删除。

## 依赖与供应链

- Python 直接依赖定义在 `pyproject.toml`；`requirements-ripple.lock.txt` 是已验证环境快照。
- 前端和独立 Node Skill 各自提交 package-lock。
- 发布前运行 `npm audit`，并建议额外运行 `pip-audit` 和密钥扫描器。
- 第三方 Skill / 参考来源必须在 `THIRD_PARTY_NOTICES.md` 和 `LICENSES/` 中保留来源及适用许可。

## 发布前

执行 `docs/ripple/RELEASE_CHECKLIST.md`。Git 历史中的旧演示媒体是否保留，需要作为单独的发行历史决策处理，不能在普通代码提交中静默重写。