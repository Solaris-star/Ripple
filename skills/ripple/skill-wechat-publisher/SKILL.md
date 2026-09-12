---
name: skill-wechat-publisher
description: |
  微信公众号文章创作、排版与独立 CLI 草稿发布流程：根据用户素材撰写/改写公众号文章，按需核验资料、配图和排版；真实草稿 API 仅在独立 CLI 环境使用。
  当用户明确要制作/修改公众号文章、做公众号排版，或在独立 CLI 环境操作公众号草稿时使用；仅咨询公众号能力/规则时只提供说明，不启动完整创作发布流程。
layer: publish
---

# 微信公众号文章创作与草稿流程

> **当前 Ripple 边界**：托管 Agent 没有微信公众号真实发布工具，不能把本 Skill 的 CLI/API 命令当作托管发布能力。托管对话可完成文章草稿、排版建议和发布任务准备；真实公众号草稿 API 步骤仅供独立 CLI/维护环境，且不代表 Ripple 工作台已接入该适配器。

完整任务可覆盖素材整理、撰写、按需事实核验、配图和排版；托管 Agent 到文章/任务准备为止，独立 CLI 才进入草稿 API 步骤。局部改稿只执行相关阶段，不为了“完整流程”补跑搜索、配图或发布。

## 独立 CLI 能力与依赖（运行时按需检查）

| 能力 | 状态 | 依赖 |
|------|------|------|
| 公众号发草稿（`publish.py` → 官方 HTTP API） | ✅ 可跑 | `wechat-publisher.yaml` 里的 `app_id` / `app_secret` + IP 白名单 |
| 反 AI 检测（`ai_score.py`） | ✅ 可跑 | 纯本地，无外部依赖 |
| MD→公众号排版（`html_converter.py`） | ✅ 可跑 | 纯本地 |
| 生成配图（`generate_image.py`） | 运行时检查 | 仅在独立 CLI 具备对应图片 Provider 时使用；Ripple 托管媒体配置不由本脚本读取 |
| 多平台同步（`multi_publish.py`，阶段七） | 运行时检查 | 需独立浏览器/扩展环境；托管 Agent 默认不执行 |

核心链路（写作→排版→反 AI 检测→发草稿）在配好 `app_id`/`app_secret` 后可跑；配图缺 key 时改用外部生图或跳过，多平台同步默认不启用。

## 账号与人格

账号、作者、主题和 voice 来自用户当前选择的 Ripple Profile/账号，或独立 CLI 的本地配置。仓库示例账号只用于示范字段结构，不能自动当作用户账号，也不能按主题擅自选择固定人格。没有持久 Profile 时可使用用户本次给出的语气/参考稿完成文章，不强制先建画像。

## 前置条件

```bash
cp wechat-publisher.yaml.example wechat-publisher.yaml   # 填 app_id / app_secret / author / theme
python3 skills/ripple/skill-wechat-publisher/scripts/wechat_api.py list-accounts
# 验证 API 连接
cd skills/ripple/skill-wechat-publisher/scripts && python3 -c "from wechat_api import get_access_token; print('OK:', get_access_token()[:10])"
pip install requests pyyaml --break-system-packages 2>/dev/null || pip install requests pyyaml
```

配置文件固定放 skill 根目录（`config.py::_find_unified_yaml()` 只查此处），账号下必须有 `app_id` 和 `app_secret`。API 参数细节见 [references/api_reference.md](references/api_reference.md)，错误码见 [references/errors.md](references/errors.md)。

## 完整工作流程（7 阶段，第 7 为可选 opt-in）

### 阶段一：理解需求与收集素材
明确主题、受众、证据、用户已有素材和本次语气。具体人名/时间/金额/版本/经历只能来自用户材料或可靠来源，不能为了“真人味”编造。账号从当前选择读取；只在本次任务需要文件式交接时产出 `brief.md`。

### 阶段二：资料核验（需要新鲜事实/数据时）
时效资讯、产品版本、市场数据或用户明确要求调研时才进行外部检索，优先权威来源并按风险做交叉核验；纯创意写作、已有材料改写或局部编辑不自动“全网搜索”。只有任务需要文件式留档时才写 `research.md`。

### 阶段三：撰写文章
根据任务复杂度选合适结构；新建长文或结构不明确时按需读取 [references/article-structures.md](references/article-structures.md)，已有文章局部修改不重新选结构。行内标色属于排版选择，只有目标主题/模板需要时才使用，不把“避免某几个词”当作写作本身的完成标准。

### 阶段 3.5：自然度检查（按需）
用户明确要求“去 AI 感/更自然”，或初稿出现明显模板套话时按 [references/anti-ai-checklist.md](references/anti-ai-checklist.md) 做证据式检查。没有固定第一人称、标点、句长或具体数字配额；不能为了“真人味”编经历/数字。已有 Profile/参考稿时以真实 voice 为准，同一版本已检查过且相关段落未变化时不重复全篇扫描。

### 阶段四：配图（任务需要时）
根据文章信息量、用户素材和当前可用工具决定是否配图及数量；不强制每篇 6-10 张。需要精确文字时优先确定性排版卡；需要插画/信息图时按需读取 image-style references。Ripple 托管 Agent 只使用当前受控媒体工具；独立 CLI 的 `generate_image.py` 只在其运行环境实际配置可用时调用。

### 阶段五：格式转换与排版
```bash
python3 skills/ripple/skill-wechat-publisher/scripts/html_converter.py article.md --theme refined-blue -o article.html
python3 skills/ripple/skill-wechat-publisher/scripts/html_converter.py article.md --list-themes
```
主题一般由 `publish.py --account` 自动从 yaml 读。15 套主题的推荐表与视觉简介见 [references/themes.md](references/themes.md)，行内标色系统见 [references/inline-markup.md](references/inline-markup.md)。主题预览：`assets/theme-previews/index.html`。

### 阶段 5.5：独立 CLI 的现有 `ai_score` 检查
当前 `publish.py` 实现仍会在发草稿前调用 `ai_score.check_ai_score()`；这是**现有业务代码约束**，本轮规则调整不绕过也不把该分数提升为通用内容质量标准。需要诊断时可手动运行：
```bash
python3 skills/ripple/skill-wechat-publisher/scripts/ai_score.py outputs/主题名/article.md --threshold 45
```
命中时只修复有具体文本证据的模板套话，并保持事实和账号 voice。若内容已经满足用户要求、只因主观分数仍被脚本拦截，报告为实现层限制，不做无界重写；是否调整 `publish.py` 门禁另案处理。第三方 AI 检测器只有用户明确要求时才调用，不作为默认双保险。

### 阶段六：发布到草稿箱（不自动群发）
```bash
python3 skills/ripple/skill-wechat-publisher/scripts/publish.py --account <已选账号> \
  --input outputs/主题名/article.md \
  --cover .../cover.jpg --title "标题" --digest "120 字以内摘要" --exec
```
独立 CLI 先 dry-run 预览账号、标题、摘要和素材；当前版本未经确认时才获取一次授权，已确认同一版本不重复询问。`publish.py` 真执行时仍负责出站敏感信息检查、排版、素材上传和建草稿。Ripple 托管 Agent 不直接运行该命令。

### 阶段七：多平台同步（可选；仅在独立运行时具备对应工具时）
一键同步到知乎/掘金/CSDN/头条（均存草稿）。基于 Wechatsync Chrome 扩展 + `@wechatsync/cli`，需浏览器。触发方式与失败处理见 [references/multi-platform-sync.md](references/multi-platform-sync.md)。

## 贴图模式（newspic，与文章模式并列）
对标公众号“图片消息”：卡片墙 + 短描述。完整独立 CLI 流程见 [references/newspic-mode.md](references/newspic-mode.md)；卡片数和短文本长度根据内容/平台当前约束选择，不为了固定范围凑内容。现有 `ai_score` 属脚本实现约束，按上面的处理原则解释。

## 文件组织约定
所有产物放项目内 `outputs/主题名/`：最终稿和封面在目录顶层，研究稿、调试 HTML、配图等中间文件放 `assets/`。不要把临时产物写到 Skill 根目录。

## 脚本说明

| 脚本 | 用途 |
|---|---|
| `publish.py` | 完整发布（一键，含 AI 味 gate），支持 `--type news\|newspic` |
| `generate_image.py` | 统一生图入口（需图像 API key） |
| `newspic_build.py` | 贴图拆卡器（brief.md → card_plan.json） |
| `wechat_api.py` | facade —— 重导出下述模块 + CLI |
| `config.py` | （内部）yaml + 配图风格加载 / `set_account` / `get_config` |
| `wechat_token.py` | （内部）`get_access_token`，本地缓存 |
| `api.py` | （内部）图片上传 / 草稿 / 发布 |
| `html_converter.py` | Markdown → 微信 HTML（多主题 + 行内标色） |
| `image_handler.py` | 图片下载 / 上传 / 替换 |
| `ai_score.py` | 反 AI 检测自检（`--mode news\|newspic`） |
| `multi_publish.py` | 多平台同步（阶段七，默认不启用，需浏览器） |

## Profile 感知
有 Ripple Profile 时按需使用账号定位/受众/语气；无 Profile 时使用用户本次语气说明、参考稿或中性编辑风格。独立 CLI 配置里的示例 voice 只在用户明确选中对应账号配置时生效，不作为无 Profile 的通用人格。

## 注意事项
- 独立 CLI 只建公众号草稿，不自动群发；账号必须来自用户当前选择/本地配置，不默认把仓库示例账号当用户账号。
- 独立 CLI 的 `--exec` 属外部写入，执行前确认当前标题、摘要、账号和素材；同一版本已经确认就不重复询问。Ripple 托管 Agent 不添加 `--exec`，真实动作交发布工作台或独立 CLI。
- Markdown、HTML 与贴图三条入口都会在上传前扫描密钥、内部地址和路径，命中时必须改稿，不得绕过。
- 多账号存在时保持各自真实 voice/theme；不要为了通过“反 AI”规则人工制造固定人格差异。
- 错误码（40164 IP 白名单 / 40001 token / 48001 未授权等）见 [references/errors.md](references/errors.md)。
