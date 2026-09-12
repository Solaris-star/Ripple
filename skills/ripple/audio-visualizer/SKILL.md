---
name: audio-visualizer
description: "音频可视化视频：把纯音频（播客片段、音乐、口播金句、电台）渲染成带动态波形/频谱的视频，配封面和标题，好发到抖音/B站/视频号等只收视频的平台。当用户说 音频可视化、音频转视频、播客做成视频、音频波形视频、音乐可视化、给音频配画面、声波视频、频谱视频、把音频发到视频平台、电台切片视频 时使用。基于 shared/scripts/audio_viz.py（ffmpeg showwaves/showcqt/showspectrum）。与 audio-mix 区别：那个输出音频，本 SKILL 输出视频；与 slideshow-video 区别：那个用图片，本 SKILL 用音频驱动画面。"
layer: produce
---

# 音频可视化视频

> 把音频渲染成带动态波形/频谱的视频，配封面+标题，让纯音频能发到视频平台。全部走
> `skills/shared/scripts/audio_viz.py`，**不要手拼 showwaves/showcqt 滤镜**。

> 输出音频（混音）见 **audio-mix**；用图片做视频见 **slideshow-video**；
> 给已有视频加字幕见 **auto-subtitle**。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| 音频文件 | 是 | 播客/音乐/口播片段（没给就问） |
| 画幅 | 需要确定 | 复用用户明确值、项目规格或目标内容已有比例；可逆预览可采用说明过的合理默认，只有多个选择会明显改变交付时才询问 |
| 模式 | 否 | `cqt`（默认，音乐最好看）/ `bars` / `waves` / `spectrum` |
| 封面 | 否 | 居中封面图（专辑封面/头像/主题图） |
| 标题 | 否 | 顶部标题文字 |

## 输出（`outputs/主题名/`）

- 可视化视频（`*.mp4`，音频已嵌入）
- 报告：模式、时长、画幅

## 执行步骤

脚本路径（相对项目根）：`skills/shared/scripts/audio_viz.py`（`render -h` 看参数）。

```bash
# 音乐/金句：CQT 音乐频谱（随音符跳动，最好看）
python skills/shared/scripts/audio_viz.py render -i clip.mp3 \
  -o outputs/主题名/out.mp4 --mode cqt --title "本期金句" --cover cover.jpg

# 播客口播：底部波形条 + 封面
python skills/shared/scripts/audio_viz.py render -i podcast.mp3 \
  -o outputs/主题名/out.mp4 --mode waves --cover avatar.png --size 1080x1920

# 律动柱状 / 滚动声谱
python skills/shared/scripts/audio_viz.py render -i song.mp3 -o out.mp4 --mode bars
python skills/shared/scripts/audio_viz.py render -i song.mp3 -o out.mp4 --mode spectrum
```

## 模式怎么选

| 模式 | 观感 | 适用 |
|------|------|------|
| `cqt` | 全屏音符频谱，随旋律跳动 | 音乐、有旋律的内容（默认） |
| `bars` | 底部频谱柱，律动感强 | 音乐、卡点、电台 |
| `waves` | 底部波形线，简洁干净 | 播客、口播、访谈 |
| `spectrum` | 全屏滚动声谱图，科技感 | 电子/科技类、氛围 |

`--bg-image` 换背景图，`--color` 换背景色，`--wave-color` 换波形颜色。

## Profile 感知

- 有 Profile：需要规格或风格时按需读取相关字段；已有项目画幅直接复用。播客/口播账号可建议 `waves`，音乐账号可建议 `cqt`/`bars`。
- 无 Profile：根据目标平台/素材采用合理默认；真正存在画幅歧义时再询问。默认 cqt 模式。

## 规则

1. 长音频先用 audio-editing/text-condenser 截出金句片段再可视化，别整集渲染。
2. 音频原声完整嵌入输出，不重采样丢质量。
3. 封面图会等比缩放居中，标题自动描边保证可读。
4. 产物统一进 `outputs/主题名/`。

## 参考来源

音频波形/频谱可视化用 ffmpeg `showwaves`/`showfreqs`/`showspectrum`/`showcqt`，是播客/音频号
上视频平台的标准做法。把各可视化滤镜与封面/标题合成封装成确定性脚本。
