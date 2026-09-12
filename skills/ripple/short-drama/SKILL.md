---
name: short-drama
description: >
  制作多集 AI 微短剧：建立剧集圣经和角色参考，完成分集剧本、逐镜 I2V、对白审计、配音字幕 BGM 与成片，保持跨镜跨集一致性。
  当用户说“AI/横屏/竖屏/微短剧、拍短剧、分集剧本、连续剧情视频、做几集短剧”时使用。
  单条非剧情视频用 auto-short-video；只写单条脚本用 video-script；只生成一个视频片段用 ai-video-gen。
layer: produce
---

# AI 短剧制作（横屏/竖屏微短剧，多集）
> Ripple 托管媒体调用以 Settings/default capability 为准；本文件里的脚本命令属于独立制作/维护环境，Skill 本身不授予 Shell 或付费调用权限。
## 正式成片质量约束（按任务阶段触发）

1. **交付的是动态短剧成片时，动态镜头必须有真实视频片段。** 用户只要剧本、分镜、静态 animatic/样片时可以停在对应阶段，但要明确标注产物形态，不能把静态图冒充动态成片。
2. **配音引擎按用户目标、当前默认能力和实际质量选择。** 不因为“闭源/Edge”品牌类别做通用硬门；角色声线一致、说话人映射正确、可懂度满足要求才是验收目标。
3. **原生对白是能力分支，不是默认信仰。** 只有当前视频模型明确支持/已验证原生音频且本任务需要时，才使用 native-first + ASR 审计；用户选择后期 TTS、模型不支持对白或已有可靠配音方案时，可直接走 dub/TTS 路径。
4. **时间线合法性是客观约束。** 对白/字幕不能越界或跨镜错位；慢放、冻结、循环是否允许取决于创作要求，只有会造成口型/动作明显错误时才阻止。

> `prepare → generate → audit → align` 只适用于“模型原生对白 + 审计”这条实现路径。若选择纯后期 TTS、已有素材或其他合法路径，不应仅因为流程不同判任务失败。当前脚本若仍对替代路径设置实现级硬门，报告为代码层限制并另案处理，不通过文档参数强行绕过。

> 编排层 SKILL：创意（圣经/剧本/分镜/lines）你 LLM 写，生成动作全委派已有 SKILL（ai-image-gen/ai-video-gen/ai-music），确定性 IO 走 `scripts/drama_ops.py` + `scripts/dubbing.py`；角色一致性靠「先定参考图再 I2V」+ 剧集圣经锁成同一部剧。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| 题材/梗概 | 是 | 一句话剧情或改编源（没给就问） |
| 集数 | 否 | 由题材/需求定，**不强制**（微短剧常 10–30 集只是常见值；起号测试可先 1–3 集验证链路） |
| 单集时长 | 否 | **不强制固定分钟数**——由题材/平台/剧情节奏定，几十秒到数分钟皆可；成片总时长 = 各镜片段时长之和（脚本不设上限，用户说多长就多长） |
| 画幅 | 成片阶段需要确定 | 优先复用用户明确值、剧集既有规格或目标内容已确定比例；概念/剧本阶段不因缺画幅停工，可逆样片可用说明过的默认；正式批量成片前确认一次即可 |
| 视觉风格 | 否 | 都市港风/古装/校园/悬疑…（定统一风格前缀）|

## 产物结构（`outputs/剧名/`）

```
series-bible.md         剧集圣经（戏剧承诺+角色表【行动模型+want/need/wound/flaw+可跟随四问+音色档案】+剧情线+情绪曲线/爽点节奏+反派阶梯+分集反转+钩子+风格前缀）
cast.json               选角表（角色→音色：edge 音色 + pitch/rate，或克隆音色）
ref_index.json          参考图索引（C 角色 / S 场景 / P 道具，跨镜跨集复用）
refs/                   参考图（C01_林策.png ...）
episodes/ep01/
  script.md             本集剧本（因果节拍四幕 + 对白 + 集末钩子）
  lines.json            本集逐行对白（{speaker,text,emotion,shot,at?}，shot=所属镜头、at=镜内起始秒/对齐说话时刻，喂时间线配音）
  shots.json            本集分镜（逐镜：时间轴 prompt + @参考 + 尾帧描述 + 可选 sfx[定时音效]；align 回写每镜 duration）
  shots/                逐镜关键帧图 + 生成的片段
  voice.mp3 / voice.srt 多角色配音（每角色独立声线、镜对齐）+ 同轴字幕
  timing.json           逐镜时长 + 逐行起止（align 产出，留档/校验）
  clip-audit.json       原生音轨、ASR、语言、说话时间与 native/dub/regenerate 决策
  final.mp4             本集成片
progress.json           逐集/逐镜生成进度（控费、断点续跑）
```

脚本（相对项目根）：`skills/ripple/short-drama/scripts/drama_ops.py`（资产/分镜/进度）、
`skills/ripple/short-drama/scripts/dubbing.py`（多角色配音）；
合成器复用：`skills/ripple/auto-short-video/scripts/assemble.py`。

## 执行步骤

> 生图/生视频/云配音可能按量计费。第一次进入新的批量制作范围时说明集数/镜数、调用类型、耗时与可得的费用信息并取得授权；当前预算和镜头范围已确认后不逐步重复询问。扩大范围、改用明显不同费用/数据目的地时再确认。可先做代表性样片降低风险。

### 0. 剧集策划（新建系列/结构性改稿时）

1. **选题材 + 定核心回报**：新系列、题材未定或现有剧情缺乏核心冲突时再读 `references/genre-hooks-handbook.md`；已有 series-bible 的续作/局部改稿复用既定题材，不重新选型。
2. **搭骨架**：`python skills/ripple/short-drama/scripts/drama_ops.py scaffold --series "<剧名>"`。
3. **故事引擎按需**：创建/重构 series-bible，或诊断到人物能动性/冲突薄弱时读取 `references/story-engine.md`——
   - 写**戏剧承诺**一句话并过其自检（主体/追求/昂贵阻力/反复回报，换名不失独特、阻力有筹码、主角有能动性、中段有回报）；
   - 每个主要角色建 **8 字段行动模型 + want/need/wound/flaw/弧光 + 可跟随四问（ep1 可见其一）**；
   - 反派按 `references/satisfaction-and-villain.md` 排 **4 层阶梯**（治工具人/爽点通胀）。
4. 写 `series-bible.md`（读 `references/series-bible-schema.md`，已含上述深度字段）：一句话卖点、**戏剧承诺**、世界观、**角色表（行动模型+深度）**、剧情主线、**情绪曲线 + 爽点节奏表 + 反派阶梯**、**分集大纲（每集：剧情 + 因果反转点 + 集末悬念）**、**统一视觉风格前缀**。
5. **结构复核**：新建/大改 bible 时按 `drama-review-rubric.md` 检查故事承诺、人物目标和冲突证据；完全缺失关键因果/角色动机属于结构问题，审美偏好或分数差异只给建议，不因自评分数阻塞局部后续工作。

### 1. 分集剧本（因果节拍 + 台词功底，别写流水账）

6. 每集先读当前 series-bible、上一集/当前状态和本集大纲；四幕、因果节拍、竖屏节奏、对白等 references 只在新阶段第一次使用或当前剧本暴露对应问题时加载，同一系列规则未变化时复用。
   目标(3秒钩)→阻碍→转折(爽/虐爆点)→集末钩子，相邻节拍用「因为/所以」串；**爽点密度**每 15–30s 一个情绪事件。
   - **反转按因果生成**：每个反转过 `genre-hooks-handbook.md` 的「因果反转一句话测试」+ 公平揭示 5 问，填不出=空降反转，重做。
   - **台词**：角色要有区分度、意图和潜台词；单句长度服从表演与镜头节奏，不强制每集凑金句或统一 15 字上限。
   - **每句台词必须标清说话人**：剧本里对白一律写成 **`角色名：台词`**（角色名用 cast/bible 里的准确名字，别用「他/她/众人」）——后续抽 lines.json 时 speaker 直接照抄，避免配音配错角色。
7. 写完按本集变更范围复核因果、节奏、说话人和设定一致性；空降反转、人物身份冲突等客观叙事错误修复后重验，节奏/金句等主观建议不循环返工。

### 2. 角色 / 场景定妆（一致性地基）

8. 用 **ai-image-gen** 为每个角色生成**定妆参考图**（正面/多角度，喂角色外貌关键词 + 统一风格前缀）；关键场景/道具同理。
9. 逐个登记进索引（自动分配 C/S/P 编号）：
   ```bash
   python skills/ripple/short-drama/scripts/drama_ops.py ref add --series "<剧名>" \
     --kind character --name 林策 --image refs/C01_林策.png --desc "男主，冷峻西装" --style "都市港风"
   ```
   读 `references/character-consistency.md` 了解为什么必须先定参考图。
9.5 **定妆复核（新建/变更参考图时）**：新生成或替换的角色定妆图需要查看其身份、年龄、服装等关键设定是否匹配；已经审核且未变化的参考图不重复复核。
   ```bash
   python skills/ripple/short-drama/scripts/drama_ops.py ref review --series "<剧名>" \
     --code C01 --observation "看到：冷峻短发西装男，符合男主设定"
   ```
   形象跑偏（如男主长成大叔、萝莉长成成年）→ 重生成定妆图再复核，**别拿跑偏形象往下生视频**（跨镜长相全崩）。角色定妆图没复核，后面 `generate` 会**硬拦**。

### 2.5 选角（角色→音色，优先匹配角色与用户质量要求）

10. 新建 cast 或角色形象/声音要求变化时读取 `voice-casting.md`。音色要和角色年龄、气质、说话方式匹配；`archetype/ref` 用于记录依据。选择当前可用且符合质量目标的语音引擎，不因 Provider 品牌设置硬门。`cast check` 应用于说话人映射、缺失 voice_id 等客观配置错误；如果现有脚本仍强制某类引擎，视为实现层限制另案处理。
   ```bash
   python skills/ripple/short-drama/scripts/dubbing.py cast init --series "<剧名>"   # 建模板（旁白默认闭源）
   # 按角色定妆图形象挑贴合的闭源预置音色，并标注 archetype/ref
   python skills/ripple/short-drama/scripts/dubbing.py cast add --series "剧名" --name 林策 --role male_lead \
     --engine clone --provider openai-compatible --voice-id FunAudioLLM/CosyVoice2-0.5B:benjamin \
     --archetype "冷峻男主" --ref C01 --note "低沉磁性，贴 C01 定妆图"
   python skills/ripple/short-drama/scripts/dubbing.py cast add --series "剧名" --name 朵朵 --role child \
     --engine clone --provider openai-compatible --voice-id FunAudioLLM/CosyVoice2-0.5B:bella \
     --pitch=+6Hz --rate=+5% --archetype "萝莉/小女孩" --ref C03 --note "童声,贴 C03 幼态定妆图"
   python skills/ripple/short-drama/scripts/dubbing.py cast check --series "剧名"
   ```
   需要**专属克隆嗓音**（非预置）→ `--provider minimax/dashscope` + 先 `voice_clone.py enroll` 拿 `--voice-id`。
   具体可用语音能力以当前 Settings/运行时或独立 CLI 配置为准；需要专属克隆嗓音时才进入 enroll。分层与配置见 voice-casting.md 与 `multi-voice-dubbing` SKILL。

### 3. 分镜脚本 + 逐行对白 + **时长规划**（自然的关键：先定好每镜停留多久，再去生视频）

11. 按本集时长与节奏**拆镜**（镜数不强制——时长短几个镜、时长长就多几个镜，跟着剧情走），写 `episodes/epNN/shots.json`（格式见 `references/shot-prompt-format.md`）：
   每镜含 `idx / desc / prompt(风格头+逐秒画面节拍+【声音】) / refs(引用 C/S/P) / tail(尾帧描述)`。
12. **同时抽本集逐行对白 `episodes/epNN/lines.json`**（有序 `[{speaker, text, emotion, shot}]`；字段规则见步骤 17）——**在生视频之前就写好**，因为每镜停留多久由它的台词决定。
13. **规划每镜时长 + 检查能否塞进片段**（治「画面停住/太赶」的根本）：
   ```bash
   python skills/ripple/short-drama/scripts/dubbing.py plan --series "<剧名>" --episode N
   ```
   ⚠️ **现实约束：AI 视频只能生成固定档位（多为 5s，部分 5/10s），不能按任意秒数出片。** 所以 `plan` 的作用是：① 估出每镜台词时长（= 最终画面应停留的时间，写回 `target_duration`）② **检查每镜台词能否塞进一个片段档**——**塞不下（台词 > ~5s）就拆成多镜或精简台词**（`plan` 会 ⚠️ 标出来），别硬生成再靠后期拉伸 ③ 给出每镜建议生成的片段档位 `gen_duration`（5 或 10）。**把每镜台词控制在一个片段档内，是画面自然的关键。**
14. **校验分镜**：`drama_ops.py shots validate --series "<剧名>" --episode N`（风格前缀/引用参考图/编号已登记/idx 连续）。

### 4. 关键帧生成（只是 I2V 的首帧，不是成片）

15. 正式动态成片需要 I2V 首帧时，用 **ai-image-gen** 按每镜 prompt 生成首帧图并引用相关 refs，落 `episodes/epNN/shots/`，路径写回 shots.json 的 `frame`。如果用户当前只要剧本/分镜/静态 animatic，可在对应阶段结束并标明产物类型。

### 5. 逐镜生视频（仅正式动态成片阶段）

16. **选择本集视频模型与音频策略**：托管调用使用用户指定或 Settings 默认 Provider+Model；独立 CLI 才按自身本地配置。多个可用不是追问理由。只有当前模型明确支持/已验证原生对白并选择 native-first 时，才把台词契约写入 generation prompt；纯后期 TTS 路线不强行要求视频模型说台词。
    ```bash
    python skills/ripple/short-drama/scripts/dubbing.py prepare --series "<剧名>" --episode N --language zh-CN --provider "$VIDEO_PROVIDER"
    ```
    选择 native-first 时，`prepare` 可把 `lines.json` 的台词契约写进对应镜头 `generation_prompt`；选择纯后期 TTS 时，视频 prompt 只描述画面/动作/所需环境声，不强迫模型生成对白。旁白按独立音轨处理。
    - 使用 native-first 时，一镜可以有多句画内对白，只要当前模型/片段时长能容纳并且后续审计可验证；纯 TTS 路线按画面节奏安排台词，不要求把台词喂给视频模型。
    - `native` 与 `dub/TTS` 都是合法策略，按模型能力、用户偏好、预算和成片要求选择。若当前脚本仍对 `dub` 有实现级硬门，报告该限制并另案修改业务代码；本轮文档不建议用强制参数绕过实现门禁。
17. **逐镜生视频（脚本驱动）**：
    ```bash
    python skills/ripple/short-drama/scripts/drama_ops.py generate --series "<剧名>" --episode N --ratio "<9:16或16:9>"
    ```
    本集确定 Provider/Model 后保持一致以便复现；clip 写回并记进度，已有可用 clip 直接复用。`generation_prompt` 是否包含对白取决于第 16 步音频策略；不要把 native-first 的实现要求套到纯 TTS 路线。
    - 需要口型/画内对白对齐时，片段必须容纳该镜对白时间线；不足就拆镜、精简或重生成。纯旁白/非口型场景可按用户创作要求使用合理的裁剪、速度或循环策略，不设“任何情况下绝不慢放/冻结/循环”的通用禁令。
    - 跨镜/跨集连贯用 `references/character-consistency.md`「尾帧→下一镜首帧」（`--only` 单镜重生成时把上一镜尾帧填进该镜 frame）。

### 6. 多角色配音 + 字幕 + 音效 + BGM（时间线对齐）

> lines.json 已在步骤 12 写好；这里做实际配音并**按时间线对齐**。
> **核心模型（时间线/轨道式）**：每镜 = 一条完整片段的时间线，**台词只占其中一段**，其余时间是
> 动作/停顿/音效——**片段时长通常 > 台词总长**。配音不是把台词首尾相接填满片段，而是把每行
> **按 `at` 偏移**放到片段时间线上（对齐说话/嘴动时刻），空白留给动作与音效。

18. **原生音轨审计（只有使用/依赖原生音频时）**：模型生成了需要保留的对白/环境音时按 `references/native-audio-workflow.md` 做 ASR/音轨检查；纯 TTS 或无原生音频路线跳过无关 ASR，不为流程完整额外跑审计。
    ```bash
    python skills/ripple/short-drama/scripts/dubbing.py audit --series "<剧名>" --episode N
    ```
    审计结果可标 `native / dub / regenerate`。语言错误、素材缺失、口型/人物明显不符属于可观察问题；重生成次数受当前任务预算和问题是否在收敛约束，不设所有场景统一“两次”。ASR 相似阈值是诊断值，结合实际对白内容、口型需求和字幕用途判断。
19. **逐行对白 `lines.json` 字段规则**（步骤 12 写、此处复核）——有序 `[{speaker, text, emotion, shot, at?}]`：
    - `speaker` **必须精确等于 cast.json 里的角色名**（照抄剧本「角色名：」，别写「他/她」泛称）——`align` 会严格校验，speaker 不在 cast 直接拦截报错（**治「配音配错角色」**），不再静默用旁白音色顶替。
    - `emotion` 是给配音引擎的**情感通道**（不是让角色把情绪念出来），写具体：愤怒/冷笑/隐忍/崩溃大哭/颤抖/惊恐/得意/温柔/失望/嘲讽/急切/沉痛/撒娇 等——闭源云 provider 转成「用<情绪>的语气说」驱动演绎。**⚠️ 音色不随情绪改变**：`emotion` 只调语调/语速/情感（`emotion_prosody` 叠加 rate/pitch/volume 增量），角色音色身份始终是 cast.json 里绑定的那一个 `voice_id`——同一角色跨镜跨集音色恒定，只有情绪语调在变。
    - **`shot` = 这句台词所属的镜头 idx（必填）**：让每句配音/字幕落到对应画面片段上；一镜可多句；纯动作镜无台词就不出现在 lines 里。
    - **`at` = 这句台词在**本镜片段内**的起始秒（可选，秒）**：对齐画面里角色**开始说话/嘴动**的时刻（如角色前 1.5s 在走动、之后才开口 → `at: 1.5`）。不写则从头顺序排。**这是「说话时刻对得上画面」的关键**；台词之间的空白就是动作/停顿/音效的时间。
20. **时间线对齐配音（正式成片且存在对白/旁白时）**：
    ```bash
    python skills/ripple/short-drama/scripts/dubbing.py align --series "<剧名>" --episode N
    ```
    native 路线读取对应审计结果；纯 TTS 路线直接使用已确定的对白/旁白轨。产出需要的 `voice.mp3`、`voice.srt`、`timing.json` 并记录真实片段时长。没有语音的纯动作内容不为了完成流程生成空配音资产。
    - 默认以真实 clip 时长建立时间线，台词按 `at` 叠加；是否裁剪、变速或循环由用户创作要求和口型/动作约束决定。会导致对白越界、口型错位或明显动作错误时必须修正。
    - **时间线硬拦**：有台词镜缺 clip、`at` 为负、同镜台词重叠、或 `at`+真实配音时长超出片段，任一情况均失败；必须重生成、拆镜、精简台词或修正 `at`，禁止让字幕/声音拖到下一镜。
    - lines 没标 `shot` 或 speaker 配错 → `align` 拦截/告警，按提示修 lines.json 重跑。
21. **音效 + BGM**（占用非台词时间，让画面有声音层次）：
    - **音效**（枪声/椅子移动/脚步/开门/耳光…）：在 `shots.json` 该镜加 `sfx` 数组 `[{"file": "sfx/gun.wav", "at": 1.2, "volume": 0.9}]`（`at`=**镜内**秒）。音效文件可用 **ai-music** 生成短音（或素材库），把路径填进 `file`。`storyboard` 会把镜内 `at` 换算成全局时间、`assemble` 定点叠进成片音轨。
    - **BGM**：**ai-music** 按剧情氛围生成（紧张/甜/悬疑），落 `episodes/epNN/bgm.mp3`（assemble 混音时自动对旁白闪避压低）。
    （字幕已由 `align` 产出 `voice.srt`，无需再单独跑 auto-subtitle。）

### 7. 逐集合成 + 交付

22. 把本集分镜转成合成输入并合成。`storyboard` 自动读取审计结果：`native` 镜**整轨原音（模型原声+环境音）直通**；`dub` 镜原生轨**丢弃、改用独立 TTS 配音**（无人声分离时保留会与配音双重人声）；完全无原音的镜补等长静音；侧链闪避仅让旁白/配音干净盖在 native 镜环境音上；合成器统一 AAC 48 kHz stereo 后拼接：
    ```bash
    python skills/ripple/short-drama/scripts/drama_ops.py storyboard --series "<剧名>" --episode N --size "<1080x1920或1920x1080>" \
      -o episodes/epNN/storyboard.json --narration episodes/epNN/voice.mp3 --bgm episodes/epNN/bgm.mp3 --subtitle episodes/epNN/voice.srt
    python skills/ripple/auto-short-video/scripts/assemble.py assemble \
      --storyboard episodes/epNN/storyboard.json -o episodes/epNN/final.mp4
    ```
    字幕由 assemble 自动烧成**底部居中、字号合适**的样式（默认按分辨率，可用 `--sub-size/--sub-margin-v/--sub-font` 微调）。
23. 每集结尾可加「下集预告/钩子卡」提升追剧。**成片后过 `references/drama-review-rubric.md`「配音/选角」「成片」评审**（尤其确认**不是全剧一个声音**、声线贴人物、**字幕/配音与画面对齐**）。多集每集重复步骤 6–22（剧本→分镜→生视频→配音→合成）；用 `progress show` 看整部进度。
24. **发布**：按已确认画幅/平台交对应发布层 SKILL（如竖屏抖音/快手，横屏 B站）。

## 编排原则（承 auto-short-video）

- **产物类型如实标注**：动态成片使用真实视频 clip；剧本、分镜、静态 animatic、纯 TTS 版都可以是用户明确要求的合法交付形态。中间产物和进度留档，已生成且可复用的镜头不重复烧钱。

## Profile 感知

- **有 Profile**：按需读取平台规格、风格、定位和偏好；已有剧集规格直接复用。
- **无 Profile**：先利用用户已给的题材/素材/目标完成可完成阶段；只问真正影响下一步的关键缺口。集数、时长、风格未指定时可给可调整建议，不一次性索取整套参数。
