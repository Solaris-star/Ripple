# Ripple SKILL 元数据

| 字段 | 值 |
|------|-----|
| **SKILL 名称** | xhs-redbook-analyzer |
| **所属层** | attribute |
| **来源仓库** | lucasygu/redbook |
| **GitHub 地址** | https://github.com/lucasygu/redbook |
| **上游许可** | MIT；Copyright (c) 2026 Lucas Gu |
| **功能描述** | 小红书数据分析：13个模块（关键词矩阵/热度图/互动信号/创作者画像） |
| **自包含** | 需XHS Cookie |
| **关系说明** | 保留 `redbook` 上游身份与安装键；Ripple 将分析能力组织为只读 Skill，发布与互动写操作交给专门发布能力 |

## Ripple 修改说明

分析模块、命令与技术参考被拆入 `references/` 以便维护；上游 TypeScript 实现及 MIT 来源关系保持明确记录。
