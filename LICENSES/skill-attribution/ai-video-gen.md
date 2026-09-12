# Ripple SKILL 元数据

| 字段 | 值 |
|------|-----|
| **SKILL 名称** | ai-video-gen |
| **所属层** | produce |
| **来源类型** | 自研 |
| **原始来源** | Ripple 自研（`shared/scripts/ai_video.py`） |
| **参考项目** | 沉淀自 AIDC-AI/Pixelle-Video（多供应商视频生成统一配置：DashScope-Wan / ARK-Seedance / Kling）、LuoGen-AI/LuoGen-agent（数字人口播流程）；各 provider 依据其公开 REST API 文档实现 |
| **依赖** | Python 标准库（urllib/hmac/hashlib，无第三方）；用户自备各 provider 的 API key |
| **许可** | Ripple 自研/重写部分：Apache-2.0；外部依赖、在线服务与参考项目保留各自许可/服务条款；原记录 （参考项目 Apache-2.0 / GPL-3.0，本实现为自研封装） |

> 整理时间: 2026-08-26
> 用途: 来源溯源与致谢

## 说明

文/图生视频 + 数字人首帧驱动，支持 DashScope / ARK / Kling / OpenAI-compatible / Agnes，并保留一个旧版异步 `media/parameters` 协议兼容标识用于迁移既有本地配置。公开发行不内置该兼容协议的组织专用端点、模型或能力假设。异步提交→轮询→下载；具体 provider 能力以配置和探针证据为准。
