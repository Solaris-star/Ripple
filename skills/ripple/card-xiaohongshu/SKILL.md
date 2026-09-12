---
name: card-xiaohongshu
description: "把已有卡片文案渲染为 1080×1440 小红书竖版知识卡片组，并按 card-design 选择视觉风格。当用户说“渲染/制作小红书卡片、知识卡、滑动卡片组”时使用。整套笔记策划与文案用 xhs-note-creator；横版金句卡用 card-quote。"
layer: produce
---

# 小红书图文卡片

你是一名小红书视觉内容设计师。根据用户提供的内容，生成一组小红书风格的 HTML 知识卡片。

## 设计系统按需使用

新建卡片组或改变视觉方向时读取 [card-design](../card-design/SKILL.md)；同一项目已经有明确 spec、只改文字/数据/单张素材时直接复用，不重新走风格选择。用户给出的品牌规范、参考图和明确视觉要求优先；无明确风格时可采用一个合理推荐默认并说明，不必先停下来等待选择。

## 输出规范

- 输出 N 张连续卡片，每张 `width: 1080px; height: 1440px`，用 flex 纵向排列方便整体截图也方便单张截图
- N 由用户内容信息量决定：短内容 3-6 张起步，长内容更多（小红书平台单帖最多 18 图，通常 9 张以内最佳）
- 一张卡只承载一个核心观点

## 卡片结构

按 `card-design/references/card-recipes.md` 的骨架选型（封面/账本/管线/对比/矩阵/数据/金句/收尾）。典型一套：
1. **封面卡** — Display 大标题(细字重) + 一句钩子副标 + 顶 kicker + 底信息行（填到底，别中段空）
2. **正文卡** — 每张一个核心观点，用**账本行/管线/矩阵**等有信息量的骨架填满，不是一句话配大空白
3. **收尾卡** — 要点回顾(小账本) + 行动号召 + 水印

## 视觉风格（基线，细节见 card-design）

- **配色**：整套卡片保持一致的配色逻辑；可从 palettes 取基线。用户品牌色/参考图优先，不强制某类主题使用固定蓝色或暖纸色。
- **禁**：深蓝/蓝紫科技渐变、渐变文字、玻璃拟态、emoji 当图标、居中一切、大标题用粗黑体、`flex:1` 顶出的底部死空白。（详见 `anti-ai-slop.md`）
- **留白与密度**：知识卡避免无意欠填，但 75%/15% 仅作诊断参考。不得为过阈值编造内容；本 Skill 的目标画幅是 1080×1440，不能因为内容少就擅自换成 1:1。（详见 `layout-laws.md`）
- 图标用线性图标(Lucide 风格,stroke 1.5)或纯排版，不用 emoji。字号大、对比强、行距宽（手机可读，正文 ≥28px）。
- 每张卡片角落小水印（作者名 / 日期）。

## Profile 感知

- **有 Profile**：从 `style.md` 读品牌配色和风格偏好（但仍遵守 card-design 的高级感底线，别退回"柔和渐变"这类模糊描述），从 `identity.md` 读账号名用作水印
- **无 Profile**：按 card-design 默认——知识/科技类默认瑞士+克莱因蓝或杂志+Indigo Porcelain，生活/情感类默认杂志暖纸系，水印留空

## 与其他卡片 SKILL 的区别

三者都是"HTML 单图 → 截图"，仅画幅与场景不同，互不替代：

- **card-xiaohongshu（本 SKILL）** = 1080×1440 竖版小红书知识卡，可多张联排滑动浏览，一套干货拆成 3-9 张。**小红书的封面/首图**也用本 SKILL 的封面卡（一套卡的第 1 张）。
- **card-quote** = 16:9 横版金句/数据卡，单张 hero 观点或核心数字，配微博 / 知乎 / X / 公众号。
- **poster-hero** = 1080×1920 竖版**独立营销海报** / 朋友圈分享图，大标题 + 卖点 + 二维码，用于产品发布、活动宣传（不是笔记首图——笔记首图用本 SKILL 的封面卡）。

（三者生成前都应先读 card-design 设计系统。）

## 输出

生成完整的 HTML 文件，写入 `outputs/` 目录，再用共享脚本自动渲染成图（勿手动截图）：

```bash
# 多张卡片：每张 .card 元素单独出图，得 card_1.png card_2.png ...
python skills/shared/scripts/render_card.py \
  --html outputs/主题名/assets/cards.html \
  --out-dir outputs/主题名 --all ".card" --prefix card \
  --width 1080 --height 1440

# 布局/渲染发生变化时运行审计；客观渲染错误必须修复，密度类结果按设计意图判断
python skills/ripple/card-design/scripts/card_audit.py audit -f "outputs/主题名/card_*.png"
```

- 竖版 1080×1440；HTML 里每张卡片外层用统一 class（如 `.card`）便于 `--all` 批量导出。
- 脚本用 playwright+chromium，对 CDN/字体有界超时不卡死；首次需 `pip install playwright && playwright install chromium`。
- `card_audit` 命中裁切、溢出、不可读等客观问题时修复并重渲；若只是密度/留白偏离建议，结合用户风格与卡型处理，不循环到“全 PASS”才允许交付。
