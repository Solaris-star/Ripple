# Ripple Prompt Stack

> Ripple Agent 的行为由 Ripple 系统边界、会话上下文和按需加载的 Skill 共同约束。第三方 Agent Runtime 只负责模型/会话执行，不改变 Ripple 的权限边界。

## 组合顺序

```text
Layer 1: Ripple system       产品能力、安全边界、发布确认规则
Layer 2: Session context     当前画像、Mother content、模型、Effort、Tool/MCP 租约
Layer 3: Skill guidance      当前任务匹配时加载 skills/ripple/<skill>/SKILL.md
Layer 4: References/scripts  当前步骤确有需要且运行时具备权限时按需使用
```

## Layer 1 — Ripple system

Web 后端生成固定的 Ripple Agent system guidance。它定义内容工作流、受控 `ripple_*` 工具、敏感信息边界和真实发布必须由用户确认等规则。

Skill 文本不能扩大权限；第三方 Runtime 的原生 Shell、文件、网络或网页登录能力不会因为 Skill 中写了命令而自动获得授权。

## Layer 2 — Session context

每个会话由 Ripple 保存并绑定：

- 当前 Agent Runtime / Agent Profile；
- 模型与 Effort；
- 当前账号画像；
- 当前 Mother content 的 `content_id` / `version_id`（若有）；
- 本轮允许的 Skill、Tool、MCP capability 和 Plugin。

画像不通过全局共享文件切换。后端直接读取 `profiles/<当前画像>/` 中当前任务需要的内容，并作为会话数据注入；不同会话不会通过一个全局 `MEMORY.md` 互相覆盖。

## Layer 3 — Skill

Skill 主库位于 `skills/ripple/`。只有当前请求匹配某个 Skill 时才加载其 `SKILL.md`。同一任务内规则未变化时可复用，不因每个追问机械重读。

Skill 主要提供业务方法、输入输出和质量边界。Ripple Capability Broker 根据当前 Runtime、Skill→Tool 绑定、readiness 与风险边界计算实际 Tool allowlist。

## Layer 4 — References / scripts

`references/` 只在当前步骤需要对应领域知识时读取；`scripts/` 只有在当前执行环境明确具备相应权限时才运行。托管 Agent 会话默认只使用 Broker 暴露的受控能力。

## Agent Runtime 集成

Ripple 当前可检测并接入 OpenCode、Claude Code、Codex 和 Hermes。它们是可插拔 Runtime，不是 Ripple 的仓库 namespace。

OpenCode 所需的 Ripple 指令和 Tool 模板保存在 `integrations/opencode/`。运行时会把这些受管文件同步到 Git 忽略的项目本地 `.opencode/`，不会要求仓库把第三方配置目录作为源码根目录提交。

Ripple 安装器不会覆盖用户已有 Agent 的全局配置。

## 设计原则

- Ripple 自己拥有业务状态、权限、发布确认与 Tool Broker。
- 第三方 Runtime 只承担其适合的模型/会话执行职责。
- Skill 按需加载，references 按步骤加载，避免无关上下文常驻。
- Profile、附件、热点和 Tool 返回都按不可信数据处理，不能覆盖系统安全边界。
- 真实发布、删除和不可逆账号操作必须保持显式用户确认。
