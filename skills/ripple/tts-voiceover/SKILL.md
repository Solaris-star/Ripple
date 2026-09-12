---
name: tts-voiceover
description: "文字转语音配音：把文案/脚本合成为口播、旁白、朗读音频，可使用 Ripple 当前可用的语音 Provider 或独立 CLI 的公共音色/云服务，并可同步输出 SRT。合成后可与 BGM 混音或加到视频作旁白。当用户说“配音”“文字转语音”“TTS”“AI 配音”“口播语音”“旁白”“朗读”“把这段文字读出来”“生成语音”“语音合成”时使用。"
layer: produce
---

# 文字转语音配音（TTS Voiceover）

> Ripple 托管 Agent 只有在当前会话实际暴露受控语音工具时才执行 TTS；Provider/Model 选择来自 Settings，Skill 不读取 Key。没有受控语音工具时可准备文本、音色要求和参数，但不声称已生成。

独立 CLI/维护场景可使用 `skills/shared/scripts/tts.py speak`。引擎选择按用户目标、实际可用能力和试听质量决定；公共音色、云 TTS 都可以是合法方案，不以“闭源/edge”类别作为通用质量门。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| text / file | 是 | 待配音的文本，或文本文件路径（长文本推荐 --file） |
| voice | 否 | 音色，默认 `zh-CN-XiaoxiaoNeural`（晓晓） |
| rate/volume/pitch | 否 | 语速 / 音量 / 音调微调 |
| output | 否 | 默认 `outputs/主题名/{name}.mp3` |

## 输出

- 配音音频文件（mp3，可选 wav/m4a），放入 `outputs/主题名/`
- 可选同步输出 SRT 字幕（`--subtitle`），供视频烧字幕用
- 打印实际执行的 edge-tts 命令 + 输出文件时长/大小/音色

## 网络（独立 CLI 按需）

使用需要联网的 TTS 服务时按当前运行环境和 Provider 网络配置处理。不要在 Skill 入口假定必须手工设置某个代理；只有实际网络失败且独立 CLI 支持代理参数时再按其帮助信息配置。

## 执行步骤

脚本路径（相对项目根）：`skills/shared/scripts/tts.py`。每个子命令支持 `-h`。

### 0. 挑音色（可选）

```bash
python skills/shared/scripts/tts.py voices          # 常用中文音色 + 简介
python skills/shared/scripts/tts.py voices --all    # 拉全量 zh- 音色（需外网）
```

### 1. 合成配音 speak

```bash
# 最简：一句话 → mp3
python skills/shared/scripts/tts.py speak --text "欢迎来到本期内容" \
  -o outputs/主题名/intro.mp3

# 长文本从文件读 + 换音色 + 加速 10%
python skills/shared/scripts/tts.py speak --file script.txt \
  -o outputs/主题名/narration.mp3 --voice zh-CN-YunxiNeural --rate +10%

# 同步出 SRT 字幕（视频烧字幕用）
python skills/shared/scripts/tts.py speak --file script.txt \
  -o outputs/主题名/vo.mp3 --subtitle outputs/主题名/vo.srt

# 输出 wav（需 ffmpeg，便于后续无损处理）
python skills/shared/scripts/tts.py speak --text "……" \
  -o outputs/主题名/vo.wav --format wav
```

参数：`--rate +10%`（语速）、`--volume +20%`（音量）、`--pitch +2Hz`（音调）。

### 2. 后处理（可选，复用已有共享脚本）

配音出来后按需接下游脚本，无需在本 SKILL 重造能力：

```bash
# ① 配音 + BGM 混音（原声 1.0 / BGM 0.3）→ 用 audio_ops concat / video_ops bgm
python skills/shared/scripts/video_ops.py bgm -i vo.mp3 -o vo_bgm.mp3 \
  --music bgm.mp3 --voice-volume 1.0 --music-volume 0.3

# ② 配音音量归一化到社媒响度（-14 LUFS）
python skills/shared/scripts/audio_ops.py normalize vo.mp3 -o vo_norm.mp3

# ③ 把配音作为旁白加到视频
python skills/shared/scripts/video_ops.py bgm -i clip.mp4 -o clip_vo.mp4 \
  --music vo.mp3 --voice-volume 0.4 --music-volume 1.0
```

## 常用中文音色

| 音色 | 特点 |
|------|------|
| `zh-CN-XiaoxiaoNeural` | 晓晓 · 女声，温暖亲和，通用首选（默认） |
| `zh-CN-XiaoyiNeural` | 晓伊 · 女声，活泼年轻，口播/种草 |
| `zh-CN-YunxiNeural` | 云希 · 男声，清朗自然，旁白/解说 |
| `zh-CN-YunyangNeural` | 云扬 · 男声，专业沉稳，新闻/播报 |
| `zh-CN-YunjianNeural` | 云健 · 男声，浑厚有力，激情内容 |

粤语用 `zh-HK-HiuMaanNeural`（曉曼），台式用 `zh-TW-HsiaoChenNeural`（曉臻）。

## 规则

1. **绝不覆盖原始素材** — 只写新文件到 `outputs/主题名/`。
2. **长文本走 --file** — 避免命令行过长 / 换行转义问题。
3. **网络按实际错误处理** — 只有联网服务失败时才诊断代理/网络，不在每次任务前强制配置。
4. **不重造能力** — 混音/归一化/加视频旁白复用 audio_ops.py / video_ops.py。
5. **无 Profile 也能用** — 无画像时用默认音色晓晓。

## Profile 感知

有 Profile 时可读取账号偏好音色 / 语速 / 平台调性（如口播偏活泼晓伊、
知识类偏沉稳云扬）作为默认参数；无 Profile 退到通用默认（晓晓、正常语速）。
