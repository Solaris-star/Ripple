# Ripple SKILL 接口规范 v0.3

> 所有 Ripple SKILL 遵循此规范。SKILL 是指导 Agent 完成工作的独立能力说明。

## 目录结构

```
skills/
├── ripple/                Ripple 五层 Skill 主库（发现/策划/制作/发布/归因）
└── shared/                跨 Skill 共享工具（脚本、配置、依赖）
```

每个 SKILL 是一个独立目录：

```
skill-xxx/
├── SKILL.md               必须 — 执行流程（精简，< 200 行）
├── references/            可选 — 领域知识（按需加载，不常驻 prompt）
│   └── *.md
├── scripts/               可选 — 可执行脚本（运行时调用，代码不进 prompt）
│   └── *.py / *.sh
└── tests/                 可选 — 测试用例
    ├── test1.prompt           输入
    └── test1.expected         期望输出（关键字匹配）
```

### 三层加载机制

| 层 | 内容 | 加载时机 | token 开销 |
|---|---|---|---|
| Metadata | frontmatter（name, description） | 常驻，用于 SKILL 路由 | 极小 |
| Instructions | SKILL.md 主体 | SKILL 被触发时 | 中等 |
| Resources | references/ + scripts/ | SKILL 执行中按需读取 | 按需 |

**核心原则：SKILL.md 只写"怎么做"，领域知识写在 references/ 里。**

### 任务触发、阅读与完成标准

- **SKILL 被加载不等于执行授权**：咨询、审查、解释、比较只读取必要规则；创作、修改、外部动作分别进入对应分支。
- **资料按需读取**：references 必须写清触发条件；同一任务已读取且内容未变化时复用，不因每次追问或局部修改重新通读。
- **确认按副作用触发**：真实发布、删除、不可逆账号操作、新的付费范围或改变数据发送目的地需要确认；同一任务已确认的范围不重复询问。低风险、可逆草稿/预览允许使用合理默认并说明。
- **验证按风险和改动范围触发**：客观结构/规格使用确定性校验；只改局部就验证局部及受影响共享模板。主观评分、审美建议默认是 warning，不作为通用硬门。
- **完成状态明确**：至少区分完成、部分完成、阻塞、结果待核实。不得把阻塞冒充完成，也不得因为非阻塞建议未处理就无限返工。
- **失败先分类**：环境/权限/网络失败不要求改写内容；产物缺陷修复后只重验受影响项目；付费重试受已授权预算约束。

### 共享工具层

`skills/shared/` 存放多个 SKILL 共用的工具脚本和配置（如 ffmpeg 封装、API client、通用模板）。SKILL 通过相对路径引用。

## SKILL.md 格式

```markdown
---
name: skill-xxx
description: >-
  用中文说明本 SKILL 做什么、用户在什么场景或用哪些说法时应触发，以及与相邻 SKILL 的边界。
layer: discover / plan / produce / publish / attribute / general
---

# SKILL 名称

> 一句话描述

## 输入

描述接受什么输入

## 输出

描述输出格式（字段说明，不写具体值）

## 执行步骤

1. 步骤（引用 references/ 下的文件获取领域知识）
2. ...

## Profile 感知

有 Profile 时怎么用，没有时怎么退
```

**frontmatter 规则：**
- 只保留 `name`、`description`、`layer` 三个常规字段，减少常驻路由上下文和无效元数据
- `description` 是 Agent 的主要触发依据，必须用中文同时写清能力、触发场景/用户说法和相邻 SKILL 边界；可使用 YAML 块标量
- `layer` 标明所属层：五个流水线层 `discover / plan / produce / publish / attribute`，外加 `general`（跨切面基础设施，如画像管理、产物管理、模板库——不属于任一流水线阶段）
- 不在 frontmatter 写 Runtime 专属清单；运行环境、二进制和权限检测由 Ripple Runtime/Capability 层负责
- 禁止 `version`、`profile_aware`、`self_developed`、`metadata`、`allowed-tools`、`tags`；执行约束和 Profile 行为写正文
- SKILL.md 主体控制在 200 行以内

全库校验：

```bash
python scripts/validate_skills.py
python scripts/validate_skill_commands.py
```

第一条检查 frontmatter、资源链接、输出和发布安全契约；第二条解析 Skill 中的 Python 命令，对照脚本的 argparse 定义检查路径与参数漂移。

## 调用方式

Ripple Web / Agent 根据用户请求选择 Skill。托管 Agent 只通过当前 Runtime 明确暴露的 `ripple_*` 工具执行动作；Skill 不扩展权限。Skill 中保留的 CLI 命令仅供独立维护场景，在该环境明确具备相应能力时使用。

## Profile 注入

Profile 由 Ripple 后端按当前会话绑定读取并注入必要维度；不同 Agent Runtime 不自行维护另一套 Profile namespace。

## 产物管理

**目录布局规约**（一个产物目录 = `outputs/<主题>/`；产品层归属以 Mother content 为准）：
```
outputs/<主题>/
├── note.md / final.mp4 / card_1.png   成品（用户要发/读的最终文件，放目录根）
├── assets/                            中间件：frames/ clips/ 构建脚本 原始素材 草稿 重复文件
└── .ripple.json                    唯一元数据：展示头 + 层间产物契约（隐藏）
```
- **成品放目录根、中间件进 `assets/`**：前端「素材与成品」据此把成品与素材分区展示。
- 目录名用人类可读主题（中文可），禁泛名（xhs/test）；测试/临时产物写 `outputs/_scratch/`。
- 从内容工作台的既有 Mother content 生成产物时，`.ripple.json` 应写入 `content_id` 与生成时的 `source_version_id`。前端按 Mother content 归档；无法可靠关联的旧目录进入「历史产物 / 未关联内容」，不会按标题猜测归属。
- 任何新脚本在创建产物前必须调用 `skills/shared/scripts/output_paths.py` 的 `validate_output_path()`；系统写入需显式传 `allow_system=True`，且只能使用已注册的 `_` 路径。
- 系统状态一律 `_` 前缀目录（`_login/_publish/_analytics/_profile_build/_scratch`）；
  **素材与成品只展示内容关联目录和历史未关联产物**，忽略根目录散文件与系统目录。

### 元数据契约（`.ripple.json`）

单一元数据文件（隐藏），由 `skills/shared/scripts/manifest.py` 读写（带 selftest），含两部分：

**① 展示头**（供前端「素材与成品」归档展示：标题/平台/状态/封面/标签 + 成品高亮）。收尾登记：
```bash
python skills/shared/scripts/manifest.py meta --topic <主题> \
  --title "<人类可读标题>" --platform 小红书 --kind cards --status draft \
  --tags "标签1,标签2" --cover cover.png --deliverables card_1.png,card_2.png \
  --content-id <Mother-UUID> --source-version-id <Mother-version-id>
```
`kind` 取 `article/xhs-note/video/cards/poster/audio/other`；`status` 取 `draft/ready/published`。
`meta` 为 upsert：只改传入字段、其余保留；缺省有兜底（title→topic、cover→首张成品媒体）。

**② 层间产物契约 steps[]**：纵向编排跨层时，上游产物路径与关键结论通过 manifest 结构化传递，下游无需重新推导。
```bash
# 上游每步产出后登记（失败也登记 --status failed，供断点续跑）
python skills/shared/scripts/manifest.py record --topic <主题> \
  --layer plan --skill video-script --profile <画像> \
  --outputs script.md,brief.md --summary "3 幕结构，钩子在前 3s"

# 下游步骤前取上游最近一步作为输入
python skills/shared/scripts/manifest.py latest --topic <主题> [--layer plan]
python skills/shared/scripts/manifest.py read   --topic <主题>   # 看全链路
```

Schema：`{topic, profile, created, updated, title, summary, platform, kind, status, tags[], cover, deliverables[], content_id?, source_version_id?, steps:[{layer, skill, at, status, outputs[], upstream[], summary}]}`。
`layer` 取 `discover/plan/produce/publish/attribute/general`；step `status` 取 `done/failed`（默认 done）。契约稳定、可被任一层消费。

> 存量目录用 `scripts/migrate_outputs.py`（dry-run→apply 回填展示头，`--reorganize` 归整中间件进 assets/）收敛；测试残渣用 `scripts/cleanup_outputs.sh` 清理。

## 出站内容安全闸门（发布/评论类 SKILL 契约）

任何把文本**发到公开平台**的脚本（xhs/douyin/web_publisher/xhs_comment/zhihu_answer 等），在真发（`--exec`）前**必须**过 `skills/shared/scripts/content_guard.py` 的 `guard_or_die(...)`。**分两级**（见 `BLOCK_CATEGORIES`）：**BLOCK 级**=真·敏感信息（API key、内部 URL/域名、代理 IP、内部路径、env 名 + `.env` 真值）→ **fail-closed 退出码 7 阻止发布**；**WARN 级**=AI 措辞（由 AI 生成/Claude/system prompt/大模型）与模型名（claude-*/gpt-image-2）→ 论文解读、AI 科普里可能是正常内容，**只提醒不拦截**。dry-run 全部只告警。放行硬拦须显式 `--allow-unsafe`。新增发布类脚本照此接入。

**有界编排约定**：manifest 只当**薄索引**（`summary` 一行给编排层路由 + `outputs[]` 指路径），**不复制内容**。跨层要传的东西分两类，都落 `outputs/<主题>/` 成文件，不留在对话里：
- **产物（载荷）**：脚本/图/视频/文案 → 写文件，`--outputs` 指过去，下游按路径读全文。
- **决策/意图**（基调、受众、钩子、do/don't 等不体现在产物里的）→ 写进 `outputs/<主题>/brief.md`（策划层的创作简报），同样列入 `--outputs`。

## 测试

SKILL 可带 `tests/` 目录，用低成本模型验证基本功能：
- `test1.prompt` — 测试输入
- `test1.expected` — 期望输出关键字/pattern

## 设计约束

1. **边界清楚** — SKILL 可独立理解；需要组合其他 SKILL 时显式说明触发条件和交接内容，不把组合调用变成每次必跑。
2. **输入按任务需要** — Profile、平台、样本等只在确实影响该能力时读取；缺少持久 Profile 不应阻止可由任务内参考标准完成的分支。
3. **接口一致** — 输出格式稳定，可被下游消费；完成/部分完成/阻塞状态可区分。
4. **SKILL.md 精简** — 执行流程在主文件，领域知识放 references/；references 按需加载。
5. **验证有界** — 硬门只用于安全、权限、不可逆副作用和可客观验证的关键规格；主观质量用证据和建议表达。
6. **泛化** — 定义规则和模式，避免把单次环境快照、固定账号或演示参数写成长期默认。
