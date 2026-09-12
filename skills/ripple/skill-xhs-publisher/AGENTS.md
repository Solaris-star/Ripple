# 仓库指南（xhs-publisher）

## 实现现状（2026-07 重写）

本 SKILL 的小红书发布使用 Playwright + 持久化登录态，支持 headless 运行。旧 CDP 脚本已删除。

- 执行入口：`../../shared/scripts/xhs_publish.py`（不在本目录，属跨 SKILL 共享脚本层）。
- 本目录只保留：`SKILL.md`（流程/约束）、`references/commands.md`（命令样例）、
  `config/accounts.json.example`、`public/`、`docs/`。

## 开发/冒烟命令（CWD=项目根）

```bash
python ../../shared/scripts/xhs_publish.py selftest   # 离线自检（选择器/标题算法/参数）
python ../../shared/scripts/xhs_publish.py check       # 验 playwright + chromium
python ../../shared/scripts/xhs_publish.py login       # 有头扫码登录（持久化）
python ../../shared/scripts/xhs_publish.py plan ...    # dry-run 预检
python ../../shared/scripts/xhs_publish.py publish --exec --headed ...   # 首次校验选择器
```

## 维护规范

- 小红书改版导致定位失败：**只改 `xhs_publish.py` 顶部 `SELECTORS` 字典**（单点集中，每条标了
  参考源），不要散改流程逻辑；先 `--headed` 观察再改。
- 反检测：脚本已内置 `--disable-blink-features=AutomationControlled` + 逐字符输入 + zh-CN；
  新增交互优先用 Playwright 原生动作，避免可被检测的大段 JS 注入。
- 安全：禁止提交真实 Cookie / 账号令牌；新登录态默认在 `~/.ripple-browser-profiles/`，旧目录仅兼容读取，均不入库。
