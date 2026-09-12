# Ripple SKILL 元数据

| 字段 | 值 |
|------|-----|
| **SKILL 名称** | xiaohongshu-skills |
| **所属层** | publish |
| **来源仓库** | white0dew/XiaohongshuSkills |
| **GitHub 地址** | https://github.com/white0dew/XiaohongshuSkills |
| **上游许可** | MIT；Copyright (c) 2026 angiin |
| **功能描述** | 小红书自动发布/评论/检索工作流 |
| **自包含** | 需要小红书Cookie |
| **当前实现参考** | xpzouying/xiaohongshu-mcp（Apache-2.0），用于当前 Playwright 发布器实现参考 |

## 实现重写（2026-07 参考 xiaohongshu-mcp）

发布实现从旧 **CDP-to-真实Chrome** 栈（`cdp_publish.py` 7000+ 行，headless 环境不可用，已删除）
**整体重写为 Playwright 版** `../../shared/scripts/xhs_publish.py`（headless 可用）。

- **参考来源**：[xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)（Go/go-rod，作者称跑一年未封号）——**仅参考其实现代码**，未引入 Docker/MCP 依赖。
- **移植的健壮技巧**：发布成功校验（URL 离开 /publish/publish）、逐图上传等预览、视频等处理完成、话题联想真绑定、新旧发布按钮兼容、遮挡检测+移弹层、DOM 长度校验、逐字符输入反检测。
- **本次范围**：图文/视频发布 + 登录。检索/互动（feeds/search/comment/like）选择器参考里有，列为后续。
- **旧来源存疑辨析见下**（保留历史），但当前实现已是我们基于 xiaohongshu-mcp 的 Playwright 移植。

## 来源血缘

收录时发现两处来源标注不一致：

- 本 META 原记：`white0dew/XiaohongshuSkills`
- SKILL.md frontmatter `metadata.source` 及正文标题：`Angiin/Post-to-xhs`

**核实结论（选定其一并注明理由）：主来源 = `white0dew/XiaohongshuSkills`；上游祖先 = `Angiin/Post-to-xhs`。**

理由：
1. 现有 SKILL.md 的命令集（`get-login-qrcode` / `content-data` / `get-notification-mentions` / `note-upvote|bookmark` 等发布+互动+数据全套）与 white0dew/XiaohongshuSkills 的功能描述（自动发布/评论/检索 + 二维码导出 + 内容数据看板，~3.2k star）完全对应，而非 Angiin 精简版。
2. 历史收录版本曾包含作者标识资产 `public/whitedew.jpg`；该文件未被 Ripple 运行时引用，已在开源收尾中从发行树移除，来源记录仍保留于本说明。
3. `Angiin/Post-to-xhs`（现已归档并迁移）是 white0dew fork 的上游祖先，因此 frontmatter/标题保留了旧名 —— 这是 fork 未改名所致，而非收编源本身。

已在 SKILL.md frontmatter 补记 `source: white0dew/XiaohongshuSkills` + `ancestor: Angiin/Post-to-xhs`，两者均如实保留。

### Ripple 修改说明

- 发布执行已重写为共享 Playwright 发布器，保留来源血缘与上游许可说明。
- 本 Skill 专注发布与互动；分析能力交给 `skill-xhs-analyzer`。
- 命令参考拆入 `references/`，不改变来源与版权归属。
