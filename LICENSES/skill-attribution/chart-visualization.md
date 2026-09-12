# Ripple SKILL 元数据

| 字段 | 值 |
|------|-----|
| **SKILL 名称** | chart-visualization |
| **所属层** | produce |
| **来源生态** | AntV（蚂蚁集团数据可视化） |
| **上游 API** | https://antv-studio.alipay.com/api/gpt-vis（gpt-vis 服务） |
| **参考仓库** | https://github.com/antvis/GPT-Vis |
| **功能描述** | 调 AntV 远程 API 生成静态图表图片 URL，覆盖 25+ 图表类型 |
| **自包含** | 是（仅依赖 curl 调用远程 API，无本地资源） |
| **profile_aware** | false |
| **关系说明** | Ripple 仅封装调用约定与图表选择指南，不复制 GPT-Vis 上游源码；远程 API 服务条款由服务提供方负责 |

## 致谢

图表能力基于 AntV 开源生态（antvis）与其 GPT-Vis / gpt-vis API 服务，特此致谢。本 SKILL 仅封装调用约定与图表选择指南，未复制上游代码。
