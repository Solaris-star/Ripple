# Plan: 小红书完整活动规则解析与字段纠错

状态：EXECUTED（结构化规则解析与字段纠错完成；纯图片规则明确标记待视觉识别）。业务基线：6a09cf2。

## Goal / Non-goals

目标：补齐官方页面已经提供但 Ripple 漏采、漏映射的参赛作品、参与条件、奖励档位与奖励条件；每个字段保留可以复核的原文与来源。修正宣传语充当奖励、任务预览充当资格结论的错误。

非目标：不重做列表、默认/最新排序与10条分页；不改其他平台采集；不操作报名、关注、抽奖、领券、收藏或发布；不导出 Cookie、不修改登录隔离、不自动付费调用模型。来源确实没有说明的规则不猜填。

## Success criteria（可验证）

- 首先核对用户截图的4个活动：Pick你的每周时刻、去阿秋店里坐坐、秋天自然要手工、过节就过音乐节。逐字段对照当前生效的官网证据，未完成解析与原文未说明必须区分。
- 去阿秋店里坐坐的已观察活动奖励可正确显示：2000流量券、定制便携餐具包、定制阿秋薯公仔；里程碑进度门槛分别为10、20、30。必须再次核对最新页面，不能把进度数猜成天数或投稿篇数，不能承诺必然领奖。
- task preview出现post_note不再自动产生“图文/视频均允许”或“符合全部参赛资格”的结论。允许使用“可查看任务/有投稿入口”等有证据的描述，资格结论单独判断。
- 奖励卡片不能显示“秋天开了店喊你来坐坐”等无奖励含义的宣传语；总奖池、单人奖励、积分、流量、实物分别保存含义。
- 页面配置中的未启用组件、历史模板、示例文案以及投稿内容流不混入活动规则。每条规则有来源类型、页面/活动ID、字段路径或定位信息、原文及采集时间。
- 修复后对旧解析缓存做版本化更新；只替换可证明来自旧解析器的错误派生字段，保留用户确认字段、收藏、选题引用、官方列表顺序。
- 详情未捕获/登录或风控状态/原文未说明/规则图片待解析具有不同状态，不再统一标记为“平台没写”。列表已同步与规则已核验分开表达。
- 执行定向和全量回归、前端构建及真实卡片字段验收；没有证据的字段不因测试通过就宣布完整。

## Context & assumptions

### 已核查代码

- ripple/xhs_browser.py:156–157把activity_reward同时赋给description和reward_summary。
- ripple/xhs_browser.py:229、231直接返回eligibility=[]和winning_conditions=[]，目前没有这两项的提取逻辑。
- ripple/xhs_browser.py:192–238把post_note的存在推导为两种作品格式，并在enabled/status为1时推导eligible。这是本地推导，不是平台返回的完整资格结论。
- ripple/xhs_browser.py:660–681只监听preview_task_list，固定等待后解析，未读取页面DSL和milestone/info。body参数没有实际参与规则抽取。
- ripple/campaign_sources.py:1251每轮详情尝试预算12；从列表顺序开始遍历，失败不缓存可能反复占用前部预算。
- ripple/campaign_sources.py:1267、1275保留宣传语为奖励摘要；1290将来源统一标记verified。
- CampaignsPage.tsx:987–990奖励优先prizes其次reward_summary，不读取reward_rules中的具体奖励；994的“查看详情”仅打开已有对象，没有触发官方详情补全。

### 本轮运行记录

检查时历史库252条小红书记录；参与条件与获奖条件均为0条非空。最近一次同步（2026-09-22T11:36:51Z）官方列表250条，附带任务详情30条，详情尝试12次。列表数量不能用来宣称规则解析覆盖率。

“去阿秋店里坐坐”的缓存reward_rules已包含“带话题连更投稿，赢取流量券+实物奖励”；prizes仍为空，卡片奖励回退到“秋天开了店喊你来坐坐”。

### 两个官方详情页的新证据

通过当前账号既有隔离Profile、native_worker及browser-operation.lock，只读打开现有活动链接，未修改生产解析器或Campaign记录。诊断操作回执：5148c3e1c5ca4ad38e94299619bd1230、58d37674e9834e579cd42cf8628fdd41。

去阿秋店里坐坐：activity_id=44728，page_id=2ab22673e060473a9d34fc1b51472ca4，resource_instance_id=327583。

- 官方HTML中的window.__SETUP_SERVER_STATE__.DSL是对象，含componentsMap、componentsTree、pageConfig。
- DSL.componentsTree[0].children[12].props.config.prizeInfo列出2000流量券、定制便携餐具包、定制阿秋薯公仔。
- GET /api/sns/v1/activity_platform/milestone/info（edith.xiaohongshu.com）返回mile_stone_info，每项的mile_stone.point_number为10/20/30，reward_info.title与上述奖励逐项对应。
- DSL另一任务组件含“带话题发布点赞≥25的家门口探店笔记”等配置。尚未确认该组件是否当前启用及对应哪项奖励，不能直接作为全活动通用门槛。
- /api/resource/delivery/page/<page_id>返回page_id、page_state；其状态用于核验活动页面可用性，不能当作完整规则。
- 部分prize_draw/query_play请求返回406，页面含“你还没有登录哦”提示。因此预览任务可读取不能证明领取/抽奖等功能已授权或资格已满足，不尝试绕过。

Pick你的每周时刻：activity_id=43010，page_id=5097c6dc32bc479caa4d2ebb368eb0af。

- DSL有Banner图片、按钮、精选笔记等组件，本次没有观察到preview_task_list，也没有在DSL文本里找到明确规则字符串。
- 必须继续检查活动Banner物料与可访问的规则入口。不能据此断言官方没有规则；不把精选笔记或其作者内容当作官方规则。

### 补全的默认取舍

- 先接官方结构化详情和当前生效的DSL组件，文案不足再读取DOM；确认规则只在图片中时，才按需做视觉识别并保留物料证据。
- 将任务描述、奖励档位、评选条件、账号资格分离。进度门槛保持平台原始单位，单位证据不足标记待确认。
- 详情读取采用显式单活动刷新、缓存和有界批次。翻页本身继续只读本地；收藏和当前关注的活动可作为后续详情优先项，不给250个活动反复打开浏览器。

## Options considered → 推荐

A. 只调整UI状态与宣传语：成本低，可以减少误导，但已提供的奖励档位仍然缺失。
B. 官方详情证据解析 + 精准映射纠错 + 旧缓存版本化更新：推荐。沿用现有隔离架构和Campaign字段，完成真正的规则适配。
不采用让Agent根据标题、话题或经验填满空白。

## Phases / Steps

### Step 1：建立完整详情读取与可追溯规则解析

文件：ripple/xhs_browser.py、ripple/xhs_ops.py、tests/test_xhs_ops.py。

读取指定活动URL中的DSL当前启用组件、preview_task_list以及对应page/instance/component的milestone/info。严格验证请求来源和响应业务状态，不合并另一活动或另一奖励组件。分别解析任务、奖品、进度规则、明确作品要求及资格证据；Banner素材仅在结构化内容不足时按需识别。以4个截图样本及脱敏fixture测试不同组件、缺失、错误、隐藏模板和多档奖励。

### Step 2：映射纠错、字段证据与旧缓存升级

文件：ripple/campaign_sources.py、web/app.py、tests/test_campaign_sources.py、tests/test_campaigns.py。

将宣传描述留在summary；从经核验的奖品/里程碑/任务描述提取prizes和reward_rules。赢奖/领取条件与账号参赛资格分别处理。去掉根据post_note机械推断作品形式和eligible的规则。对source evidence、解析版本与失败状态作必要扩展；版本升级后按来源精准重解析，保留人工确认字段。截图4张卡逐字段检查结果与原文，不重排官方活动清单。

### Step 3：详情刷新入口与真实验收

文件：web/app.py、web/frontend/src/lib/api.ts、web/frontend/src/components/CampaignsPage.tsx及相应测试。

详情抽屉提供受限、显式的规则读取/重试入口，复用单账号串行锁和缓存。卡片区分“列表已同步/规则待解析/已解析/原文未说明/读取失败”；保留卡片对齐、默认/最新排序和每页10条。不把整体来源verified扩展为所有字段已核验。实际验证奖励档位、格式与资格证据，测试缓存命中、失败重试、人工字段保护和分页不变。

## Touch list

Will change（确认后）：ripple/xhs_browser.py、ripple/xhs_ops.py、ripple/campaign_sources.py、web/app.py、web/frontend/src/lib/api.ts、web/frontend/src/components/CampaignsPage.tsx、tests/test_xhs_ops.py、tests/test_campaign_sources.py、tests/test_campaigns.py；必要的脱敏fixture。

Will NOT touch：其他平台采集、官方活动排序与分页、账户凭据存储、发布/互动/领取动作、已有选题和本地收藏。

## Risks / rollbacks

- DSL可能存在未启用组件/旧模板：以实际页面启用状态和关联运行时数据交叉验证，不递归扫到规则词就入库。
- 登录/风控响应：失败明确标记，保留已有规则和原文；不把任务预览可见当作全功能可用。
- 规则图片识别误差：记录图片来源，对数字、单位、奖池/单奖含义逐项核验，低置信项留待确认。
- 旧记录缺少字段级来源：无法证明为程序推断的已确认值不自动清除；保留旧版本快照供回滚。
- 不保证所有活动都有资格或评选规则；允许真空字段，必须区别“尚未解析”和“完整核对后原文未说明”。

## Execution result

状态：EXECUTED（结构化详情证据、字段纠错、单活动重读、状态区分已完成；纯图片规则保留为 `needs_visual_review`，未用不可靠 OCR 猜填）。

- 详情缓存升级到 v3，旧解析缓存自动失效。批量同步与单活动重读均使用同一账号隔离 Profile 和 browser-operation.lock。
- 当前解析来源包括：
  - `preview_task_list`：投稿/关注/点赞等任务与话题；
  - 当前生效的 `window.__SETUP_SERVER_STATE__.DSL.componentsTree`：奖品配置、明确发布/投稿/创作要求及显式资格/评选文案；
  - `/api/sns/v1/activity_platform/milestone/info`：进度阈值和对应奖品。
- 不再遍历 componentsMap，避免未挂载/历史组件污染规则；DSL任务仅把明确含“发布/投稿/创作”的条目归入参赛作品要求。
- 不再根据 post_note 推断“图文/视频均可”，也不再据任务可见推断 `eligible`。任务可见只保留为证据，资格默认为 unknown，除非平台存在更明确结论。
- `activity_reward` 保留为宣传摘要；只有含奖励语义的文本才可作为概览奖励。详情存在结构化奖品时，卡片使用真实奖品，不再显示“秋天开了店喊你来坐坐”这类宣传语作为奖品。
- 字段证据记录来源、DSL路径/原文或里程碑原文；列表“来源已核验”和详情“规则已解析/待视觉识别/读取失败”分开展示。
- 新增 `POST /api/campaigns/{cid}/xhs-detail` 与详情抽屉“读取/重新读取小红书规则”，只读取单个活动，不让翻页触发浏览器。
- 人工确认的资格与作品规格继续优先；详情读取失败不覆盖已有资格；普通列表刷新不会把已解析详情状态降级。
- 官方默认/最新排序与每页10条分页未改变。

### 真实账号验收

通过当前绑定账号既有隔离 Profile 对用户截图中的4个活动执行只读详情读取：

- Pick你的每周时刻：结构化任务/奖品/里程碑未发现，但当前页面存在 Banner 等图片物料，因此状态为 `needs_visual_review`；未宣称“官方没写规则”，也未猜填图片内容。
- 去阿秋店里坐坐：
  - 奖品：2000流量券、定制便携餐具包、定制阿秋薯公仔；
  - 官方里程碑阈值：10 / 20 / 30，保留“进度单位以活动页说明为准”，不猜成天数/篇数；
  - 参赛作品要求最终只保留“连更打卡任务”及3条明确“带话题发布点赞≥25…”的投稿要求，点赞/评论/分享任务不再混入作品要求；
  - 作品格式未由 post_note 推断，资格仍为 unknown。
- 秋天自然要手工：读取到“发布秋日手工笔记”及多条秋日手工投稿方向、积分任务；当前未发现结构化奖品/获奖门槛，不猜填。
- 过节就过音乐节：
  - 奖品：redclub出门走走贴纸、一起冲浪薯队长公仔、说走就走旅行包、jellycat玩偶（随机款）；
  - 里程碑阈值：5 / 10 / 15 / 20；
  - 作品格式不猜，资格仍为 unknown。

### Validation

- 定向：74 passed。
- 全量：452 passed，1 个既有 Starlette TestClient deprecation warning。
- 前端 `npm run build` 通过；仅保留既有 >500 kB chunk warning。
- Python编译检查通过。
- 真实详情验证使用只读操作；没有执行报名、关注、领取、抽奖、发布，也没有导出 Cookie/请求头。
- 本轮没有重启当前已运行的 Ripple 服务；提交后需要单独授权重启，当前 UI 才会加载这些新代码。

## Open questions / remaining

- Pick你的每周时刻当前确认规则主要存在于图片物料或未暴露为结构化文本；已标记 needs_visual_review。本轮不引入低置信 OCR 自动填充。
- 其他活动若结构化接口与DSL都不足，也采用相同状态，不把空字段解释成“平台明确没有要求”。
