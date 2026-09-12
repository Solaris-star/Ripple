# Ripple Third-Party Notices

Ripple 自研代码按根目录 `LICENSE` 中的 Apache License 2.0 发布。本文件列出仓库内直接包含、改编或需要保留来源说明的主要第三方内容。依赖管理器安装的完整依赖许可证仍以对应包自身为准。

## Vendored / adapted skills and references

详细逐项来源记录位于 `LICENSES/skill-attribution/`。

### OpenClaudia `icp-builder`

- Upstream: https://github.com/OpenClaudia/openclaudia-skills/tree/main/skills/icp-builder
- Used by: `skill-audience-profiler` lineage
- License: MIT
- Preserved license: `LICENSES/OpenClaudia-MIT.txt`

### lucasygu/redbook

- Upstream: https://github.com/lucasygu/redbook
- Used by: `skills/ripple/skill-xhs-analyzer/`
- License: MIT; Copyright (c) 2026 Lucas Gu
- Preserved notice: `LICENSES/MIT-UPSTREAM-NOTICES.txt`
- The package remains attributed to its upstream name; Ripple branding does not replace upstream copyright/identity inside this imported component.

### white0dew/XiaohongshuSkills / Angiin/Post-to-xhs lineage

- Upstream record: https://github.com/white0dew/XiaohongshuSkills
- License: MIT; Copyright (c) 2026 angiin
- Preserved notice: `LICENSES/MIT-UPSTREAM-NOTICES.txt`
- Used by: historical lineage of `skill-xhs-publisher`; current publishing implementation has been substantially rewritten around Ripple's shared browser publisher.
- Provenance details: `LICENSES/skill-attribution/skill-xhs-publisher.md`
- An unused upstream author-image asset was removed from the release tree; attribution remains in text form.

### jiji262/wechat-publisher lineage

- Upstream: https://github.com/jiji262/wechat-publisher
- License: MIT as declared by the upstream README
- Used by: `skills/ripple/skill-wechat-publisher/` knowledge/assets lineage.
- Provenance details: `LICENSES/skill-attribution/skill-wechat-publisher.md`
- Ripple's native account/publish implementation in `ripple/wechat_adapter.py` is maintained separately from the Skill lineage.

### cclank/news-aggregator-skill lineage

- Upstream: https://github.com/cclank/news-aggregator-skill
- License: MIT as declared by the upstream README
- Used by: `skill-news-intelligence` lineage; Ripple localized the source set and creator-oriented profiles while retaining the aggregation/parsing lineage.
- Provenance details: `LICENSES/skill-attribution/skill-news-intelligence.md`

### First public release exclusion: Douyin publisher

- A previous local development implementation of the Douyin publisher had derivative lineage from `WJZ-P/douyin-upload-mcp-skill` (AGPL-3.0).
- The derived publisher Skill, implementation script, attribution record, and bundled AGPL license text are intentionally excluded from the first public Ripple release tree.
- Ripple therefore does not ship that AGPL-derived Douyin login/publishing component in this release. Douyin trend/planning references and independently written read-only analytics code are separate.

### Other verified upstream licenses

The following major source/reference repositories have an explicit upstream license and remain identified in their per-skill attribution records:

- `charlie947/social-media-skills` — MIT; Copyright (c) 2026 Charlie Hills; source/reference for `post-formatter`, `social-content`, `skill-voice-builder` and related strategy skills. Notice preserved in `LICENSES/MIT-UPSTREAM-NOTICES.txt`.
- `louisedesadeleer/clipify` — MIT; Copyright (c) 2026 Louise de Sadeleer; source of the `clipify` workflow. Notice preserved in `LICENSES/MIT-UPSTREAM-NOTICES.txt`.
- `liangdabiao/ecom-details-image` — MIT as declared by the upstream project; source of the e-commerce image workflow.
- `xpzouying/xiaohongshu-mcp` — Apache-2.0; implementation reference for the current XHS Playwright publisher rewrite.
- `nexu-io/open-design` — Apache-2.0 at repository level, with its own documented per-component exceptions; used as a design-system/skill reference.
- `jiji262/wechat-publisher` — MIT as declared by its upstream README; source of the legacy WeChat publishing Skill lineage. Ripple's native WeChat adapter is separate.

References to an upstream project do not automatically relicense independently written Ripple code. When Ripple carries a copied or derivative component, its file/component license takes precedence over the root license and must remain documented here.

### Trend source notices

See `LICENSES/THIRD_PARTY_TRENDS.md` for the exact MIT notices retained for trend-source integrations.

## Clean-room replacements

`skill-content-calendar` previously carried an uncertain migration lineage. The public release version was independently rewritten around Ripple's current Mother / platform-version / calendar workflow. Historical provenance is retained in `LICENSES/skill-attribution/skill-content-calendar.md`; uncertain upstream text is not the basis of the current implementation.

## Self-developed skills

Entries marked `来源类型: 自研` in `LICENSES/skill-attribution/` describe Ripple-authored implementations. Their Ripple-authored source is covered by Apache-2.0. References to FFmpeg, edge-tts, online model APIs or other dependencies/services identify external tools; those projects/services keep their own licenses and terms and are not relicensed by Ripple.

## Runtime dependencies

Python and npm dependencies are not copied into this source repository by the normal Git checkout. They are installed from their package registries according to `pyproject.toml` and package-lock files. Their own license terms apply.

This notice is an engineering inventory, not a replacement for the license text of any third-party project. If a provenance or license record cannot be verified, the affected copied/ported content should be excluded or independently rewritten before a public release rather than assigned a guessed license.