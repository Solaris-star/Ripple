---
name: poster-hero
description: "生成 1080×1920 竖版营销海报，包含大标题、核心卖点和可选二维码，适合产品发布、活动宣传与朋友圈传播。当用户说“做竖版营销海报、活动宣传图、朋友圈海报、产品发布海报”时使用。横版金句卡用 card-quote，小红书知识卡用 card-xiaohongshu。"
layer: produce
---

# 营销海报

你是一名视觉营销设计师。根据用户提供的内容，生成一张竖版高冲击力营销海报 HTML。

> 新建/改变海报视觉方向时读取 [card-design](../card-design/SKILL.md)；同一项目已有 spec 的文字/数据修改直接复用。渐变、字重、留白等由品牌与设计意图决定；可读性、裁切和用户明确禁区才是客观门。

## 输出规范

- 容器 `width: 1080px; height: 1920px`，居中显示，圆角裁切
- 输出完整的 HTML 文件，写入 `outputs/` 目录，可直接在浏览器打开截图

## 海报结构

1. **上部** — 品牌名 / 标签（发布日期、版本号、活动名等）+ 一个**线性图标或抽象几何标记**（禁 emoji 当图标）
2. **中部视觉中心** — 主标题（**字越大越细**：大字用 Thin/Light 字重，靠字号+留白建立层级，而非 `font-black`）+ 一句话副标题，用 1 个强调色高亮关键词
3. **下部信息卡片** — 3-5 条核心卖点，每条 **线性图标（Lucide，stroke 1.5）** + 短句
4. **底部** — 右下角品牌 / 二维码（用 SVG 占位）+ CTA 文案；**底部填满，不留死空白**

## 视觉风格

> 参考 [card-design](../card-design/SKILL.md) 保持整张海报的配色/字体/网格逻辑一致；用户品牌规范、参考图和可访问性要求优先。下面是常见风险提示，不是所有品牌的绝对审美红线。

- **背景**：氛围渐变 / mesh 或单色系深底 + 1 个强调色。**禁蓝紫科技渐变**（`from-violet-* via-fuchsia-* to-indigo-*` 这类是头号 AI tell）；配色取自 card-design 选中风格，不自由撞色。
- **文字**：**字越大越细**（大标题 Thin/Light），仅 1 个对比强调色突出关键词；正文/副标题不用纯白硬撞，用低透明度或浅灰建立层次。**禁渐变文字**（`bg-clip-text`）。
- **装饰克制**：发丝线 / 网格 / 极淡噪点纹理（grain）即可。**禁玻璃拟态**（`backdrop-filter:blur`）、禁堆叠阴影。
- **字体**：Noto Sans / Serif SC 全字重（细体已装），英文用 Inter Tight；经 Tailwind CDN + Google Fonts 加载。
- **留白与密度**：1080×1920 必须按用户规格输出；75%/15% 仅用于发现无意欠填，不为通过阈值添加无关卖点或装饰（见 card-design `layout-laws.md`）。

## 示例 Prompt

- "帮我做一张产品发布海报，产品是 XXX，核心卖点是 A、B、C"
- "做一张活动宣传海报，主题是年中大促，时间 7 月 20 日"
- "朋友圈分享图，内容是我们团队刚拿了 XX 奖"

## 与其他卡片 SKILL 的区别

三者都是"HTML 单图 → 截图"，仅画幅与场景不同，互不替代：

- **poster-hero（本 SKILL）** = 1080×1920 竖版营销海报 / 朋友圈分享图，全屏渐变 + 大标题 + 卖点卡片 + 二维码，用于产品发布、活动宣传。
- **card-quote** = 16:9 横版金句/数据卡，单张 hero 观点或核心数字，配微博 / 知乎 / X / 公众号。
- **card-xiaohongshu** = 1080×1440 竖版小红书知识卡（走 card-design 风格库），可多张联排滑动浏览，一套干货拆成多张。

## Profile 感知

- **有 Profile**：从 `style.md` 读取品牌配色替换默认背景，从 `identity.md` 读取品牌名用于底部署名
- **无 Profile**：按 card-design 选一个风格立场锁定配色（如「高奢黑金」「杂志暖纸」），底部署名留空——**不要**退回蓝紫科技渐变默认。

## 输出

生成完整的 HTML 文件，写入 `outputs/` 目录，再用共享脚本自动渲染成图（勿手动截图）：

```bash
python skills/shared/scripts/render_card.py \
  --html outputs/主题名/assets/poster.html \
  --out outputs/主题名/poster.png \
  --full-page --width 1080 --height 1920
```

- 竖版海报 1080×1920 用 `--full-page`。脚本对 CDN/字体有界超时不卡死。
- 首次需 `pip install playwright && playwright install chromium`。
