# X 发布后管理真实验收记录

日期：2026-09-28。时间均为北京时间（Asia/Shanghai）。本记录只涉及本轮明确创建的测试作品；原有历史作品只做只读检查。

## 账号与 Profile

- Ripple 账号 ID：`2e6f864a01f844839db70498afe360c2`；平台身份：`x-web:solarisl31285`（`@SolarisL31285`）；浏览器通道：Chrome；执行节点：本机。
- 只读探测从 `.ripple-private/outputs/accounts/2e6f864a01f844839db70498afe360c2/browser/XProfile` 取得相同平台身份。执行器始终使用该账号保存的 Chrome 通道。
- 个人主页当前使用 `UserOriginalsTimeline` 和 `UserRepliesTimeline`。同步读到 4 条历史本人作品，包含原帖和回复；最终同步为 `complete=true`。这些历史作品没有被编辑或删除。

## 纯文字测试作品

- 本地发布任务：`d6914b39505842069fbbeab1aad553c5`。15:59 提交，发布器返回 `accepted`。创建响应未提取 ID，因此没有再次发布；同账号列表只新增含唯一标识 `QA-20260928-9f0d55` 的作品。按远端 ID 回读、核对作者后关联任务。
- 原 ID：`2104481113037283687`；编辑新版本 ID：`2104483934667186686`，平台版本列表同时包含这两个 ID。编辑操作 `aefc794528174586942b3128b003635d` 于 16:10 提交，16:15 左右从新版本回读为 `verified`；媒体前后均为空。
- 原正文：`Ripple X post-management test QA-20260928-9f0d55. I will check this post, edit it if X allows, and delete it.`
- 修改后正文：`Ripple X post-management test QA-20260928-9f0d55. The text has been edited in Ripple. I will verify it and delete this test post.`
- 16:32 删除新版本。操作 `9140474e25f746d1a95e017f26d9e7e3`、操作 ID `ac6ff6495fdf465fbfb85b3194001719` 取得绑定目标 `2104483934667186686` 的 `DeleteTweet` 200 响应；同一 Profile 重新加载目标后未见作品，状态为 `verified`。**已清理。**

## 首次配图测试与媒体问题

- 最终提交任务：`44a15cad0e944094acbdbfaeee3dfd92`，16:21 返回 `accepted`。之前 3 个准备任务分别在账号身份或正文编辑器尚未加载时中止，均为 `not_submitted=true`，没有提交标记或平台作品。
- 原 ID：`2104486715566252368`；原正文：`Ripple X image post-management test QA-20260928-6361ce. One test image; I will verify, edit if available, then delete.`；原图片媒体 ID：`2104486706733035520`。
- 16:27 编辑操作 `5338011729c3474d9da9a8611fba260c` 提交文字。新版本 ID：`2104488078010359910`；新正文：`Ripple X image post-management test QA-20260928-6361ce. Text edited in Ripple; the original test image is retained. I will verify and delete it.`
- 平台详情与页面均确认新版本没有图片。该操作记为 `partial`，证据同时保存编辑前媒体 ID 和编辑后空媒体列表；**此次配图编辑未通过媒体保持验收**。原因是平台编辑弹窗延迟载入原图，执行器在图片预览出现前点击了 `Update`。
- 16:31 删除新版本。操作 `9a55ca1a83f840989ccbe214b511d8bc`、操作 ID `c79ab526da7541ad8c1823937d4a446c` 取得绑定目标 `2104488078010359910` 的 `DeleteTweet` 200 响应，并在重新加载后确认目标缺席。**已清理。**

## 配图媒体保持补测

- 发布任务：`3843856385554859a3d38664cb7860f6`。16:34 提交，原 ID：`2104489952436437394`；原图片媒体 ID：`2104489939249590272`。本次私有 `CreateTweet` 响应在 `data.create_tweet.tweet_results.result.rest_id` 返回该 ID，账号和正文均匹配。原始响应保存在该账号私有目录的 `operations/ceb35eae62fc4e13b1f81110c15ae4ab/platform-create-response.json`，不会由新接口对外返回。
- 原正文：`Ripple X media-retention test QA-20260928-86d10f. The image should remain during a text-only edit; this post will be deleted after verification.`
- 编辑弹窗内原图片约 5 秒后以 `blob:` 预览出现。执行器现在等待原媒体预览数量与作品媒体数量一致，并在填入正文后再次核对，才允许点击 `Update`。
- 16:39 编辑操作 `f5cad37a5b8340af9c487ea51a6c8c6d` 回读为 `verified`。新版本 ID：`2104491114216710324`，版本列表保留原 ID；新正文：`Ripple X media-retention test QA-20260928-86d10f. Edited text; the original image stays attached. This test post will be deleted.`。编辑前后图片媒体 ID 和 URL 完全一致。
- 16:40 删除新版本。操作 `148cee466eb3464286a0ebd9fb44886e`、操作 ID `9f15b28f7d7d462db4ebd0a43ee99e77` 取得绑定目标 `2104491114216710324` 的 `DeleteTweet` 200 响应，并在重新加载后确认目标缺席。**已清理。**

## 创建回执补测及待核对删除

- 16:49 通过 Ripple 再发布短文字测试帖，任务 `e75ebe563485424d830658aa5ebe6f6c`、操作 ID `1ae4c92890bb490a96a892c324f278be`。提交回执当场包含 `platform_create_response`、远端 ID `2104493741977534940` 和账号身份 `x-web:solarisl31285`。按该 ID 从本人 Profile 回读到相同正文，任务保留 `accepted`，可见范围仍为“未知”。私有创建响应和回执均保存在该操作目录。
- 正文：`Ripple X receipt test QA-20260928-d608a0. I will read this post by ID and delete it after verification.`
- 16:50 删除操作 `686ed50a83924cea83c100847466f3cf`、操作 ID `3104328751574d92988778ca54ae68d6` 已写入目标绑定的提交前标记并点击删除确认，但执行器没有捕获 `DeleteTweet` 响应。**没有再次提交。**
- 之后同账号对目标详情的只读请求返回 `TweetDetail` 200，其中 `entryId` 为 `tweet-2104493741977534940`、`tweet_results` 为空；页面提示目标不存在。16:55 完整同步仅有 4 条历史本人作品，没有本轮测试标识。16:58 只读核对将这些目标状态写入操作证据；随后再次读取目标详情与本人主页，结果一致。原始响应保存在该账号私有目录的 `operations/3104328751574d92988778ca54ae68d6/query-evidence.json`，接口不返回私有路径。由于缺少目标删除回执，操作继续保留 `unknown_result`，账号为 `recovery_required`。该作品当前不可见，**删除证据尚不足以判定已验证清理**，列为未完成项。当前作品记录为 `not_found_in_sync`，没有伪标“已删除”。

## 编辑限制和最终状态

- 历史原帖 `2096488383002677344` 的发布日为 9 月 6 日，详情能力显示“X 仅允许发布后 1 小时内编辑”；历史回复 `2096488616998678588` 显示“X 不支持编辑回复”。未对两条历史作品发起写操作。[X 编辑规则](https://help.x.com/en/using-x/edit-post)还列明订阅资格、1 小时内最多 5 次编辑和同设备要求；本轮没有用真实帖子耗尽 5 次限额。
- 16:43 完整同步读到 4 条历史本人作品，没有前三个测试标识。前三条测试作品的当前版本均有目标绑定删除回执与重新加载检查；本地 6 条版本记录均标为已删除。第四条创建回执补测的删除仍待核对，见上节。本人的作品可读不等于已证明公开可见，测试期间的可见范围均保留为“未知”。
- 创建响应解析已兼容 X 当前缺少 `__typename` 的 `CreateTweet` 结果，并在 16:49 的补测中实测直接把 ID 写入回执。前三条测试作品因修复发生在其发布之后，使用平台本人列表、目标详情或当次私有创建响应核对 ID，没有伪造历史创建回执。

## 自动检查与运行限制

- 相关 Python 测试：106 项通过，6 条依赖警告；前端单元测试 71 项通过，构建和 lint 通过。测试覆盖目标详情不可用但缺少删除回执时继续保留 `unknown_result`。
- 当前常驻 `7860` 后端仍是 9 月 26 日启动的旧进程，未加载这些新增接口。本轮真实操作使用当前源码的 `WorkspaceService` 和 Ripple 独立 Profile 完成，没有重启常驻服务；使用网页新入口前需正常重启后端。

## 17:12 删除待核对恢复补充

- 对操作 `686ed50a83924cea83c100847466f3cf` 再次执行只读核对，没有重复提交删除。目标详情返回绑定 ID `2104493741977534940` 的不可用结果；本人作品列表读取完成，未发现该 ID 或关联版本。
- 本地 `submission.json` 与操作 ID、目标 ID、账号身份一致。由于仍缺少 `DeleteTweet` 成功响应，操作继续保持 `unknown_result`，作品保持 `not_found_in_sync`，不能宣称已验证删除。完整列表和目标详情的证据已保存到操作记录。
- 该账号的 `recovery_required` 锁已解除；目标作品的编辑和删除仍禁用。删除执行器现在等待目标响应最多约 30 秒，并记录是否发出目标绑定请求，避免提前导航中断慢速响应。取得删除回执后还会等待目标详情返回不可用结果，不能仅凭页面暂时没有帖子判定成功。
- 本次相关 Python 测试 58 项通过，前端单元测试 71 项通过，构建和 lint 通过。没有新增平台写操作。尝试重启本地 `7860` 服务时被当前执行策略拦截；旧进程 `48384` 仍在运行。新界面和服务代码需在允许正常重启后加载。

## 17:29 X 删除新帖验证

- 使用已连接的 Chrome Profile `@SolarisL31285`。删除前完整读取本人作品列表，`complete=true`，共有 4 条历史作品。此次只对新建临时帖执行平台写操作，历史作品未修改。
- 17:29 创建纯文字临时帖，发布任务 `f5d4eabae02445059bf03a75bbdf4b15`、发布操作 ID `1580a04a4b7948aeb7e59062b49eed01`。正文为 `Ripple X deletion verification QA-20260928-XDELETE-112d16b3. This temporary test post will be deleted after verification.`。17:30 发布回执返回 `accepted`、`platform_create_response` 和远端 ID `2104503874442953039`。按 ID 回读确认作者为 `x-web:solarisl31285`，正文与任务完全一致；可见范围仍为 `unknown`。
- 17:30 删除预览绑定该远端 ID、账号和正文。删除操作 `014f31d9c41c4416a945cf808a5387f6`、操作 ID `dbaedf294e1a4e17bb039ebfdf3d5717` 只提交一次。原始浏览器证据记录了目标绑定的删除请求，以及唯一一条目标 ID 匹配、响应体有效的 `DeleteTweet` 200 回执。执行器重新加载目标后取得 `target_absent_after_reload` 证据，操作状态为 `verified`。
- 17:31 再次独立读取目标详情，返回 `exists=false`、`target_unavailable=true`；随后完整读取本人作品列表，`complete=true`，共有 4 条作品，目标 ID 及关联版本均未出现。本地作品状态为 `deleted`，删除入口关闭，账号没有待核对操作。**本次临时帖已验证清理。**
- 先前测试帖 `2104493741977534940` 的操作 `686ed50a83924cea83c100847466f3cf` 仍为 `unknown_result`；本次新帖的成功回执不能补足该旧操作缺失的回执。
- 本次真实操作直接使用当前源码的 `WorkspaceService` 和 Ripple 独立 Profile。`7860` 常驻进程仍是 9 月 26 日启动的旧进程；本次结果验证了当前源码的删帖执行路径，未验证新网页入口。
