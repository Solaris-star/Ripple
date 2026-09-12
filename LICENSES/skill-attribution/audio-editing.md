# Ripple SKILL 元数据

| 字段 | 值 |
|------|-----|
| **SKILL 名称** | audio-editing |
| **所属层** | produce |
| **来源类型** | 自研 |
| **原始来源** | Ripple 自研；封装共享脚本 `skills/shared/scripts/audio_ops.py`，通过 subprocess 调 ffmpeg/ffprobe |
| **参考项目** | FFmpeg — https://ffmpeg.org（trim/loudnorm/afade/atempo/concat 等滤镜与 ffprobe 探测）；EBU R128 loudnorm 响度标准（社媒 -14 LUFS） |
| **许可** | Ripple 自研/重写部分：Apache-2.0；外部依赖、在线服务与参考项目保留各自许可/服务条款；原记录 （FFmpeg: LGPL/GPL） |

> 整理时间: 2026-07-23
> 用途: 来源溯源与致谢
