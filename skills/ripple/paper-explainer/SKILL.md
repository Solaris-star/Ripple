---
name: paper-explainer
description: >
  科研论文解读：解析 arXiv/PDF 的公式与图表，提炼问题、贡献、方法、关键图和结论，再产出 B站/视频号解读视频或知乎/公众号图文。
  当用户说“论文解读、讲论文、论文转视频/图文、科研科普、arXiv、学术视频”时使用。
  本 SKILL 从论文做内容；video-to-article 从视频做图文，doc-convert 只转换文档格式。
layer: produce
---

# 科研论文解读（论文 → 视频 / 图文）

> 把一篇论文讲成普通人/同行都爱看的视频号视频或图文。核心中间产物是一份
> **结构化 asset library**（一次解析+提炼，视频与图文两条产线共用，不重复调 LLM）。
> 确定性 IO（拉论文/解析 PDF/骨架）走 `scripts/paper_ingest.py`；**提炼与分镜脚本由你 LLM 完成**——这是本 SKILL 的核心价值。

> 视频转图文（反向）见 **video-to-article**；纯格式转换见 **doc-convert**；
> 只做图表见 **chart-visualization / infographic**；发视频号见 **skill-channels-upload**。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| 论文 | 是 | arxiv id（2401.12345）/ arxiv 链接 / 本地 PDF 路径（没给就问） |
| 目标形态 | 否 | 视频（默认，视频号/B站）/ 图文（知乎/公众号）/ 两者都要 |
| 视频画幅 | 视频时需要确定 | 优先复用用户明确值、当前项目规格或目标平台已确定的输出规格；只有多个合理选择会明显改变最终交付时再询问，可逆预览允许采用说明过的合理默认 |
| 受众深度 | 否 | 大众科普（默认）/ 同行向（更专业） |
| 时长 | 否 | 视频默认 2–4 分钟（视频号中视频） |

## 产物结构（`outputs/论文简称/`）

```
article.md               图文版（知乎/公众号）
final.mp4                成片
assets/                  paper.pdf / parsed/ / asset-library.json / script.md
  slide-plan.json        结构化分页（页面唯一输入，口播与屏幕文字分离）
  slides/                稳定渲染的逐页 PNG + HTML + audit report
  slides-contact-sheet.jpg  整套视觉复核图
```

脚本（相对项目根）：`paper_ingest.py`（解析）+ `render_slides.py`（分页校验/渲染/审计）。

## 执行步骤

### 1. 取原文 + 解析

1. **环境自检（首次实际解析或故障时）**：独立 CLI 第一次需要拉取/解析论文，或上次解析因环境失败时运行 `paper_ingest.py check`。局部修改解说稿、slide 文案时不重复做环境检查；缺依赖只报告阻塞，不在未获授权的任务中自动安装。
2. **拉论文**：`paper_ingest.py fetch --paper <id/url/本地pdf> -o outputs/论文简称/assets/paper.pdf`。
3. **解析**：`paper_ingest.py parse -i outputs/论文简称/assets/paper.pdf -o outputs/论文简称/assets/parsed/`
   （有 `MINERU_API_TOKEN` 走 MinerU 含公式/图表结构化，否则 pdfplumber 纯文本 + 尽力抽图）。

### 2. 结构化提炼（你来做，核心）

4. 生成骨架：`paper_ingest.py skeleton -o outputs/论文简称/assets/asset-library.json`。
5. 读 `assets/parsed/content.*`，按 `references/paper-distill-schema.md` 填满 `assets/asset-library.json`：
   `one_liner`（一句话讲清干了啥）、`problem`/`prior_gap`、`contributions`（≤3 条）、
   `method`（含**通俗类比** analogy）、`key_figures`（挑 2–4 张关键图，每张写 `plain` 大白话解释）、
   `results`（含关键数字）、`limitations`、`takeaway`、`terms`（术语通俗表）。
   **通俗化方法**见 `references/explain-methodology.md`（公式/图表→大白话、类比法、避免堆术语）。
6. **忠于原文**：不夸大、不编造结论；拿不准的地方标注，别臆测（学术内容错了会被同行抓）。

### 3A. 视频产线（视频号/B站）

7. **分镜脚本**：按 `references/video-storyboard.md` 结构（钩子→问题→已有不足→贡献→方法一图讲清→结果→意义）把 asset-library 写成 `assets/script.md`。
   分镜/留存/口播节奏**复用 video-script** 的方法（喂论文语境）。可选**双人问答口播**（主持人提问+讲解者回答）比单人旁白更抓耳——用双人时把口播写成逐行 `lines.json`（`{speaker,text,emotion}`，speaker=主讲/提问）。
8. **视觉素材盘点 + 配图**：先列出每页的视觉角色（证据图/重绘图/概念线稿/字体图形/motif），再写 slide-plan。论文原图从 `assets/parsed/figures/` 选用；复杂原图先裁关键区域，方法流程/结果图用 **infographic / chart-visualization** 重绘。封面/概念页缺图时，主动找或制作与主题直接相关的线稿、局部图或符号素材，不用随机机器人/blob 填空。图中文字在目标分辨率不可读就不得直接使用。
9. **稳定 slide 产线**：新建 slide 视觉方案或改变全局主题时读取 card-design 与 `references/slide-design.md`；同一项目已有 slide-plan/style、只改内容时复用，不重复通读。把用户/Profile/参考图的可观察风格特征写入 `style`；未指定时可用 `editorial` 作为默认，但用户明确的品牌/参考图优先。运行：
   ```bash
   python skills/ripple/paper-explainer/scripts/render_slides.py validate --plan outputs/<项目>/assets/slide-plan.json
   python skills/ripple/paper-explainer/scripts/render_slides.py render --plan outputs/<项目>/assets/slide-plan.json --out-dir outputs/<项目>/assets/slides
   python skills/ripple/paper-explainer/scripts/render_slides.py audit --plan outputs/<项目>/assets/slide-plan.json --slides-dir outputs/<项目>/assets/slides --contact-sheet outputs/<项目>/assets/slides-contact-sheet.jpg
   ```
   非 0 退出先分类：plan/schema/越界/对比度等产物问题才修改 plan；缺依赖、渲染器故障或网络问题按环境错误处理。创建/全局主题变化后看 contact sheet，并抽查代表性原尺寸页；局部修改只检查受影响页及共享模板影响面。文字裁切、越界、不可读、论文图无法辨认等客观问题阻塞交付；留白、节奏和素材丰富度作为设计诊断，不要求固定抽查“至少 3 张”或循环到主观全 PASS。
10. **成片（配音+字幕+合成，缺一不可）**：从 slide-plan 的 narration 生成口播——单人用 **tts-voiceover**，双人用 **multi-voice-dubbing**；同步 SRT，缺则跑 **auto-subtitle**。把 `assets/slides/slide_*.png`、配音和字幕写入 auto-short-video storyboard 后合成，必须设顶层 `"image_motion": "static"`；slide/图表禁用 Ken Burns，不得缩放、平移或裁掉边缘。页面停留时长按对应 narration 音频/字幕分段，不均分整轨。不能只交静态图或无声视频。
11. 用 `manifest.py meta` 登记 `final.mp4` 或 `article.md` 为 deliverable；中间解析、slide 和音频只放 `assets/`。
12. **发布**：交 **skill-channels-upload**（视频号）/ B站 biliup。

### 3B. 图文产线（知乎/公众号）

13. 用**同一份 asset-library** 写 `article.md`：标题（钩子）+ 用大白话讲清 problem→method→results→takeaway，配 `assets/` 的图。
    平台适配见 `references/platform-adapt.md`（知乎逻辑链、公众号成文起承转合）。排版/长图交 **doc-convert**；发布交 **skill-zhihu-publisher / skill-wechat-publisher**。

## Profile 感知

- **有 Profile**：按任务需要读取平台规格、受众、风格和领域信息；已有明确画幅/时长时直接复用，不再额外确认。
- **无 Profile**：默认视频号 2–4 分钟中视频、大众科普深度，先问领域与受众。

## 规则

1. **忠于原文**：不夸大贡献、不编造数字/结论；术语拿不准先查原文，别臆测。
2. **一次提炼、两处复用**：asset-library.json 是唯一真相源，视频与图文都从它出，避免重复提炼与口径不一。
3. **通俗但不失真**：用类比降低门槛，但类比不能扭曲原意；关键术语给一句通俗解释而非回避。
4. **图优先**：论文靠图讲方法/结果，视频/图文尽量用图（原图或重绘信息图）承载信息。
5. **刻意不做**：数字人讲座（太重）、依赖 LaTeX 源（从 PDF 入覆盖更广）。
6. **页面不是口播稿，也不是口号板**：一页一个中心结论，但必须用解释、证据或数字口径让页面在暂停/静音时也能独立读懂；细节留给 narration，不得靠缩小字号容纳过量文字。
