---
name: auto-short-video
description: "一句话主题 → 成品短视频：自动串联文案、配图/AI视频、配音、字幕、BGM 与合成。单条口播/资讯视频可用图片缓动或动态片段；当用户说一键生成视频、自动做短视频、主题生成视频、口播视频一条龙、自动出片时使用。有剧情/角色/对白/多集的短剧改用 short-drama，正式动态短剧使用真实视频片段，不能把静态图冒充成片；只写脚本用 video-script；只生成单个片段用 ai-video-gen。"
layer: produce
---

# 一键短视频（端到端编排）

> 输入一个主题，自动产出一条短视频。本 SKILL 是**编排层**：把已有制作零件串成流水线——
> 文案(video-script) → 逐句配图(ai-image-gen)或片段(ai-video-gen) → 配音(tts-voiceover) →
> 字幕(auto-subtitle) → BGM(ai-music) → **合成(scripts/assemble.py)**。

## 输入

- 主题 / 文案（必填）
- 可选：目标时长、风格、是否要配音/字幕/BGM、配图用 AI 生图还是用户素材
- **画幅**：优先使用用户明确值、已有项目规格或目标内容已确定的比例；只有多个合理选择会明显改变最终交付时才询问。可逆样片可采用说明过的合理默认；正式付费批量开始前确保本任务画幅已确定即可。

## 输出

成品短视频写入 `outputs/主题名/final.mp4`；分镜图、配音、字幕和 storyboard 写入 `outputs/主题名/assets/`。

## 执行步骤（按需裁剪，缺 API key 的环节自动降级或询问）

1. **写脚本分镜**：用 [video-script](../video-script/SKILL.md) 把主题写成口播文案，拆成 N 句（每句一个分镜），每句配一个画面描述。

2. **生成画面**（每个分镜一张图/一段片）：
   - 有图像 API key → [ai-image-gen](../ai-image-gen/SKILL.md) 逐句 text2img（按已确认画幅）
   - 要动态 → [ai-video-gen](../ai-video-gen/SKILL.md) text2video/image2video
   - 用户自带素材 → 用 [image-editing](../image-editing/SKILL.md) `pad` 到已确认画幅
   - 都没有 → 按已确认画幅选图卡（竖版用 card-xiaohongshu/poster-hero，横版用 card-quote）再 pad，避免画幅错配。

3. **配音**：按用户本次选择或 Settings/当前任务默认的可用语音能力合成口播（同时出 SRT）。音色质量看实际结果与用户要求，不因为 Provider 类型给“闭源必优于其他引擎”的通用门禁；不需要配音则跳过。

4. **字幕**：用 TTS 附带的 SRT，或对配音跑 [auto-subtitle](../auto-subtitle/SKILL.md)；也可让 assemble 用各分镜 caption 自动生成。

5. **BGM**：[ai-music](../ai-music/SKILL.md) 生成，或用用户提供的音乐。可选。

6. **合成成片**：把上面的素材写成 storyboard JSON，调合成器：
   ```bash
   python skills/ripple/auto-short-video/scripts/assemble.py assemble \
     --storyboard outputs/主题名/assets/storyboard.json \
     -o outputs/主题名/final.mp4
   ```
   storyboard 结构（图/片二选一，narration/bgm/subtitle 可选，缺 duration 时按配音均分）：
   ```json
   {
     "size": "<已确认尺寸，如1080x1920或1920x1080>",
     "image_motion": "ken-burns",
     "shots": [
       {"image": "outputs/主题名/assets/shot1.png", "duration": 3, "caption": "第一句", "motion": "static"},
       {"video": "outputs/主题名/assets/clip2.mp4", "caption": "第二句"}
     ],
     "narration": "outputs/主题名/assets/voice.mp3",
     "bgm": "outputs/主题名/assets/bgm.mp3",
     "subtitle": "outputs/主题名/assets/voice.srt"
   }
   ```
   `image_motion` 设整条图片默认运动，单镜 `motion` 可覆写：照片用 `ken-burns`，含文字的 slide/图表/界面必须用
   `static`（等比缩放 + 补边，不裁切、不平移）。
   合成器自动做：按 `image_motion` 生成静帧或 Ken Burns、补边到画幅、拼接、配音+BGM 混音（BGM 自动压低）、烧录字幕。

7. **交付**：产出 final.mp4，附一句制作说明（用了哪些环节、哪些降级了）。

## 编排原则

- **零件可缺**：缺图像/视频/TTS API key 的环节自动降级（图卡兜底 / 跳过配音），不阻断整体，并如实告知用户降级了什么。
- **付费范围一次确认**：首次进入新的付费批量范围时说明大致调用量、耗时和可得的费用信息；用户已授权当前范围后，各分镜/子步骤不重复确认。扩大范围或切换到明显不同费用/数据目的地时再确认。
- **中间产物留档**：分镜图、配音、字幕和 storyboard 都写进 `outputs/主题名/assets/`，方便单独替换后重新合成。

## Profile 感知

- 有 Profile：按需读取风格、平台规格和偏好；已有明确项目画幅就复用。
- 无 Profile：根据用户目标与当前素材确定规格；画幅确有歧义且影响最终交付时再询问。
