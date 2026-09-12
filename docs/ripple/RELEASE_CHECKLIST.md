# Ripple GitHub Release Checklist

## 1. Source tree

- [ ] `git status --short` clean
- [ ] 默认发布分支指向已验证的 Ripple commit
- [ ] README 的 clone/start 命令在仓库根可执行
- [ ] `.env*`、`.venv/`、`.ripple-private/`、真实 outputs、Cookie/Profile 未被跟踪
- [ ] `git diff --check` clean

## 2. Tests / build

- [ ] `python -m pytest -q`
- [ ] `python scripts/ripple_smoke.py`
- [ ] `cd web/frontend && npm ci && npm run build && npm run lint`
- [ ] `cd web/frontend && npm audit`
- [ ] `cd skills/ripple/skill-xhs-analyzer && npm ci --ignore-scripts && npm run build && npm audit`
- [ ] Python 依赖运行 `pip-audit`（CI 会执行）

## 3. Security

- [ ] 密钥扫描覆盖当前树和拟发布历史
- [ ] 没有真实 Token / Cookie / AppSecret / 私钥 / Browser Profile
- [ ] Server 首次初始化要求 `RIPPLE_BOOTSTRAP_CODE`
- [ ] 首个 Owner 创建后移除初始化码并重启
- [ ] 公开 Server 位于 HTTPS 反向代理后，Trusted Host / Public Origin 正确
- [ ] Member 无法执行环境安装、账号凭据配置和 Agent 控制面变更
- [ ] 素材接口无法读取/删除 `_ripple`、`_sessions`、`_runtime`、`_inbox` 等内部状态

## 4. Publishing safety

- [ ] 真实写操作均需要显式确认
- [ ] 远端响应不确定时不会自动重发
- [ ] 微信草稿 / 发布回执的远端 ID 在本地持久化失败时仍被保留并进入人工核对
- [ ] Blog 修改 OpenAPI Origin 时要求重新输入 Token
- [ ] 测试没有真实社交平台写操作

## 5. Licensing / branding

- [ ] `THIRD_PARTY_NOTICES.md` 与 `LICENSES/` 一致
- [ ] 移植/复制组件均有可验证的许可证；不能确认授权的内容已 clean-room 重写或排除
- [ ] 第三方版权/归属声明未因品牌清理而删除
- [ ] 产品入口、安装器和公开包元信息使用 Ripple
- [ ] Python 包、Skill 路径、存储键和公开目录均使用 Ripple 命名；旧产品 namespace 不进入发行树
- [ ] 不包含无运行用途的第三方作者宣传资产
- [x] AGPL 血缘的抖音发布 Skill / 脚本已从首个公开发行树排除；若未来恢复抖音直连发布，需先完成独立许可审查或 clean-room 实现

## 6. Dependency advisories

- [ ] 主前端 `npm audit` 无已知漏洞
- [ ] 独立 Node Skill `npm audit` 无已知漏洞，或有书面隔离/风险接受
- [ ] Python `pip-audit` 无未处置漏洞

## 7. Clean install smoke

在一个新的临时目录/虚拟环境中验证：

- [ ] wheel/sdist 能构建
- [ ] wheel 能安装且 `ripple --help` 可运行
- [ ] `setup.ps1` PowerShell 语法可解析
- [ ] `setup.sh` 通过 `bash -n`
- [ ] 安装器不会全局安装或修改用户 Agent

## 8. Git history decision

- [x] 首个公开 GitHub 仓库使用**净化的公开发行历史**：从最终验证树创建单独的公开根提交，不把旧开发提交、大型演示媒体或本地备份标签推送到公开 `origin`。
- [x] 执行历史切换前已创建本地备份标签 `backup-pre-public-release-20260912`，旧开发分支仅在本机保留。
- [x] 公开 `origin/main` 推送后已复核：远端只发布 `main`，无旧开发分支/备份标签；可达历史仅包含公开发行根及后续公开提交。

旧开发历史不作为公开发行历史的一部分。