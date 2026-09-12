# Contributing to Ripple

感谢参与 Ripple。

## 开发环境

按 README 运行 `setup.ps1` 或 `setup.sh`。安装器只操作仓库内环境，不应要求贡献者覆盖全局 Agent 配置。

## Pull Request 要求

1. 改动保持聚焦，避免顺手大规模重构。
2. 新功能同时给出可观察的测试或验证步骤。
3. 平台真实写操作必须继续经过用户确认和发布预检。
4. 网络测试使用 MockTransport、本地测试服务器或只读公共接口；CI 不允许真实发帖、评论、删除或登录个人账号。
5. 不提交 `.env`、Token、Cookie、AppSecret、Browser Profile、真实素材或个人画像。
6. 新增第三方源码、Skill、模板或素材时，在 `LICENSES/` / `THIRD_PARTY_NOTICES.md` 记录来源、版本/commit 和许可证。无法确认再分发授权的内容不能直接进入仓库。
7. 参考其他项目的思路时应独立实现，并在需要时记录参考来源；不要把未知许可证代码改名后当作 Ripple 自研。

## 验证

```bash
python -m pytest -q
python scripts/ripple_smoke.py
```

```bash
cd web/frontend
npm ci
npm run build
npm run lint
npm audit
```

提交前确认 `git diff --check` 无错误。

## Brand / integrations

公开产品、Python 包、Skill 路径和存储命名统一使用 **Ripple**。第三方名称只应出现在真实集成或许可归属上下文中，不得作为 Ripple 自身的目录、包或协议命名。