# Upgrade guide

How to safely move a Brain Gateway install from one release to the next, including the pre-v1.0.0 → v1.0.0 transition for anyone who has been tracking `main`.

If you're installing fresh, you don't need this page — read [`INSTALL.md`](INSTALL.md) instead.

---

## TL;DR

```bash
cd /opt/gateway_mvp
git fetch --tags
git checkout v1.0.0           # or any later release tag
docker compose pull           # pull updated container images
docker compose up -d --build  # rebuild + restart
```

The setup wizard does **not** re-run on upgrade — `data/app/setup_state.json` records that setup is complete and the wizard endpoints stay locked at HTTP 410. To change settings, use the `/settings` page in the dashboard.

---

## Before any upgrade

```bash
# 1. Pin the current state in case you need to roll back
git rev-parse HEAD > /tmp/brain-gateway-pre-upgrade.sha
docker compose ps > /tmp/brain-gateway-pre-upgrade.ps

# 2. Back up the data directory (state, palace, schedules, overrides)
tar czf /tmp/brain-gateway-data-$(date +%F).tgz data/

# 3. Confirm the orchestrator is healthy *before* you start
curl -s http://localhost:8888/health | jq .
```

The `data/` directory holds everything that survives a rebuild: `setup_state.json`, `setup_overrides.env`, `selfcare_schedule.yaml`, `routines.yaml`, `announcement_routes.yaml`, ChromaDB persistence, SQLite state, meal photos, training corpus, paperless inbox. Back it up before any non-trivial upgrade.

---

## Standard upgrade (release → release)

```bash
cd /opt/gateway_mvp

# Pull tags + see what's available
git fetch --tags
git tag --sort=-creatordate | head -5

# Check the changelog for the target version
git show v1.X.Y:CHANGELOG.md | less     # or read on GitHub

# Move to the new version
git checkout v1.X.Y
docker compose pull
docker compose up -d --build

# Verify
curl -s http://localhost:8888/health
docker compose ps
```

If the changelog says new env vars or new compose services landed, re-read `.env.example` and merge anything new into your `.env`.

---

## Pre-v1.0.0 → v1.0.0 (one-time migration)

If you've been tracking `main` for the past few months, you already have most of v1.0.0's surface area. The migration is mostly about taking advantage of the new defaults rather than fixing anything broken.

```bash
cd /opt/gateway_mvp
git fetch --tags
git checkout v1.0.0
```

Then check each of these:

### 1. `setup_state.json` already exists?

If yes, the wizard endpoints are already locked. Good. You can ignore the wizard and continue using your existing `.env`.

```bash
cat data/app/setup_state.json
# {"setup_completed": true, "completed_at": "2026-..."}
```

If no, the wizard is live at `/setup`. You can either:
- Walk through it (it will read your existing `.env` values where it can and offer to overwrite).
- Skip it by writing `setup_state.json` manually: `echo '{"setup_completed": true, "completed_at": "'$(date -u +%FT%TZ)'"}' > data/app/setup_state.json`.

### 2. New env defaults to be aware of

| Variable | Old default | New default | Action |
|----------|-------------|-------------|--------|
| `TTS_VOICE` | `jessica` | `default` | If you want to keep the personal cloned voice, add `TTS_VOICE=jessica` to `.env` |
| `COMPOSE_PROFILES` | unset → all services started | unset → default profile only | If you relied on `nebula-sync` / `promtail` / `nut-exporter`, add `COMPOSE_PROFILES=advanced` |
| `JESS_ADVANCED` | implicit `true` (no gate) | explicit `false` | If you use `code_agent` / `ask_expert` / `query_budget` / `finance_status` / `check_claude_activity`, add `JESS_ADVANCED=true` |
| `NUT_EXPORTER_PASSWORD` | `:?` (hard-required) | `:-` (soft) | No action; just means fresh installs no longer fail compose validation |
| `LOKI_PUSH_URL` | hard-required by `:?` | soft `:-` | If you use promtail, ensure `LOKI_PUSH_URL` is still set in `.env` |
| `MODEL_URL` | `http://${NODE_HELIOS_IP}:${SERVICE_MODEL_PORT:-8080}/v1` | `http://vllm-primary:8000/v1` | If you run vLLM as a host systemd unit (Helios pattern), pin `MODEL_URL=http://<host-ip>:8080/v1` in your `.env`. Compose-managed `vllm-primary` just works with the new default. |
| `MODEL_NAME` | `Qwen3.5-27B` | `qwen3.6-27b-int4` | Matches the post-Phase-3 `VLLM_SERVED_NAME`. If you point at a different model, set this to that model's served name. |
| `TTS_URL` | `http://localhost:8002` | `http://qwen-tts:8002` | Pin to `http://localhost:8002` if TTS runs as a host systemd unit. |
| `STT_URL` | `http://localhost:8003` | `http://parakeet-stt:8003` | Pin to `http://localhost:8003` if STT runs as a host systemd unit. |
| `GATEWAY_ROOT_PATH` | `/opt/gateway_mvp` | _empty (install.sh fills it)_ | Set to the absolute path of your repo (`$(git rev-parse --show-toplevel)`). Bind-mounts silently break with an empty value at non-standard install paths. |
| `DASHBOARD_TOKEN` | _missing → frontend fell back to `'changeme'`_ | _empty → still falls back to `'changeme'`_ | Generate a real one: `python3 -c "import secrets; print('DASHBOARD_TOKEN=' + secrets.token_urlsafe(24))" >> .env`. `install.sh` does this on fresh installs. |
| `JESS_LAN_IP` | _did not exist_ | _empty → welcome shows `<your-box-ip>` placeholder_ | Set to the box's LAN IP (`ip -4 route get 1.1.1.1 \| awk '/src/ {print $7}'`) so the first-chat welcome's `/settings` link is clickable. |
| `VLLM_EXTRA_ARGS` | _did not exist_ | _Lorbus-27B tuning string in `docker-compose.yml`_ | If you run a non-Lorbus model in compose's `vllm-primary` (e.g. an 8B AWQ on a below-floor card), override to `--tool-call-parser hermes` — the default's MTP speculative-config crashes on models without MTP weights. |

### 3. Model layer (vLLM / Qwen3-TTS / Parakeet)

The compose stanzas for `vllm-primary`, `qwen-tts`, and `parakeet-stt` are now in `docker-compose.yml` behind a `models` profile. If you've been running them as host systemd units (the Helios pattern), keep doing that — leave `models` *out* of `COMPOSE_PROFILES` so compose doesn't double-start them.

If you want to migrate to compose-managed model servers:
1. Stop the host services: `sudo systemctl stop vllm-primary qwen-tts parakeet-stt`
2. Disable them: `sudo systemctl disable vllm-primary qwen-tts parakeet-stt`
3. Add `models` to `COMPOSE_PROFILES`: `COMPOSE_PROFILES=models` (or `advanced,models`)
4. Repoint `MODEL_URL`, `TTS_URL`, `STT_URL` to compose-internal DNS (e.g. `http://vllm-primary:8080/v1`)
5. `docker compose up -d`

If you use the custom voice clone (cloning-capable `*-Base` model that returns 401 on HuggingFace), set `QWEN_TTS_MODEL_DIR=/path/to/local/Qwen3-TTS-1.7B-Base` so the compose stanza bind-mounts it. Keep the backup tarball — that model is gated and not re-downloadable.

### 4. Doc layout shifted

These pages moved. If you bookmarked them, update:

| Old path | New path |
|----------|----------|
| `docs/INFRASTRUCTURE.md` | `docs/internal/HELIOS_INFRASTRUCTURE.md` (maintainer reference only) |
| `docs/VLLM_PHASE_3_PLAN.md` | `docs/internal/VLLM_PHASE_3_PLAN.md` |
| `docs/REMOTE_DEV.md` | deleted (was personal workflow) |
| (new) `README.md` | end-user install guide |
| (new) `docs/INSTALL.md` | detailed install procedure |
| (new) `docs/HARDWARE.md` | GPU tier matrix |
| (new) `docs/DEV.md` | contributor / dev guide |

---

## Rolling back

If an upgrade goes badly:

```bash
cd /opt/gateway_mvp
git checkout $(cat /tmp/brain-gateway-pre-upgrade.sha)
docker compose up -d --build
```

If the data directory got migrated forward (a release added new tables / schema fields), restore from your pre-upgrade backup:

```bash
docker compose down
tar xzf /tmp/brain-gateway-data-YYYY-MM-DD.tgz -C /opt/gateway_mvp/
docker compose up -d
```

---

## When NOT to upgrade

- A point release lands while you have an active focus session, ongoing routine, or unconfirmed reminders — wait until those clear. The state SQLite is migrated forward by `state_store.py` on boot, but you'll lose any in-flight delivery acks.
- The changelog mentions a breaking schema change you haven't read. Read it first.
- You're running a customized fork. Merge first, upgrade second.

---

## Where to file upgrade issues

If an upgrade breaks, open a GitHub issue with:
- The from-version + to-version (`git log` SHAs are fine if you're pre-v1)
- The output of `docker compose ps`
- `brain-orchestrator` logs from the failing boot
- Whether you've changed any defaults in `.env`
