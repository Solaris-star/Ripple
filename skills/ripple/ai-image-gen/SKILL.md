---
name: ai-image-gen
description: "通用 AI 生图：文生图 / 图生图 / 图像变体。当用户说 AI 生图、AI 画图、文生图、图生图、生成图片、生成配图、图像生成、AI 出图、AI 作图、换图、改图、图像编辑、给我画一张、生成一张图 时使用。支持 OpenAI 兼容 API 与 apimart 异步 API，用户自备 API key。"
layer: produce
---

# ai-image-gen Skill

> 通用 AI 文生图 / 图生图 / 图像变体。Ripple 托管调用从 Settings 读取已配置的 Provider/Model 默认组合，产物进入 Ripple 素材库；Skill 不读取或索要 API Key。

托管 Agent 有 `ripple_generate_image` 时优先使用受控工具。`skills/shared/scripts/ai_image.py` 只作为独立 CLI/维护场景的兼容入口；SKILL 中出现脚本命令不代表托管 Agent 获得 Shell 权限。

## 边界（和相邻 SKILL 区分）

- **ai-image-gen（本 SKILL）**：通用 AI 生图，任意题材，文生图 / 图生图 / 变体。
- **ecom-details-image**：电商详情页 / 商品主图专用出图（25 场景模板、PDP 序列）。要做电商商品图走它。
- **card-\* / poster-\***：HTML+CSS 渲染截图（金句卡、小红书卡、海报），**非 AI 生成**，是确定性设计出图。
- **image-editing**：已有图片的确定性处理（改尺寸/裁剪/加水印/压缩），不生成新画面。

## 配置来源

**Ripple 托管 Agent**：先读取媒体 capability；用户未点名时直接使用 Settings 的默认图片 Provider + Model。存在多个可用模型不是追问理由。只有默认不可用、用户要求某个特殊能力，或切换会改变成本/数据发送目的地时才询问。

**独立 CLI/维护场景**：`ai_image.py` 仍兼容本地环境变量和历史配置；只有实际运行该 CLI 时才执行它的 `check`。不要把 CLI 的 `.env` 规则用于判断 Ripple Settings 是否已配置。

同步/异步提交属于 Provider 协议适配细节，托管 Agent 不要求用户选择请求模式。

## 执行步骤

### 1. 确认本次生成条件

托管 Agent 只检查脱敏 capability、输入图（如有）、尺寸/用途等本次必要条件；Settings 默认组合可用就继续，不额外做配置访谈。独立 CLI 才按需运行：

```bash
python skills/shared/scripts/ai_image.py check
```

CLI check 失败时报告缺失配置；网络/Provider 故障不要误判为提示词或图片内容问题。

### 2. 文生图 text2img

先把用户诉求写成一条清晰的图像 Prompt（主体 + 风格 + 构图 + 光线 + 画质），再执行：

```bash
python skills/shared/scripts/ai_image.py text2img \
  --prompt "一只戴墨镜的柴犬，扁平插画风，明亮撞色背景，高细节" \
  --size 1024x1024 --n 1 \
  --output outputs/主题名/ai-image
```

- `--size`：同步模式用像素（`1024x1024` / `1536x1024` / `1024x1536`…）；异步模式用比例（`1:1` / `16:9` / `9:16`…）。
- `--n`：生成张数（多张时按序号自动命名）。
- `--output`：目录（多张自动编号）或含扩展名的单文件；统一放 `outputs/主题名/`。
- 同步可加 `--quality low|medium|high`；异步可加 `--resolution 1k|2k|4k`。

### 3. 图生图 / 图像编辑 img2img

基于一张输入图 + 指令生成新图（OpenAI 走 `/images/edits` multipart，可选 `--mask` 局部编辑；apimart 把输入图作参考图走生成端点）：

```bash
python skills/shared/scripts/ai_image.py img2img \
  --prompt "把背景换成夜晚霓虹街道，保留主体" \
  --image path/to/input.png \
  --output outputs/主题名/edited.png
```

### 4. 图像变体 variations

由一张图生成多个变体：

```bash
python skills/shared/scripts/ai_image.py variations \
  --image path/to/input.png --n 3 \
  --output outputs/主题名/variations
```

### 5. 交付

告诉用户产物位置以及对结果有意义的参数（模型、尺寸、张数）。内部请求模式、密钥、Base URL、代理等不进入交付说明。如需再改尺寸/加水印/压缩，转 `image-editing`。

## 产物

统一输出到 `outputs/主题名/`。脚本会自动创建目录，多张按时间戳 + 序号命名。

## Profile 感知

有账号 Profile（`=== EASEL ACCOUNT PROFILE ===`）时，把品牌视觉风格（配色 / 调性 / 元素偏好）融入 Prompt，保持系列图统一；无 Profile 时按用户描述走通用生成。

## 常见问题

- **缺 key / 缺配置**：`check` 会明确指出缺哪项及 `.env` 示例；报错为友好中文，不抛 traceback。
- **Provider 协议差异**：托管模式由 Ripple 适配器处理；独立 CLI 报参数错误时再按其帮助信息检查尺寸/比例，不让用户先理解同步/异步实现细节。
- **不要把真实 key 写进任何产物或提交**。
