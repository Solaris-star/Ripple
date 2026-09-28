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

本机 Agent 工具连接凭据加密保存在 Workspace 私有目录的 `agent-tool-auth.json`，服务重启时复用，避免仍在运行的 OpenCode 持有旧凭据而导致所有工具返回 403。MCP 的会话租约仍按原规则过期。首次从旧版升级时，需要同时重启 Ripple 与它管理的 OpenCode 进程；不要删除正在使用的凭据文件。

OpenCode 的状态检查和进程复用会核对启动时的工具连接标记。该标记由私有目录、工具凭据和回调地址的摘要生成，通过进程环境变量解析到配置中；不包含明文凭据。旧进程缺少标记或连接不匹配时显示“工具连接已过期”，不会仅凭进程存活显示已连接，也不会自动终止来源不明的进程。

Python 包、CLI、Skill 路径和公开发行元信息统一使用 Ripple 命名。OpenCode、Claude Code、Codex、Hermes 仅作为可插拔第三方 Agent Runtime 集成出现。

## 内容与发布

主流程：

```text
选题 → 手动开始写稿，或确认策划后生成图文草稿
→ 编辑与图片预览（沿用当前账号）
→ 准备发布（自动保存、字段检查、复用任务、预检）
→ 核对完整内容，确认并发布或确认定时发布
→ 自动检查结果（最多 3 次），必要时手动核对
```

未知远端结果进入待核对状态，禁止盲目重发。微信公众号草稿写入与公开发布是不同动作；Blog Token 只绑定原 OpenAPI Origin，修改 Origin 时必须重新输入 Token。

渠道能力中的 `adapter_available` 表示账号适配能力，`publish_available` 单独表示当前安装是否包含发布模块，`direct_publish` 还要求账号和执行环境可用。抖音账号连接与只读功能可以继续使用；缺少 `douyin_publish.py` 时，界面明确显示发布尚未接入，预检和执行入口均阻止提交。

手动灵感继承当前目标平台，保存后可直接开始写稿，无需配置 AI 或确认策划。再次打开会复用关联稿件；单平台选题自动创建对应平台稿并沿用当前所选账号。选题与平台版本共用服务端能力清单，未接入平台提前显示限制。AI 协作可选择通用草稿或当前平台稿；当前平台正文改写先展示差异，应用时检查来源版本，不覆盖通用草稿或后续手工修改。发布详情和平台选择通过 URL 记录位置，刷新与浏览器后退可恢复原位置。

同一主稿及其平台稿可以复用已经保存的素材。多平台发布总览支持统一预检，逐个平台核对完整内容并勾选确认，再执行所选任务；每个平台独立保留失败原因与结果。预检失败进入“需要处理”，接口确认与人工确认使用不同标签。X 发布与回复共用 280 加权字符计数。B 站投稿必须明确填写分区 ID、原创或转载类型，转载还需来源；这些参数进入审核快照与上传命令。Blog 可在创建版本时选择已连接目标，本地导出使用独立的检查及确认文案。

通用草稿和平台版本的未保存修改会在离开时提示。窄窗口通过“编辑与预览 / AI 协作 / 全部内容”切换，切换视图不卸载编辑器。AI 返回时按字段保留等待期间的手工编辑；图片生成状态通过 `media` 事件和恢复快照传递，缺少有效产物不能显示为成功。重试图片必须由用户点击。

发布任务仍保存审核时的内容快照。原任务未核对结果或仍在定时队列时，修订平台版本不能绕过检查创建另一条发布任务；执行按钮只允许发送当前已保存、已审核的版本。

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
