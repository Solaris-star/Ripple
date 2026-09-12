---
name: xhs-note-creator
description: >
  小红书内容总入口：生成标题、正文、caption、hashtags，以及 3-9 张图文卡片或短视频分镜，覆盖素材分析、卖点评估、去 AI 味和质检。
  当用户说“写/做小红书笔记、小红书图文/种草/文案、出一套卡片、小红书视频”时使用。
  整套笔记用本 SKILL；仅渲染卡片用 card-xiaohongshu；其他平台的通用文案用 social-content。
layer: produce
---

# 小红书笔记创作

把一个主题 + 可选素材，生成**图文卡片组**或**短视频分镜脚本**，按规范输出到 `outputs/`。

## 核心理念

小红书读者看的不是长文，是**卡片组**或**短视频**。长文原稿只是中间产物。

- **图文帖**：3-9 张 3:4 竖版卡片（cover + content × N + ending），每张 ≤80 字
- **视频帖**：15-90 秒竖屏视频分镜 + 封面卡

### 爆款 5 大原则

1. **真实素材优先**：截图、对比图 > 纯 AI 生图
2. **聚焦核心卖点**：用公式判断优先级（稀缺性 × 实用性 × 可感知）
3. **高级感视觉（走 card-design，别做糙卡）**：卡片视觉**统一走 [card-design](../card-design/SKILL.md) 设计系统**（选风格锁 spec、字大字细、填满画幅、禁大 emoji 廉价感）。文案要口语化、真实感，但**视觉不能糙**——想要「素人 / 手账」质感就选 card-design 的**手账贴纸 / 奶油温柔**风格（仍是高质量 HTML 渲染，不是糙 t2i 大 emoji）。
4. **痛点导向**："能解决什么问题" > 堆砌功能列表
5. **有反馈再迭代**：先完成满足任务标准的可用版本；用户提出偏好或代表性预览暴露问题时再定向修改，不为了固定“60→70 分”流程制造额外轮次。

详细方法论：`references/xiaohongshu-viral-methodology.md`

## 工作流

按用户实际目标执行到需要的阶段。完整“从主题到整套笔记”走全流程；只改标题、caption、单张卡片或已有稿件时直接进入对应步骤，不补跑无关阶段。

### Step 0 — Intake（只补关键缺口）

先复用用户消息、当前项目、已有素材和 Profile 中已经明确的信息。主题/目标读者/核心观点等只有缺失且会明显改变结果时才询问；输出形态未指定可默认图文并说明；卡片数/视频时长可根据内容量给出可调整默认。视觉风格在真正创建/改变卡片设计时按 card-design 处理，不在入口重复做选择。

### Step 0.5 — 卖点/亮点分析（推广/分享/测评类必做）

1. 列出与用户目标相关的功能/特性/亮点。
2. 根据现有证据评估稀缺性、实用性和可感知性；只有这些判断依赖用户商业信息且无法推断时才询问。
3. 选择 1-2 个最值得表达的卖点，并说明依据；不要求用户完成一轮固定量表才能继续。

### Step 1 — 素材清点（有素材才做）

如果用户提供了素材，先用脚本生成清单：

```bash
python3 skills/ripple/xhs-note-creator/scripts/analyze_material.py <path>... \
  --out <work-dir>/reference/materials.json \
  --frames-dir <work-dir>/reference/frames
```

素材价值排序：对比图 > 功能演示 > 数据图表 > 品牌素材

### Step 2 — 采集外部参考（需要新鲜事实/数据时）

资讯、时效事实、竞品数据或用户明确要求调研时按 `references/reference-search.md` 取证；纯创意改写、已有素材重组、局部编辑不自动联网。关键事实按风险选可靠来源核验，不机械要求每个观点都凑两个来源。

如果用户给的是**小红书参考笔记/链接并要求参考其打法创作**，按 `references/reference-to-original.md`：优先用 Ripple 读取笔记与必要评论，只提炼可迁移的 Hook/结构/素材角色/互动机制，再换入用户自己的事实、案例和素材。不要复制参考作者的个人经历、独特措辞或原图；用户只要拆解时不自动继续创作。

### Step 3 — 长稿中间层（复杂主题按需）

复杂知识/测评需要先展开论证时可写长稿；短种草、已有稿件、卡片改写可直接进入拆卡。只有用户明确要求中间审稿，或下一步将启动新的高成本批量制作且关键观点仍未定时，才在此暂停确认。

### Step 4 — 自然度/文风检查（按需）

去 AI 感规则统一走 text-polisher 权威源，不在本 SKILL 维护副本：

- 中文规则（含小红书素人感/闺蜜语气/emoji 节奏特化）→ `../text-polisher/references/zh-ai-markers.md`
- 通用填充短语 → `../text-polisher/references/phrases-to-remove.md`
- 公式化结构 → `../text-polisher/references/structures-to-avoid.md`

根据用户要求和文本问题选择相关规则；不设置固定自评分数门，不为“活人感”编造第一人称经历或数据。

### Step 5 — 分发：图文 or 视频

#### 5A. 图文帖：拆成 3-9 张卡片

- cover（第 1 张）+ content（中间）+ ending（最后 1 张）
- 每张 ≤80 字
- 一张卡只讲一个论点
- 全套配色/字体/风格保持一致（由 card-design 选中的那一套贯穿整组）

**视觉路径按信息类型选择。** 精确文字、数据和版式卡优先用 card-design 的确定性渲染；真实照片优先保留真实素材；插画、场景图和视觉概念在合适时可使用 Settings 中已配置的图片模型。不要因为某种工具类别本身判定“高级/廉价”。策略写入 `meta.json`，枚举与 `references/meta-schema.md` 保持一致。

| 策略 | 适用 | 渲染路径 |
|------|------|----------|
| `html_card` | 纯文字卡 / 数据卡 / 需要精确排版的概念卡 | **走 [card-xiaohongshu](../card-xiaohongshu/SKILL.md)**：复用已有 spec 或按需确定风格 → HTML → `render_card.py` → 对受影响卡片做客观渲染检查 |
| `text_on_photo` | 有真实照片 + 钩子文字（真实素材最佳） | Pillow `scripts/text_on_image.py`（小红书真实素材路径，保留） |
| `collage` | 有 2-4 张互补照片 | Pillow `scripts/collage_3x4.py` |
| `generated_visual` | 插画、氛围场景、视觉隐喻等不依赖精确图中文字的画面 | 使用 Ripple Settings 默认图片 Provider/Model；生成后按任务检查构图、主体和事实风险 |

> 需要精确文字的卡片不要依赖生成模型正确绘字；纯视觉/插画型内容可以使用生成模型。一个笔记可以混合真实照片、确定性排版卡和生成视觉，只要整套视觉逻辑一致。

#### 5B. 视频帖：写分镜脚本

- 6-12 个分镜，每镜 2-8 秒
- 每镜含：narration（≤30 字）、on_screen_text（≤15 字）、visual、material_ref
- 出一张 cover 卡作为封面

### Step 6 — caption + hashtags + 标题

**标题**：从公共源 `skills/shared/references/hook-title-formulas.md` 选标题公式（痛点+方案 / 提问式 / 发现式 / 热点词 / 身份共鸣等），产 2-3 个备选。

**caption**：100-300 字，hook 开头 → 关键信息 → CTA，闺蜜语气 + 少量 emoji（点缀节奏，不当图标）

**hashtags**：5-8 个，4 核心（热点词 + 核心功能 + 差异化 + 目标人群）+ 4 辅助（场景 + 品类）

### Step 7 — 落盘

```
outputs/主题名/
├── note.md              # 最终笔记/长稿（按任务需要）
├── meta.json            # 元数据（卡片/分镜/caption/hashtags）
├── card_*.png / cover.* # 最终成品放项目根
└── assets/              # materials.json / 搜索结果 / HTML / 中间素材
```

目录名走脚本标准化：`python3 skills/ripple/xhs-note-creator/scripts/normalize_slug.py "标题" --with-ts`

### Step 8 — 校验（强制，两道门）

**① 文本/结构门**（标题/caption/hashtag/卡片字数与结构）：

```bash
python3 skills/ripple/xhs-note-creator/scripts/validate_meta.py outputs/主题名/meta.json
```

**② 视觉门**（仅 `html_card` 路径，渲染后跑；死空白/密度硬门禁）：

```bash
python skills/ripple/card-design/scripts/card_audit.py audit -f "outputs/主题名/card_*.png"
```

校验失败先区分客观结构错误、工具/环境错误和设计建议。标题/字段缺失、文件损坏、文字溢出等客观缺陷修复后重验受影响项；密度/风格类建议不要求循环到全 PASS。工具失败不通过改文案来“修”。

## 不要做的事

- 不写 H1；标题只放 `meta.json.title`
- 不在正文末尾写参考来源
- 不在 `.md` 里留 `【插入图片】` 占位符
- 不跳过与本次交付相关的客观校验；Step 4 仅在用户要求自然化或文本确有公式化问题时执行
- 不手算目录名，一律走 `normalize_slug.py`

## Profile 感知

- **有 Profile**：读取赛道、目标受众、内容风格，对齐笔记语气和标签策略
- **无 Profile**：使用用户本次给出的受众/语气/参考材料；没有关键歧义时采用中性、自然的小红书表达，不为了建立画像先停工

## 工具依赖

- Python 3.8+、Pillow（必需，真实照片卡路径）
- playwright + chromium（`html_card` 路径渲染卡片，走 card-xiaohongshu；首次 `pip install playwright && playwright install chromium`）

## 参考资料

- **卡片视觉** → [card-design](../card-design/SKILL.md)（新建/改变视觉方案时读取；已有 spec 的局部修改复用）+ [card-xiaohongshu](../card-xiaohongshu/SKILL.md)（HTML→截图渲染管线）
- `references/xiaohongshu-viral-methodology.md` — 文案/卖点/标题/情绪方法论（视觉部分已交 card-design）
- 去 AI 化 → `../text-polisher/references/zh-ai-markers.md`（权威源，含小红书特化）
- 钩子/标题公式 → `skills/shared/references/hook-title-formulas.md`（公共源）
- `references/material-intake.md` — 素材处理流程
- `references/image-sourcing.md` — 图片来源处理
- `references/meta-schema.md` — 元数据字段定义
- `references/output-spec.md` — 输出目录规范
