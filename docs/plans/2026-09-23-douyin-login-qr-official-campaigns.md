# Plan: 抖音登录二维码与官方创作活动采集

状态：EXECUTE 已完成（2026-09-23）。用户确认后完成实现、定向修复、真实同步和验证；原计划与执行结果一并保留。

## Goal / Non-goals

目标：复用已绑定抖音账号的 Ripple 独立 Edge Profile，准确采集创作者中心真实活动，接入现有活动广场；补齐登录弹窗的真实二维码及状态回传。

非目标：重新登录或断开用户已绑定账号；报名、领奖、发布；引入付费第三方服务或 Agent 补全；更改其他平台采集频率；重构账号系统或活动广场；读取个人日常浏览器 Profile。

## Success criteria（可验证）

1. 新建/重新连接抖音账号时，平台确实展示二维码才能显示真实二维码；启动、待扫码、待确认、成功、超时/验证失败有对应提示。短信/风控验证继续由用户在平台窗口完成；没有二维码时提供明确说明，不能空白等待。用户当前已绑定账号保持可用。
2. 精确识别官方活动列表响应。分类接口中的 39 个标签全部被排除，真实列表里的每个有效 activity_id 独立保存；相同名称/相同首页链接不得把不同 ID 合并。
3. 正确映射 show_name、activity_id、jump_link、challenge_ids 等真实字段。show_start_time/show_end_time 先作为来源展示时间保存；未确认的展示时间不能直接充当报名/投稿截止时间。
4. 核实时间窗口与列表范围，不把首页一次返回的 13 条宣称为抖音全部活动。遵循实际接口的分页/时间窗口/类别语义，达到安全上限或未完整读取时显式说明。
5. 官方详情有明确原文才填参与条件、作品要求、奖励、获奖规则；缺失/图片规则/仅 App 可见分别标记，不生成条件和奖励。
6. “抖音”筛选、关键词搜索、排序、分页、按账号过滤正常；重复采集不生成重复记录；刷新只处理抖音，不触发其他平台或收费来源。
7. 登录失效、验证码、账号占用、接口业务错误、解析不匹配与真正空列表可区分。失败时保留上次成功数据，不能把失败报告成成功采集 0 条。
8. 通过新增回归、相关既有测试、前端构建及浏览器验证。执行结束自动重启当前 Ripple 服务，核对实际接口与最终前端资源，仅提交本任务改动，不推送。

## Context & assumptions（含补全）

仓库：D:\AI\Ripple\app，检查时 main / 7588db8。工作区有 41 个既有变更条目，必须保留；web/app.py、ripple/api.py 等共享文件只能按本任务 hunk 提交。

### 已确认事实

- 本机账号 API 返回一个已连接的抖音“个人账号”，使用 msedge 独立 Profile。本轮没有发起账号重新登录；对既有 Profile 的有锁只读访问获得官方 JSON 响应。
- 登录操作结果中不存在 qr.png 和 status.json。ripple/native_worker.py:78-107 的抖音分支直接返回，绕过后面 126-139 行的状态/二维码约定；ripple/douyin_browser.py:134-154 仅打开平台窗口等待，未导出二维码。前端 Accounts.tsx:266 只有 qr_available 为真才显示二维码。
- 上次采集原始结果是 39 个分类，例如“综合”“随拍”“文化教育”“美食”“摄影摄像”，没有独立链接/活动时间。
- 官方 GET /web/api/v2/creator/activity/tags/query 返回 query_tags（39 个 id/name），旧通用解析器错误接收它们。
- 同一页面真实调用 GET /web/api/v2/creator/activity/pc/list，HTTP 200、status_code=0，本次返回 list 共 13 项。已观察到字段 activity_id、show_name、show_start_time、show_end_time、challenge_ids、jump_link、jump_type、query_tag 等。
- 旧 _candidates 认识 title/name/activity_name，不认识 show_name，故本次真实列表没有被识别。
- 本次真实列表样例含“2026抖音创作者大会”（至少两个不同 activity_id）和“活成自己生命里的光”等。列表项并不都具有普通网页详情链接。
- 官方列表请求实际携带 start_time、end_time，认证/签名参数仅留在本机浏览器，不打印、不保存到计划。响应根没有已观察到的分页字段；完整范围待核实。
- 已观察到 jump_link 的 https://api.amemv.com/magic/eco/runtime/release/... 和 sslocal://webview?url=... 形态。当前来源仅允许 douyin.com 域且没有受限解包逻辑，详情地址会丢失。
- ripple/campaign_sources.py:1623-1660 对无链接项使用同一个 creator.douyin.com 首页；web/app.py:4252-4284 会据相同 URL 合并。现有抖音广场仅剩“摄影摄像”一条，rule_version=39、saved=false，证实发生了连续错误合并。
- 当前免费抖音来源约每小时同步；TikHub 未启用。本任务保持现有采集计划与收费开关。

### 补全与默认方案

- 同时处理二维码回传及活动适配，不要求用户解绑重绑。
- 使用浏览器自身发出的真实请求/同会话只读请求，不自行制造签名、绕过验证或复制 Cookie 到第三方。
- show_* 时间的业务含义待详情/页面核实，默认保留原始展示时间，不误标参赛期限。
- 分类污染数据默认先备份并隔离；只有能够明确证明是本次旧适配自动生成、没有人工修改/收藏/业务引用的记录才允许精准修复，禁止清空全部抖音数据。

## Options considered → 推荐

A. 沿用宽泛 URL 关键词监听和递归 id/name 猜测：改动少，但会继续混入标签、任务摘要等数据；不采用。

B. 官方列表专用解析 + 现有独立 Profile + 受限详情读取：能够按实际字段校验、按稳定 ID 去重，无新增收费依赖；推荐。

C. 使用 TikHub/模型检索替代：新增额度成本，且不能解决当前二维码和错误合并；本轮不启用。

## Phases / Steps

### Step 1: 登录二维码与状态回传

- 涉及 ripple/douyin_browser.py、ripple/native_worker.py、必要时 ripple/accounts.py、web/frontend/src/components/workspace/Accounts.tsx。
- 给抖音登录分支接入既有 operations/<operation_id>/qr.png + status.json 协议，复用现有授权二维码访问接口。只截取二维码元素，不截图整页账号数据；轮换二维码需要前端更新版本，结束/超时后旧码不可再访问。
- 修正弹窗提示：启动时不能继续只显示“尚未登录”；平台要求短信/验证时给出“到独立窗口完成”的说明。登录完成后隐藏二维码。
- 加强已登录判断，不能仅以页面没有“扫码/手机号”文字或停留在 creator 域为登录成功，避免加载中页面误判。
- 验证：隔离测试覆盖 qr_ready/轮换/扫码后等待/超时/成功及其他平台兼容；使用独立临时测试环境检查页面状态，不让已绑定账号退出登录。

### Step 2: 官方活动接口专用读取

- 涉及 ripple/douyin_browser.py、tests/test_douyin_campaigns.py。
- 仅接收官方活动列表的允许 host/path/schema，排除 tags/query 和 mission/user_summary 等无关响应。核验 status_code 与 list 结构。
- 解析 activity_id/show_name 等实际字段，明确跳转类型及来源展示时间；保留 stable IDs、列表排序、抓取时间和范围信息。
- 确认 start_time/end_time 含义及实际页面的全部活动入口/分类请求；沿已观察到的协议按需有界读取，真实空列表与解析失败分开。
- 验证：39 个分类零入库，真实样例可识别；同名不同 ID 保留、业务错误/坏结构/分页边界/限制提示正确。

### Step 3: 详情链接与活动规则

- 涉及 ripple/douyin_browser.py、ripple/campaign_sources.py，必要时活动类型与卡片展示。
- 在已观察到的官方 jump_link 范围实现受限 sslocal webview 解包；仅访问确认归属的 HTTPS 官方域及允许重定向，限制深度、响应大小和耗时，拒绝私网/任意网址。
- 话题/挑战型活动保留 challenge_ids；只有核实官方可打开地址后才提供网页链接。无法网页化时明确标记 App 内查看，不能统一用首页冒充详情。
- 从可读官方原文提取参赛要求、奖励、规则和截止时间；无法确认的字段留空并带来源/待核实状态，不把“账号可见”推导为“资格已满足”。
- 验证：真实样例的一致性，以及外域/私网/嵌套跳转拒绝、缺失详情、图片规则和仅 App 活动的展示。

### Step 4: 来源接入、去重与误采修复

- 涉及 ripple/campaign_sources.py、web/app.py、tests/test_campaigns.py、tests/test_campaign_sources.py。
- 将抖音真实稳定 ID 作为合并主键；不同 ID 不能因同名、公共首页或共享会场链接合并。其他平台行为保持不变。
- 来源状态显示有效活动数和实际入库结果，分类响应不算成功活动数据；区分登录连接状态与来源采集健康状态。
- 对已确认的“摄影摄像”污染记录做一次备份、人工字段/收藏/业务引用检查后精准隔离，不批量删除同名正常活动。
- 验证：重复刷新幂等、同名 ID 隔离、人工导入保留、账号可见性隔离、失败保留旧快照、零付费调用。

### Step 5: 用户流程验收与交付

- 先跑离线回归，再仅对已连接抖音账号做一次真实免费同步；不触发全平台刷新。对照此次官方返回的 ID 集合、过滤原因及有效入库数量。
- 浏览器检查账号仍连接、抖音活动卡片格式、详情、关键词搜索/分页/筛选、刷新范围、失败提示；截图避免账号隐私信息。
- 新增可重复只读 smoke 验证，不执行报名/领奖/发布。前端构建、定向测试、diff 审核通过后自动重启并验证实际运行版本。
- 提交本次业务与回归改动；对共享脏文件只暂存本任务 hunk，保留其他会话内容，不推送。

## Touch list

Will change（待确认后）：ripple/douyin_browser.py、ripple/native_worker.py、ripple/campaign_sources.py、web/app.py 的抖音合并逻辑、Accounts.tsx 的抖音登录提示；必要的账号状态投影、类型、活动详情状态展示及对应测试/只读 smoke。

Will NOT touch：当前账号凭据/个人浏览器 Profile、其他平台采集器与频率、付费开关、主稿/聊天/侧栏等既有未提交改动、发布/报名接口。

本轮已创建：本计划文档。

## Risks / rollbacks

- 已登录页面也可能要求额外验证或出现账号占用：停止该次读取，提示用户处理，保留之前快照，不强杀用户浏览器。
- 官方字段/时间窗口/跳转类型仍可能变化：严格 schema 检查并明确失败；可回滚本任务变更，不用标签/首页文本回退冒充活动。
- 详情可能仅在 App 或图像内：保留来源与缺失说明，不承诺自动补齐不可得规则。
- 污染记录涉及业务引用时不删除，保留备份并报告需要的定向修复决策。
- 当前工作区不干净：执行前创建检查点、保存本任务前后 diff；回滚仅限本任务内容。

## Open questions

用户已确认执行。列表窗口已核实为当前账号创作者中心活动日历的当月范围；展示日期保留为展示日期。App 内详情及图片里的规则仍需核实，不填写未经确认的奖励、条件或截止时间。

## Execution results（2026-09-23）

- 官方只读接口 `/web/api/v2/creator/activity/pc/list` 在北京时间 2026-09-01～2026-09-30 的窗口返回 13 条；通过一次 `platforms=[douyin], force=true, allow_paid=false` 的真实同步，13 个官方 ID 全部独立入库，分页 10 + 3。两条同名“2026抖音创作者大会”保留不同 ID。同步过程约 12 秒。
- 本次 11 条活动没有可打开的网页详情，2 条官方详情包含图片且未找到可直接提取的规则，均明确标注；没有把 show_* 展示日期当投稿截止日期，也没有用平台首页冒充详情。
- 登录使用已有独立 Profile，增加真实二维码元素识别/截图、状态原子写入、二维码版本更新和结束清理。匿名临时浏览器实测可取得真实二维码，超时后图片清除；前端使用模拟账号及合成二维码验证启动、二维码轮换、扫码等待、超时及成功，未要求已绑定账号重新扫码。
- 修复前检查“摄影摄像”记录无收藏、人工规则或业务引用；服务暂停期间仅隔离该 ID。完整原始文件保存在 `.ripple-private/outputs/recovery/campaigns-before-taxonomy-0dfad7646640532d8ecc3a5c08cb34c2bab9a0464c90a7d541124a6c198008e7.json`。其他 313 条记录在修复时保持不变，随后抖音同步也未改动其他平台条目。
- 后端相关回归 225 项通过、无失败或跳过；前端单测 12 项通过，定向 lint 无警告/错误，生产构建成功。浏览器 5 组场景通过，无页面异常、无真实登录/刷新等写请求。保留两项现有测试依赖弃用警告。
- RippleLocalService 已重启，运行 PID 从 49124 变为 123704；后续确认新接口及最终前端 `index-ZkB_4Ih1.js` 已实际提供，用户抖音账号仍 connected。
- 保留免费抖音来源每小时同步、TikHub 关闭和其他平台采集计划。没有启动收费模型，没有报名、发布或领奖。
- 本轮前置 checkpoint 工具返回错误，改用 `artifacts/douyin-official-20260923/baseline/` 保存精确原始文件、SHA 和工作区状态；提交按该基线生成本任务 hunks，既有未提交内容不混入。

### Evidence / repeatable checks

结果记录：`artifacts/douyin-official-20260923/live-sync-report.json`、`browser-report.json`、`validation-summary.json`、`tests.xml`。

离线回归：`tests/test_douyin_official_adapter.py`（严格列表结构/ID/时间、域名及跳转安全、规则来源、二维码轮换及访问边界、账号范围和误采隔离），并回归现有活动、账号/服务认证及执行节点测试。

浏览器验证：`python scripts/douyin_adapter_smoke.py`，基于本次已同步的 2026-09 日历样本，不启动真实登录或采集。定向数据修复脚本 `scripts/douyin_taxonomy_repair.py` 默认只预览，应用需要停止服务、明确记录 ID、匹配原文件 SHA、无人工改动/引用，并先保留完整备份。
