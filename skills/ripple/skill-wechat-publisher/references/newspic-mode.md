# 贴图模式(newspic / 图片消息)完整流程

和 7 阶段"图文"(news)流程**并列**的第二种发布形态。对标微信公众号的"图片消息":5-10 张图的**卡片墙** + 一段 100-300 字的**短描述**,适合:

- 单一主题的"拆卡"式讲解(示例:[Claude Code /rewind](https://mp.weixin.qq.com/s/erEF74HRGkrBPxTGsKDsSQ))
- 金句 / 观点串
- 图片清单 / 作品合集
- 任何"文字偏少、靠图主导"的内容

### 贴图默认就是高密度手绘信息图

**不需要在 brief.md 里写 `image_style`**,贴图模式的默认兜底是 `infographic-warm`。默认要求是:

- **高密度中文信息图**,不是普通插画、不是大字海报
- **手绘水彩 + 墨线**,不是 flat vector、不是 3D
- 9:16 竖版,每张卡都要承载足够信息
- 配色、布局、设计组件和画面元素按主题自适应,不要固定成同一套版式
- 机器人、小男孩、终端条、2×2 网格、胶囊标签等都是可选元素,只在内容合适时使用

想换整体氛围就在 brief.md 写 `image_style: infographic-blue` / `infographic-dark` / `infographic-mint`(见 [`assets/image-styles/README.md`](../assets/image-styles/README.md))。即使用这些风格,也只是给一个视觉方向,不是强制固定布局。

**账号级别**：独立 CLI 的账号配置可以声明 `newspic_image_style`，与文章模式的 `image_style` 分开。实际默认来自用户所选账号/Profile；仓库中的示例账号和风格只能作为示范，不应用来推断当前用户偏好。

### 何时用贴图,何时用图文

| 判据 | 图文(news) | 贴图(newspic) |
|---|---|---|
| 正文字数 | 2500-5000 字 | 100-300 字短描述 |
| 图数 | 6-10 张内联 | 5-10 张卡片墙 |
| 主载体 | 文字 | 图片 |
| 结构 | 开篇/小节/结尾 | 拆卡,一卡一要点 |
| 适合 | 深度观察 / 教程长文 | 观点串 / 技巧卡 / 金句 |
| 自然度检查 | 按需、证据式 | 按需、证据式；现有 `ai_score` 如启用属于 CLI 实现约束 |

### 4 步流程

```
brief.md → newspic_build.py 拆卡 → `scripts/generate_image.py` 批量生图 → publish.py --type newspic
```

#### 1. 写 brief.md

```markdown
---
topic: "Claude Code /rewind 命令"
image_style: infographic-warm  # 可选,不写用账号 newspic_image_style,再兜底 infographic-warm
card_count: 6                  # 可选,不写按要点数
title: "Claude Code 里,最有用的命令之一"
account: <已选账号>
---

# 要点

1. /rewind 厉害的地方不是"撤销一下",而是给你一个更对的工作流
2. 你可以输入 /rewind,也可以连续按两次 Esc,快速回滚代码
3. AI 解决不好问题,常常不是因为它不够会写,而是你不敢让它放手试
4. /rewind 的价值,就是把"试错"这件事真正变得可控

# 短文本

/rewind 厉害的地方,不是"撤销一下",而是给你一个更对的工作流:
先大胆尝试,再快速回退。
真正值得的不是它的撤销力,而是它给你的"敢试"。
```

**frontmatter 字段**:
- `topic`(必填):整个贴图的核心主题,用于给 Claude 提供语境
- `image_style`(可选):配图风格。不填就走 **账号 `newspic_image_style` → 全局 `infographic-warm`** 兜底。贴图模式默认就是高密度手绘水彩信息图,正常情况下这行留空。
- `card_count`(可选):卡片数量,不填按要点数,必须 ≤ 要点数,≤ 20
- `title`(可选):贴图标题,不填也行
- `account`(可选):发到哪个账号

**正文**至少要有 `# 要点` 小节,每行一条要点;`# 短文本` 可选(不填就让 Claude 根据要点写)。

#### 2. 拆卡 + 生成计划

```bash
python3 skills/ripple/skill-wechat-publisher/scripts/newspic_build.py brief.md
# → 同目录写出 card_plan.json,列出每张卡的主副文字 + 完整 Gemini prompt + 目标文件名
```

Claude 读 `card_plan.json`,按每张卡的 `prompt` 字段调项目内置 `scripts/generate_image.py` 生图,保存到 `brief.md` 同目录的 `images/01.png`、`02.png` ...

#### 3. 写 / 检验短文本

如果 `brief.md` 的 `# 短文本` 还是空的,Claude 根据要点写一段 100-300 字,填回去。

**短文本自然度检查按需执行**。当前独立 CLI 的 `publish.py` 仍可能调用 `ai_score`，这是实现层约束；规则层不把其主观分数视为通用完成标准：

```bash
# publish.py 会自动在发送前跑一次,这里是手动预检
python3 skills/ripple/skill-wechat-publisher/scripts/ai_score.py brief.md --mode newspic --threshold 45
```

命中明确模板套话时做定向改写；如果事实、用户 voice 和可读性已经满足要求但脚本仍因主观分数阻塞，停止无界重写并报告实现层限制。

#### 4. 发布

```bash
python3 skills/ripple/skill-wechat-publisher/scripts/publish.py --account <已选账号> --type newspic --brief brief.md --exec
# 或显式覆盖风格
python3 skills/ripple/skill-wechat-publisher/scripts/publish.py --account <已选账号> --type newspic --brief brief.md --image-style knowledge-card --exec
```

`publish.py` 做的事:
1. 从 brief.md 读 frontmatter + 短文本
2. 运行当前 CLI 实现包含的自然度/ai_score 检查；因实现门禁停止时如实报告，不用无界改稿规避
3. 扫 `brief.md 同目录/images/*.{png,jpg,jpeg,webp}`,按文件名排序作为展示顺序
4. 逐张上传为微信永久素材(每张占一个永久素材名额,5000 上限)
5. 调 `draft/add` 建 newspic 草稿

⚠️ **永久素材成本提醒**:贴图每张都走 `add_material`,5-10 张贴图每次发布占 5-10 个永久素材名额。文章模式的正文图走 `uploadimg` 不占名额,但**封面图**和**贴图图片**都要占。

### newspic 的限制

- 微信最多 20 张图,建议 5-10 张,低于 2 张会警告
- 不支持多平台同步(`--sync` / `--sync-from-config`)
- 不支持行内标色、HTML 主题 —— 短文本只是一段纯文本
- 建议用 `marker-*` 极简大字卡,避免过重装饰 + 长句,**卡面字数超过 20 字会影响阅读**
