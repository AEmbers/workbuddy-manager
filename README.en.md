<div align="center">

# WorkBuddy Manager

**Management console for Tencent CodeBuddy account pools · OpenAI-compatible gateway**

A web console and OpenAI-compatible gateway for Tencent CodeBuddy account pools: QR onboarding, scheduled check-in and keep-alive, key distribution, IP and model allowlists, request logs and usage stats. After the original project stopped public maintenance, AEmbers continues development here; the upstream Go source is maintained as patches and ships inside the release package (MIT) — see the [deployment guide](deploy/README.md#〇上游源码从哪来随发布包分发) for migration.

![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-22c55e)

[![Release](https://img.shields.io/github/v/release/AEmbers/workbuddy-manager?color=22c55e&label=Release)](https://github.com/AEmbers/workbuddy-manager/releases)
[![Changelog](https://img.shields.io/badge/Changelog-CHANGELOG-blue)](CHANGELOG.md)
[![Issues](https://img.shields.io/github/issues/AEmbers/workbuddy-manager?color=f59e0b&label=Issues)](https://github.com/AEmbers/workbuddy-manager/issues)

**English** · [简体中文](README.md)

> **Maintenance status**: This is the **community continuation** of WorkBuddy
> Manager, independently maintained by **AEmbers** after the original project
> stopped updating. The console, release packages, Windows installer, container
> images and upstream source patches are now maintained at
> [AEmbers/workbuddy-manager](https://github.com/AEmbers/workbuddy-manager).

<p align="center">
  <strong>⚠️ Intended use:</strong> this project is only for managing accounts that <strong>you or your family members have voluntarily authorized</strong>. Bulk registration, account trading, registration-on-behalf, SMS-code farming, account theft, bypassing platform risk controls, resale, or any for-profit use are prohibited. By using this project you agree to the <a href="#intended-use--disclaimer">full disclaimer</a>.
</p>

<img src="docs/images/dashboard.png" alt="WorkBuddy Manager dashboard" width="100%" />

**Interface preview**

</div>

---

## What is this

WorkBuddy Manager is a self-hosted console and OpenAI-compatible gateway for Tencent
CodeBuddy account pools. It brings accounts, keys, proxies, usage, logs, scheduled tasks
and the upstream service together in one web interface:

| What you used to do | What you do now |
|---|---|
| Run `login.sh` on the server to scan a QR code | Click "Add account", scan, and it is checked in and managed automatically |
| `curl /status` to see which account is down | Dashboard shows health, cooldown and token expiry in real time |
| Hand-edit `config.json` for check-in / concurrency | Visual settings with toggles and number inputs |
| Every downstream client shares one global key | Issue multiple keys, each with its own quota, IP and model allowlist |
| No idea who consumed how much | Every call is logged: model, tokens, latency, source IP |
| No IP protection at all | Inbound allow/deny lists plus per-key IP limits and allowlists |

**Maintenance relationship**: this project continues development after the original
WorkBuddy Manager and workbuddy2api stopped public maintenance. The console, Go upstream
patches, issues, releases, Windows installer and container images are maintained by
AEmbers. The historical projects are credited as code origins under MIT; this does not
mean that their authors still participate or that either side sponsors, certifies or
endorses this continuation.

Contributions are welcome; issues for both the console and bundled upstream source belong
in [this repository](https://github.com/AEmbers/workbuddy-manager/issues).

---

## Highlights

### Accounts

- **QR onboarding** — WeChat / QQ scan-to-authorize, then daily check-in, auth file write and upstream reload
- **Token monitoring** — expiry bars with warnings, one-click renewal, connectivity probe, manual check-in
- **Account groups** — several pools side by side; lists, check-in and keys stay per-group
- **Credits & expiry** — live balance, graded colouring, countdown for soon-to-expire credits
- **Task history** — check-in / travel / activity / keep-alive results and credit flow, paged

### Gateway (`/v1`)

- **OpenAI compatible** — standard SDKs connect directly; streaming and non-streaming
- **Key distribution** — per-key realm, expiry, IP count, IP / model allowlists and quota
- **Model aliases** — map the names your downstream uses onto the real models
- **Rate-limit fallback chains** — on an explicit 6004 or exhausted pool, try up
  to three configured fallback models while carrying cooldown and retry-stop headers
- **Model centre** — every usable model on one page: context, max output, thinking tiers
- **Full audit trail** — key, IP, model, status, TTFB, billing and cache hits per call

### Visual settings

- **Scheduled tasks** — check-in / travel / activity / keep-alive, each with its own switch and time
- **Multiple upstreams** — configure several instances, each with its own address and `api_key`
- **Security** — inbound IP allow / deny lists (CIDR), audit log, separate session and API tokens
- **Updates & backup** — one-click update (checksum-verified), off-site PostgreSQL backup
- **Five locales** — Simplified / Traditional Chinese, English, Japanese, Korean

The exhaustive list (with detail per item) lives in [the full feature list](docs/features.md).

---

## Screenshots

<img src="docs/images/accounts.png" alt="Accounts" width="100%" />

Accounts: group switcher, availability tiers, credits and expiry

<img src="docs/images/keys.png" alt="API keys" width="100%" />

Keys: realm, quota, IP and model allowlists

<img src="docs/images/stats.png" alt="Usage stats" width="100%" />

Usage: daily / hourly, failures and latency

<img src="docs/images/logs.png" alt="Request logs" width="100%" />

Request logs: model, status, TTFB and billing per call

More screenshots: [full gallery](docs/screenshots.md).

---

## Quick start

You need a server that can run Docker (1 vCPU / 1 GB is enough). Pick one:

**① Docker (run the container yourself)**

```bash
docker run -d --name workbuddy-manager -p 7864:7864 -v /opt/workbuddy-manager/data:/app/data ghcr.io/aembers/workbuddy-manager:latest
```

**② One-shot installer (dependencies, package, systemd)**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/AEmbers/workbuddy-manager/main/deploy/install.sh)
```

Then open `http://<your-server>:7864`; the initial admin password is printed in the first
startup log.

> The upstream source ships inside the release package, so a fresh install just works.
> Reverse proxy / HTTPS, port hardening, Windows (no Docker) and local development are
> covered in the [deployment guide](deploy/README.md) and
> [development notes](docs/development.md).

---

## Usage

Three steps in daily use: **onboard accounts → hand out keys → point your downstream at
`/v1`**. How each step works, client snippets (OpenAI SDK, cc-switch, …) and the FAQ live
in [the usage guide](docs/usage.md).

---

## API reference

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | gateway key | OpenAI-compatible chat (streaming / non-streaming) |
| `POST` | `/v2/chat/completions` | gateway key | Same as above (v2 path) |
| `POST` | `/v1/responses` `/responses` | gateway key | OpenAI Responses API compatible (streaming / non-streaming) |
| `POST` | `/v1/messages` | gateway key | Anthropic Messages API compatible (Claude Code, etc.) |
| `POST` | `/v1/messages/count_tokens` | gateway key | Rough input-token estimate (by character count) |
| `GET` | `/v1/models` | gateway key | Model list |
| `GET` | `/v1/usage` | gateway key | Self-check for one key: used / remaining quota, expiry, model scope (reads panel records; makes no call) |
| `GET` | `/healthz` | none | Liveness probe (includes upstream connectivity) |
| `GET` | `/api/me` | session | Current user |
| `POST` | `/api/login` `/api/logout` | none | Sign in / out |
| `GET` | `/api/accounts` | session | Account list |
| `GET` | `/api/accounts/credits-snapshot` | session / read-only token | Read-only snapshot of recorded credits (dashboards / reconciliation; never queries upstream) |
| `POST` | `/api/auth/start` `/api/auth/poll` | admin | QR authorisation flow |
| `POST` | `/api/accounts/{file}/checkin` `/test` `/refresh` | admin | Check-in / probe / refresh |
| `DELETE` | `/api/accounts/{file}` | admin | Delete an account |
| `GET/POST/PATCH/DELETE` | `/api/keys[/{id}]` | session / admin | Key management (handed to downstream callers) |
| `GET/POST/PATCH/DELETE` | `/api/upstreams[/{id}]` | session (admin) | Multiple upstream endpoints (a key bound to one = account-pool group); `POST /api/upstreams/{id}/probe` checks connectivity |
| `GET/POST/PATCH/DELETE` | `/api/tokens[/{id}]` | session (admin) | Admin API tokens (for scripts / CI, see [docs/api-tokens.md](docs/api-tokens.md)) |
| `GET` | `/api/logs` `/api/stats/*` | session | Logs and usage |
| `GET/POST/DELETE` | `/api/security/*` | session / admin | IP rules and audit |
| `GET/POST` | `/api/settings/*` | session / admin | Upstream config, model mapping |
| `GET/POST` | `/api/settings/pg-sync*` | session (admin) | PostgreSQL off-site backup: config / test / migrate / restore |

Admin API details are available at `/docs` (Swagger UI) when enabled.

---

## Security

- Keys are shown once and stored as hashes; account credentials never reach the browser
- Inbound IP allow / deny lists (CIDR) plus per-key IP and model allowlists
- Every sensitive action is audited (who, when, what)
- One-click updates verify the published SHA-256 and file size before installing

Threat model and hardening notes: [security notes](docs/security-notes.md) and the
[security audit report](docs/SECURITY-AUDIT.md).

---

## Known limitations

- **No outbound IP pool**: only **inbound** IP control is implemented. Binding a dedicated
  egress IP / proxy per Tencent account would require proxy-pool support in the upstream Go
  service and is out of scope here.
- Request and response **bodies are not stored** — only metadata (model, status, tokens,
  latency, source), to protect privacy.
- Usage is aggregated per day × key × model; hourly granularity would require extending the
  `usage_daily` table.

---

## More docs

- [Usage guide](docs/usage.md) — the three steps and client integrations
- [Full feature list](docs/features.md) — every feature in detail
- [Screenshot gallery](docs/screenshots.md) — all screenshots
- [Development & architecture](docs/development.md) — local dev, architecture, layout
- [Security notes](docs/security-notes.md) — threat model and hardening
- [Deployment guide](deploy/README.md) — reverse proxy, HTTPS, Windows, updates
- [Account proxy routes](docs/account-proxy-routes.md) — per-account egress
- [API tokens](docs/api-tokens.md) — read-only tokens and scopes

---

## Changelog & feedback

- **Changelog**: [CHANGELOG.md](CHANGELOG.md) — additions, fixes and changes per version
- **Releases**: [Releases](https://github.com/AEmbers/workbuddy-manager/releases) — each
  version ships a deployable `.tar.gz` / `.zip` (with the built frontend); unpack and run
  `sudo bash deploy/install.sh`
- **Feedback**: [report a bug](https://github.com/AEmbers/workbuddy-manager/issues/new?template=bug_report.yml) ·
  [request a feature](https://github.com/AEmbers/workbuddy-manager/issues/new?template=feature_request.yml)

> Please include the version and error logs, and **remove any keys or tokens first**.
> Issues with the bundled workbuddy2api source also belong in
> [this repository](https://github.com/AEmbers/workbuddy-manager/issues).

### Release process

Maintainers only need to push a tag:

```bash
git tag v1.0.1 && git push origin v1.0.1
```

CI builds the frontend, packages the artifacts, extracts the matching CHANGELOG section as
release notes, and creates a Release with the archives attached.

---

## Related projects

- [**sanguine886/workbuddy-sdk**](https://github.com/sanguine886/workbuddy-sdk) (Go, MIT) —
  a community-maintained Go client library covering both of this project's API surfaces:
  the control plane `/api/*` (accounts, keys, stats, logs, security, settings, users,
  updates) and the data plane `/v1/*` (Chat Completions / Responses / Anthropic
  Messages / Models). Handy for Go tooling — `go get` it instead of wiring HTTP and
  session auth by hand.

> A community project with **no code dependency on this repository**; please report
> issues to [its tracker](https://github.com/sanguine886/workbuddy-sdk/issues).

---

## Credits

- [**linux-do/cdk**](https://github.com/linux-do/cdk) (MIT) — design tokens and floating
  dock component; this project's UI follows its visual language
- [**ithtelab/workbuddy-manager**](https://github.com/ithtelab/workbuddy-manager) — the
  predecessor console continued by this project (MIT; stopped after v2.8.3)
- [**Sliverkiss/workbuddy2api**](https://github.com/Sliverkiss/workbuddy2api) — the account
  pool and OpenAI-compatible proxy underneath (MIT; its source is distributed with
  this project's releases)
- [**lbjlaq/Antigravity-Manager**](https://github.com/lbjlaq/Antigravity-Manager) — feature
  reference for the console

---

## Intended use & disclaimer

**This project exists to help individuals manage accounts they or their family members have voluntarily authorized. It is not designed for bulk operations or commercial gain.**

Permitted use:

- Managing Tencent CodeBuddy accounts that you own or that your family members have explicitly authorized you to manage;
- Learning, research and personal use on your own devices or private network.

Prohibited use:

- Bulk registration, registration-on-behalf, SMS-code farming, or buying, selling, renting or trading accounts;
- Account theft, credential stuffing, or bypassing identity verification, risk controls, regional restrictions or other platform security mechanisms;
- Charging for, reselling, lead-generating with, or otherwise monetizing this project or any derived service;
- Any use that violates Tencent's terms of service, applicable laws, or the rights of others.

You are responsible for confirming that your account authorization and use are lawful. Any account ban, data loss, service interruption or legal liability caused by abuse, misuse or violation of these boundaries is the user's responsibility; the authors and contributors are not responsible for your actions and provide no warranty of any kind.

This project is not affiliated with or endorsed by Tencent or CodeBuddy. Related trademarks belong to their respective owners. The project is provided "as is" under the [MIT](LICENSE) license.

---

## License

[MIT](LICENSE)
