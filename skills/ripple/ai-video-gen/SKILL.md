---
name: ai-video-gen
description: "AI 视频生成：文生视频 / 图生视频 / 数字人首帧驱动。通过可插拔 provider（通义万相 Wan / 火山 Seedance / 快手可灵 / OpenAI 兼容）异步生成视频，用户自备 API key。当用户说 AI 视频生成、文生视频、图生视频、AI 生成视频、AI 短视频、让图片动起来、数字人视频、生成一段视频 时使用。与 video-strategy（选型/策略）、video-editing（剪辑处理）、clipify（切片）区别：本 SKILL 是从 0 用 AI 生成新视频。"
layer: produce
---

# AI 视频生成

> 文生视频 / 图生视频 / 数字人首帧驱动。Ripple 托管 Agent 通过 `ripple_generate_video` 和 Settings 中已配置的 Provider/Model 调用；`shared/scripts/ai_video.py` 是独立 CLI/维护场景的兼容入口。

## 配置来源

**Ripple 托管 Agent**：从媒体 capability 读取可用 Provider/Model，未点名时使用 Settings 默认组合。不要读取或要求用户重复填写 Key/Base URL；不要因为存在多个 Provider 就再次询问。

**独立 CLI/维护场景**：实际运行 `ai_video.py` 时才按本节的 provider 参数与本地配置做 `check`。以下适配器名称用于 CLI/协议诊断，不是托管 Agent 的用户配置层级：

```bash
python skills/shared/scripts/ai_video.py check --provider dashscope
```

| provider | 服务 | 需在 .env 配 |
|----------|------|-------------|
| `dashscope` | 阿里通义万相 Wan | `DASHSCOPE_API_KEY`（可选 `DASHSCOPE_VIDEO_MODEL`/`DASHSCOPE_BASE_URL`；兼容旧名 `DASHSCOPE_MODEL`） |
| `ark` | 火山引擎 Seedance | `ARK_API_KEY`（可选 `ARK_MODEL`/`ARK_BASE_URL`） |
| `kling` | 快手可灵 | `KLING_ACCESS_KEY` + `KLING_SECRET_KEY`（JWT 鉴权） |
| `openai-compatible` | 通用 /videos 端点 | `VIDEO_API_KEY` + `VIDEO_BASE_URL`（可选 `VIDEO_MODEL`） |
| `xhs-maas` | 旧版兼容标识：异步 `media/parameters` 协议 | 仅为已有本地配置迁移保留；必须显式配置端点、凭据与模型。公开版不内置组织专用端点或模型 |
| `agnes` | Agnes（agnes-video-2.5-flash）| `AGNES_API_KEY`（可选 `AGNES_BASE_URL`/`AGNES_MODEL`/`AGNES_SIZE`）。OpenAI Videos 兼容创建 + 自定义端点轮询；**默认带原生音频**（prompt 描述声音）；外网走代理 |

独立 CLI 可用自身的 provider/model 参数选择适配器。托管 Ripple 不使用 `VIDEO_PROVIDER` 或 `model_registry.py` 作为权威配置源。

## 输入

> **画幅按任务触发**：优先使用用户明确值、当前项目已锁定规格或目标内容已有的确定画幅。只有多个合理选择会明显改变最终交付时才询问；可逆预览/样片允许采用明确标注的合理默认。付费正式生成前确保画幅已经确定即可，不重复确认同一规格。

- 文生视频：画面/镜头/风格描述（prompt）
- 图生视频：一张输入图（本地路径或 URL）+ 可选运动描述
- 可选：时长 `--duration`、画幅 `--ratio`（16:9 / 9:16 / 1:1）、模型 `--model`、原生音频 `--audio auto|on|off`

## 输出

生成的视频文件；必须用 `-o` 指定到 `outputs/主题名/`。异步任务自动轮询到完成再下载。

## 执行步骤

1. **确认能力**：托管 Agent 使用 Ripple capability 中明确的 Provider/Model 信息；不要根据品牌名猜原生音频、图生视频等模型能力。独立 CLI 调试时才运行 `check` / `capabilities`。
   - **`probe-dialogue`**（短剧用）：真发 1 次生成 + ASR，测该模型能否**逐字忠实**说出指定台词，判 `dialogue_faithful` 并缓存——短剧据此决定用原生对白，还是"无台词生成 + 后期配音"。用法 `probe-dialogue --provider <p> --model <m>`。
2. **写好 prompt**：AI 视频对 prompt 敏感，按 [AI 视频提示词规范](../video-strategy/references/ai-video-prompting.md) 写镜头、运镜、风格与时长。竖版短视频用 `--ratio 9:16`。
3. **文生视频**：
   ```bash
   python skills/shared/scripts/ai_video.py text2video --provider dashscope \
     --prompt "海边日落，慢镜头推进，暖色调，电影感" --ratio 9:16 --duration 5 \
     --audio auto \
     -o outputs/主题名/clip.mp4
   ```
4. **图生视频 / 让图动起来 / 数字人首帧**：
   ```bash
   python skills/shared/scripts/ai_video.py image2video --provider kling \
     --image outputs/主题名/cover.png --prompt "人物微笑挥手，头发轻微飘动" \
     -o outputs/主题名/clip.mp4
   ```
5. **后续加工**：生成的片段可交给 `video_ops.py`（拼接/加字幕/加 BGM/横竖转）、`auto-subtitle`（字幕）、`tts-voiceover`（配音）串成成片，或直接进 `auto-short-video` 端到端流程。

## Profile 感知

- 有 Profile：按需读取视觉风格和平台规格；已有明确项目画幅时直接复用。
- 无 Profile：按用户目标与当前内容规格生成；只有画幅确实不明确且会影响最终交付时再询问。

## 注意

- 视频生成通常耗时较长且可能按量计费。新的批量范围、明显增加费用或改变服务目的地时确认一次；同一任务已授权的预算/镜头范围内不要逐片重复确认。
- 各 provider 的 model 名/字段各版本有差异，均可用 `--model` 或 env 覆盖；如报错对照官方最新文档调整。
- `--audio auto` 只按 capability profile 映射已知字段；能力声明不等于质量保证，下载后仍须 ffprobe/ASR/视觉审计。网关默认有声但开关字段未知时，不猜测注入参数。
