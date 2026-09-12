[中文](README.md) | **English**

# Ripple

Ripple is a local-first content workbench for content creators. The current version is **0.2.7**.

It brings ideation, Mother content, platform variants, assets, Agent collaboration, account connections, review, and publishing receipts into one workflow. Real publishing, login confirmation, and interactive write operations always require explicit user action.

## Current capabilities

- **Topics and trends**: trend radar, topic library, and account-profile-assisted recommendations.
- **Content workbench**: Mother content, assets/final outputs, platform variants, and AI collaboration.
- **Publishing management**: preflight checks, variant review, immediate or scheduled execution, and receipt verification. Ripple avoids blind retries when a publishing result is uncertain.
- **Agents**: detects and reuses existing OpenCode, Claude Code, and Codex installations. Hermes can be connected when local runtime requirements are satisfied. Installing Ripple does not overwrite existing Agent configuration.
- **Platform connections**: implemented local or official integration paths for X, WeChat Official Accounts, Xiaohongshu, Kuaishou, WeChat Channels, Zhihu, Bilibili, and others. The first public release does not bundle a Douyin login/direct-publishing adapter because the previous implementation had AGPL lineage and was excluded from the release tree. Douyin trend features and read-only compatibility with existing local Profiles remain available. TikTok is still planned and the UI does not present planned capabilities as working integrations.
- **Blog Connector**: connects to compatible blogs through a controlled OpenAPI flow and also supports Markdown/asset export.

## Quick start

### Windows

Requires Python 3.10+ and Node.js 22.19+. FFmpeg is recommended for video/audio processing.

```powershell
git clone https://github.com/Solaris-star/Ripple.git
cd Ripple
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-Ripple.ps1
```

Open `http://127.0.0.1:7860` in your browser.

`setup.ps1` only creates `.venv` inside the repository, installs project dependencies, and builds the frontend. It does not globally install or modify OpenCode / Claude Code / Codex / Hermes.

### Linux / macOS

```bash
git clone https://github.com/Solaris-star/Ripple.git
cd Ripple
bash setup.sh
.venv/bin/python -X utf8 web/app.py
```

Ripple stores private credentials using machine-local encryption. Windows uses DPAPI; Linux/macOS uses AES-256-GCM with a machine key stored at `~/.config/ripple/secret.key` with `0600` permissions. You can override that path with the absolute-path environment variable `RIPPLE_SECRET_KEY_FILE`. Ciphertext is intentionally not designed to be portable across machines; after moving a Server deployment, platform accounts should be reconnected.

The current release baseline primarily targets **local Windows operation**. Linux/macOS support the core installer, Web service, and Secret Store paths and are covered by CI. Integrations involving real browser login, desktop Agents, media tooling, or platform anti-abuse controls still need host-specific validation. A successful `setup.sh` run does not imply that every platform connection has completed end-to-end Linux/macOS Server validation.

## Local and Server modes

The default mode is `local`: Ripple listens on the loopback interface and runs as a single-user local application without login.

Server mode must be placed behind an HTTPS reverse proxy and configured with a trusted origin, trusted hosts, and a deployer bootstrap code:

```text
RIPPLE_DEPLOYMENT_MODE=server
RIPPLE_PUBLIC_ORIGIN=https://ripple.example.com
RIPPLE_TRUSTED_HOSTS=ripple.example.com
RIPPLE_BOOTSTRAP_CODE=<one-time-high-entropy-value-at-least-24-characters>
```

After the first Owner account is created, remove `RIPPLE_BOOTSTRAP_CODE` from the runtime environment and restart the service. Server mode uses HttpOnly session cookies, CSRF validation, and Workspace access boundaries. The current version exposes one default Workspace and does not claim full SaaS-grade multi-tenant isolation.

## Data and privacy

Runtime data is separated by default into:

- `outputs/`: user content, assets, and task outputs;
- `.ripple-private/`: account credentials, browser Profiles, private connections, and operation state;
- `.env`: local model/API configuration.

These paths should never be committed to Git. The repository includes `.gitignore`, but an independent secret scan is still recommended before publishing changes.

Do not place real Cookies, Tokens, AppSecrets, browser Profiles, or personal media in issues, test fixtures, logs, or Pull Requests.

## Development and validation

```powershell
python -m pytest -q
cd web\frontend
npm ci
npm run build
npm run lint
npm audit
```

For fuller development guidance, Server configuration, platform limitations, and release checks, see:

- `docs/ripple/DEVELOPMENT.md`
- `docs/ripple/RELEASE_CHECKLIST.md`
- `SECURITY.md`
- `CONTRIBUTING.md`

## License and third-party sources

Ripple-authored code is released under the Apache License 2.0 in the root `LICENSE` file. The repository includes or adapts some third-party Skills and reference implementations; their sources, licenses, and preserved notices are documented in `THIRD_PARTY_NOTICES.md` and `LICENSES/`.

Third-party names are used only for compatibility, attribution, or identification of real integration targets. They do not imply endorsement of Ripple.
