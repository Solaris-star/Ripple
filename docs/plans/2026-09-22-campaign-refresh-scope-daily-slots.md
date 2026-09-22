# Plan: 活动广场刷新范围、每日三次采集与活动源调研

状态：PLAN，待用户确认实施。业务基线：a24371c90a53c99991378f63ee900a03830c69bc；本轮开始时 main 工作区干净。

## Goal / Non-goals

目标：移除右上角“Agent 补全缺失规则”入口；手动刷新严格遵守当前平台范围；活动源每天只在09:00、14:00、20:00发起三轮自动采集；核查抖音、公众号、视频号是否有可复用的活动源。

非目标：不改热点雷达采集频率、发布调度、XHS详情解析、官方默认/最新排序及每页10条分页；不删除收藏、选题和历史活动。不自动购买或启用收费服务，不替用户扫码登录，不安装第三方代理证书，不把视频下载/发布项目描述成活动采集适配器。

## Success criteria（可验证）

1. 活动页右上角不再出现“Agent 补全缺失规则”。保留现有单活动操作与运行中任务取消入口，移除仅被删除按钮引用的预览弹窗/无用前端状态，不删除无关后端能力。
2. 全部平台视图刷新所有已就绪活动源；选中一个平台后，页头和空状态刷新只传该平台。按钮分别明确标识“刷新全部活动”或“刷新小红书活动”等。请求不得触发其他平台的采集、规则整理、重试或收费备用源。
3. 当前平台未配置/登录失效/仅支持导入时显示对应说明和配置或重新登录入口，不能触发全部平台刷新。显式空数组、未知平台不能回退成全量采集。
4. 各已启用自动活动源按 Asia/Shanghai 的09:00、14:00、20:00进入采集批次，时间可在UI看见。每个平台每个时段至多自动发起一次；平台实际请求在批次内按现有锁和限额执行，三轮不等于只有三个HTTP请求。
5. 手动刷新可额外执行但不移动下一固定时段。自动失败保留历史结果，等待下一固定时段或用户单次重试；不按短间隔无限补采。进程重启不重复已认领批次，也不追补关机错过的多轮采集。
6. 只在对应平台成功刷新后，在同一批次预算内处理该平台规则队列。无来源到期时的scheduler tick不能额外启动X模型请求；刷新小红书/抖音不能启动X规则整理。
7. 来源卡片显示“每日09:00 / 14:00 / 20:00（北京时间）”和实际下一次采集时间/倒计时。后端next_sync_at、last_sync.next_run_at与新调度一致；遗留的30分钟/1小时字段不能影响当前展示或执行。
8. 验收覆盖原先的串平台场景、未配置抖音空页面、固定时刻边界、跨日、重启、失败和并发。定向/全量测试、前端构建完成后commit，自动重启本地Ripple并检查页面/API；用户已授权以后改完自动重启，无需再次询问。

## Context & assumptions（含补全部分）

### 当前代码与运行态证据

- CampaignsPage.tsx:496–502：refreshNow直接取sources中全部automatic平台，未使用platformFilter限制。页头855及空状态988共用此函数。
- CampaignsPage.tsx:858：待移除的按钮实际打开B站批量规则补全预览；单活动B站按钮另在约1032/1134行。
- web/app.py:4808及campaign_sources.py:1473：两层“platforms or 默认列表”将显式[]解释成全平台，必须同时纠正。
- web/app.py:4812：任意平台刷新后无条件尝试_start_x_enrichment_batch；4774–4784：无来源到期的scheduler tick同样会启动X规则整理。tests/test_campaigns.py:595–603甚至将该行为作为旧预期，须明确更新。
- campaign_sources.py:31–36、716–824：当前按上次尝试时间加固定秒间隔；B站1800秒、X7200秒、小红书/抖音3600秒。next_sync_at、_record_sync.next_run_at、_recent、due_platforms都依赖旧间隔。
- ripple/api.py:800–815：共享scheduler先运行service.tick，约30秒检查一次活动调度，主循环约2秒。不能将共享循环改成每日三次，否则会影响其他业务。
- 本轮GET /api/campaigns/sources核验：B站、X、小红书为automatic ready；抖音needs_config且automatic=false；公众号与视频号为manual。GET /api/ripple/accounts仅有一个已连接小红书账号，没有已连接抖音账号。
- 抖音已有浏览器campaign_read骨架和显式收费fallback。campaign_sources.py:1431–1469仅接TikHub活动列表，没有验证完整活动详情适配。未连接账号不应显示成“已成功采集0个活动”。
- 服务端本轮时间为2026-09-22T20:43:29+08:00。依赖已经有tzdata和filelock。

### 默认取舍（在确认计划时可调整）

- “各频道”按截图范围解释为活动广场的平台活动源，不改变热点雷达、文章发布和选题日历等其他周期任务。
- 09/14/20按当前项目运行时区固定为北京时间Asia/Shanghai，不跟随浏览器所在地改变。UI明确写时区。
- 全部视图允许全量手动刷新；单平台视图所有刷新入口均遵从该平台。只移除右上角全局Agent按钮，保留单活动规则处理。
- 关机/休眠错过时段不自动追赶旧批次，启动等下一时段；用户可手动刷新。默认允许时段后60秒的调度精度窗口应对30秒检查间隔，同窗口重启依靠持久化认领避免重复。已到点认领的整批平台允许串行稍后执行，不能因第一个平台耗时而让后续平台丢失该轮。
- 第三方项目本轮仅调研及确定候选，不自动安装或开启收费备用源；新增平台适配在取得实际账号/来源样本后单独验收。

## Public source research（2026-09-22）

### 抖音

- TikHub厂商文档明确列出活动列表及详情：GET /api/v1/douyin/creator/fetch_creator_activity_list；相邻目录提供fetch_creator_activity_detail。列表参数start_time/end_time；需要Bearer Token，文档示例明确写本次调用计费。文档不是实际成功采集证据，不能承诺全量覆盖或稳定运行。
  - https://docs.tikhub.io/346680197e0
- TikHub官方Python SDK有开源仓库和douyin_creator资源，SDK连接的是TikHub服务；开放代码不代表厂商API免费。
  - https://github.com/TikHub/TikHub-API-Python-SDK/blob/main/README_CN.md
- MediaCrawler README功能范围为公开帖子、关键词、评论、创作者主页，没有将官方创作活动列表/完整参赛规则列为能力；其README也有用途约束。不能直接以“支持抖音”视为活动源已实现。
  - https://github.com/NanmiCoder/MediaCrawler
- 本轮没有访问收费数据端点。未经授权直接读取api.tikhub.io/openapi.json曾返回403，随后查阅可访问的厂商文档；不绕过该限制。

### 微信公众号

- WeRSS / rachelos/we-mp-rss提供公众号文章采集、RSS、API、Webhook，可作为指定官方公告账号的订阅底座。它未提供Ripple所需的活动实体及规则字段，仍需白名单订阅源、原文解析、时间/活动去重和过期处理。
  - https://github.com/rachelos/we-mp-rss
- WeWeRSS也支持公众号历史文章、全文RSS及定时更新，但仓库已于2026-05-11归档；不按仍持续维护的即插即用方案引入。
  - https://github.com/cooderl/wewe-rss
- 复用前核查许可证、安全问题、授权方式与外部中转；禁止把账号凭证交给未知公共中转。没有安装任何候选项目。

### 微信视频号

- wx_channels_download是视频下载器；social-auto-upload主要提供上传与定时发布。其文档不能证明具备“官方活动中心+奖励/资格规则”采集能力。
  - https://github.com/ltaoo/wx_channels_download
  - https://github.com/dreammis/social-auto-upload
- TikHub SDK有wechat_channels资源，但平台支持列表不能证明有官方活动Feed。本轮尚未在检查的项目里核实可直接复用的完整视频号活动适配器，不声称全网没有。
- 后续方向：筛选官方发布的创作者活动公告，并评估授权账号下的活动入口。尚未核实的来源保留manual/未接入状态。

## Options considered → 推荐

A. 仅改前端按钮、把旧间隔统一调大：改动小，但后端串台及无到期X规则请求仍在；不能准确表示09/14/20。
B. 刷新范围前后端一致 + 现有调度器增加固定时段认领：推荐。复用现有状态文件、锁、zoneinfo，不引入新的调度服务/数据库。

## Phases / Steps

### Step 1：平台刷新范围与UI入口

文件：web/frontend/src/components/CampaignsPage.tsx、web/frontend/src/lib/api.ts、web/app.py、ripple/campaign_sources.py；tests/test_campaigns.py、tests/test_campaign_sources.py。

- 移除页头Agent入口及仅为该入口使用的前端代码；保留运行中任务进度/取消和单活动功能，不能因为去按钮就遗留无用导入或让现有任务无法取消。
- refreshNow接收明确范围，在点击时冻结平台。all取当前ready的活动源；单平台只取该平台，未就绪时就地展示配置/导入/登录说明。
- 后端平台列表校验去重。省略platforms可保留兼容性（解析为当前就绪平台）；显式[]拒绝422，未知平台拒绝422；已知但未就绪平台只返回自身的未配置/不支持状态，不触发网络，不回退全平台。审查所有API调用点防止旧默认[]误用。
- busy按请求平台集合记录；重复相同平台请求不重复排队。结束响应只回写其平台结果；用户切换筛选后不被旧请求重置页码、排序或提示。局部错误不让其他平台刷新按钮一直转圈。
- _start_x_enrichment_batch仅在X真正成功刷新后启动；B站补全只在B站刷新成功后启动，继续保留现有限额及证据指纹防重。
- 验收使用服务端mock调用计数与浏览器网络记录：小红书局部刷新过程中B站、X列表和X规则调用次数均为0；抖音未配置刷新不能产生任何其他来源请求。

### Step 2：固定每日三次采集与持久化防重

文件：ripple/campaign_sources.py、web/app.py，必要时一个小型纯计算辅助文件；tests/test_campaign_sources.py、tests/test_campaigns.py。

- 采用显式Asia/Shanghai时区、09:00/14:00/20:00固定时段，计算下一时刻和本轮slot key；不换成8小时滚动间隔。
- 扩展现有活动源状态schema，保存调度版本、时区、times及有界slot尝试状态；读取/迁移兼容旧last_sync与排序快照。slot认领在锁内、网络调用前持久化；多进程保护使用已有filelock能力。不能在长网络请求中持有用于状态读写的短锁。
- 自动批次先为到期的就绪平台建立待处理集合，原有采集互斥/账号锁照常使用；同批次平台串行稍晚完成合法。每平台每slot至多一次尝试，成功/失败/中断记录分开；重启遇到无法确认结果的已开始slot不重新发送收费请求。
- 仅在当前时段精度窗口认领新的自动批次，错过旧窗口不回放；跨日下一轮为明日09:00。首次迁移在非窗口只排下一轮，不立即把所有源重刷。
- 手动force与scheduled slot分开记录，手动不修改下一固定时刻；如果到点时同一平台已有读任务，合并/跳过该slot并记录原因，避免并发重复。
- 自动失败等下一轮，不在旧interval或x_next_retry到期就自行启动外部请求。X规则队列只在所属采集批次预算内处理，不能每30秒排下一组。
- 共享ripple/api.py本地心跳保持原样；仅替换活动调度的due判定，不影响service.tick或其他任务。

### Step 3：采集时段状态展示

文件：web/frontend/src/lib/api.ts、CampaignsPage.tsx、ripple/campaign_sources.py。

- sources返回schedule.mode=daily_slots、timezone、times、next_run_at及每平台运行状态；旧sync_interval_seconds仅兼容读取，不再用于新调度/UI。
- next_sync_at与last_sync.next_run_at统一投影自新调度，读取来源状态时不能将旧存储字段优先覆盖新值。
- 页面显示每日三个时刻及北京时间；下一次明确显示日期+时刻，倒计时用server_now对齐。manual/未配置来源不显示假的采集倒计时。
- 保留手动刷新活动与单活动详情读取，且各自scope准确；页面加载、翻页、排序只读本地，不能产生新的采集轮次。

### Step 4：验证、提交与自动重启验收

- 定向：时钟模拟覆盖08:59:59/09:00/09:00:30/14:00/20:00、次日09:00、调度窗口外、同slot重复tick、同slot进程重启、关机错过、失败、人工08:59刷新、同平台并发、跨平台隔离、禁用来源和[]/未知平台。
- 更新旧测试test_scheduler_processes_existing_x_rule_queue_even_when_no_source_is_due为不启动外部请求的预期；增加仅X成功fresh才启动X规则整理的回归。
- 前端实测：Agent页头按钮消失，未配置抖音正确说明，单平台两处刷新范围一致、提示与忙碌状态准确，切tab不串响应；既有默认/最新和每页10条保持。
- 执行pytest相关用例、全量pytest、npm run build、git diff --check，审查只改计划文件；commit。
- 用户已明确授权改完自动重启本地Ripple。实施后通过既有计划任务重启并核实监听端口实际Python子进程已替换，检查新API/来源时段/UI；不能仅以schtasks返回成功就宣布代码已加载。不自动push/deploy。

## Touch list

Will change（确认后）：
- ripple/campaign_sources.py
- web/app.py
- web/frontend/src/components/CampaignsPage.tsx
- web/frontend/src/lib/api.ts
- tests/test_campaign_sources.py
- tests/test_campaigns.py
- 必要的分页/平台刷新浏览器回归脚本或测试fixture（复用现有测试能力，不引入新框架）
- 本计划及必要的本地运行说明

Will NOT touch：XHS官方排序和详情解析器、抖音/公众号/视频号真实采集实现（本轮仅调查，不虚报已接入）、账号凭据/浏览器隔离、热点源、发布调度、选题与收藏数据。

## Risks / rollbacks

- 调度迁移可能改变下一次时间：新状态显式版本化，保留历史成功计数；迁移后以新计划投影，旧数据不被清空。
- 到点多平台串行会稍晚完成：保存同slot批次，禁止依完成时刻创建新slot或丢失后续平台。
- 网络结果不确定时无法严格保证远端“恰好一次”：采用本地slot最多一次派发，异常记录为中断并等下一轮/人工操作；不通过重复收费请求验证。
- 删除入口不等于关闭所有规则处理：计划只移除右上角批量入口；后台规则整理已并入对应来源采集轮次，单活动操作保留。
- 开源文章/视频工具不能保证活动规则完整：源接入必须用真实活动样本另验收，授权或付费条件不足时保持未就绪。
- 回滚以本次提交和状态备份为基础，不删Campaign/收藏；回滚旧调度可能恢复高频，先停止该旧调度再处理。

## Open questions

等待确认本计划。默认按活动广场范围、北京时间和错过时段不补采执行；时区如需改用其他值，应在实施前确认。抖音真实账号授权和付费源选择不阻塞本轮UI/调度修复；公众号/视频号新增采集器单独规划。
