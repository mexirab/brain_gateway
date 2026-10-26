# Developer guide

This is the contributor / hacker doc. End users installing Brain Gateway should read [`README.md`](../README.md). AI coding assistants should read [`CLAUDE.md`](../CLAUDE.md) for the codebase tour + post-change pipeline.

---

## Architecture in 30 seconds

```
User → Dashboard /chat (or any OpenAI client) → Orchestrator → Unified agentic loop
                                                                │
                                                                ├─► Primary LLM (vLLM, Qwen3.6-27B INT4)
                                                                ├─► ChromaDB (mempalace memory)
                                                                ├─► Home Assistant API
                                                                ├─► TTS (Qwen3-TTS) / STT (Parakeet TDT)
                                                                ├─► SearXNG web search
                                                                └─► Tool registry (decorator-based)
```

**Single model, single loop.** No router, no delegation, no v6 hybrid. The primary handles conversation *and* tool execution in one OpenAI-style function-calling loop. See `ARCHITECTURE.md` for the full data flow.

---

## Repo layout

| Path | What's there |
|------|--------------|
| `orchestrator/` | FastAPI app, unified loop, tool handlers, all the brain logic |
| `frontend/` | Next.js 14 dashboard (App Router); served on port 3001 |
| `tts/` | Qwen3-TTS server + Parakeet STT server + Wyoming bridges |
| `scripts/` | One-shot helpers: `detect_hardware.sh`, `model_layer_smoketest.sh`, `setup.sh`, `reindex_rag.py` |
| `monitoring/` | Prometheus + Grafana + Loki stack (docker compose, advanced profile) |
| `docs/` | User-facing docs (install, hardware, env vars, feature guides) |
| `docs/internal/` | Maintainer's Helios runbook + historical migration plans |
| `jess-features/` | ADHD feature specs (F-001 → F-014), one per file |
| `rag/` | RAG ingest script |
| `monitoring/grafana/dashgen/` | Python generator for the Grafana dashboards (don't hand-edit the JSON) |

The full file map is in [`CLAUDE.md`](../CLAUDE.md) → **Key Files**.

---

## Local development

```bash
# Rebuild + restart the orchestrator after a Python change
docker compose up -d --build orchestrator

# Tail orchestrator logs
docker logs brain-orchestrator --tail 50 -f

# Run the test suite (tests live in orchestrator/tests/; throwaway container from the orchestrator image;
# the data/ mount is needed by test_palace_system_wing.py, which reads data/palace.yaml)
docker run --rm -v "$PWD/orchestrator:/app/orchestrator:ro" -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/data:/app/data_repo:ro" -w /app gateway_nerves-orchestrator \
  sh -c "pip install -q pytest pytest-asyncio respx && python -m pytest -o asyncio_mode=auto -p no:cacheprovider orchestrator/tests -q"

# Rebuild the frontend
docker compose up -d --build --force-recreate frontend

# Validate compose stanzas without starting anything
docker compose --profile models config
```

**Post-change pipeline (mandatory).** Every code change runs through a two-phase review pipeline (code-reviewer + security + prod-support + frontend/hacker + unit-test + docs-updater). Full spec in [`CLAUDE.md`](../CLAUDE.md) → **Post-change review workflow**.

**Test ids must stay ≤ 300 chars.** CI runs `pytest -v`, which prints every node id, and a parametrize value with no id becomes the id. `orchestrator/tests/conftest.py` (`MAX_NODEID_CHARS`) fails collection with exit code 4 when any id is longer, printing only the test function, count and length. Give big cases a short id: `pytest.param("9" * 400, id="400-digits")`, or `ids=[...]` on `@pytest.mark.parametrize`. Why: two tests parametrized with ~1 MB strings stalled the CI test job for 25+ min (2026-10-26). Guard tests: `orchestrator/tests/test_nodeid_length_guard.py`.

---

## CI and auto-deploy

`.github/workflows/ci.yml` runs on PRs to `main` and pushes to `main`. The repo is public, so CI logs are too: steps must never print secrets, `.env` values or personal content (the deploy's checkout guard prints only a count of local changes; the SearXNG step prints container state, never its logs).

| Job (`name:`) | Timeout (min) | What |
|---------------|---------------|------|
| Python Lint & Format | 10 | ruff check + format check on `orchestrator/` (ruff version pinned) |
| Grafana Dashboards In Sync | 10 | regenerates `monitoring/grafana/dashgen/`, fails on drift |
| Suite Theme In Sync | 10 | theme generator tests + `theme/build.py` drift check |
| Python Tests | 15 | `pytest orchestrator/tests/ -v` (usually ~4 min) |
| Frontend Lint & Type Check | 15 | ESLint, `tsc --noEmit`, `npm test` |
| Docker Build (orchestrator) | 25 | builds the orchestrator image |
| Deploy to Jupiter | 45 | push to `main` only; self-hosted runner on Jupiter (`actions.runner.mexirab-brain_gateway.jupiter.service`), operates on the live checkout `/home/labadmin/gateway_nerves`; concurrency group `deploy-jupiter`, a running deploy is never cancelled |

**Deploy job steps:**

1. **Fast-forward the deploy checkout.** Refuses unless the checkout is on `main` with no modified tracked files, no untracked files under `orchestrator/` or `frontend/`, and no untracked compose(.override) file at the root (local changes are reported as a count only). `git fetch --no-write-fetch-head origin main` (3 tries), then `git merge --ff-only <github.sha>`: it ships the commit whose CI passed, not main's tip, and only while that commit is still on `origin/main` (force-push guard). Refuses if HEAD has commits not on `origin/main`. If the checkout was already past that commit and the last deploy (a manual pull, or a deploy that stopped partway), it refuses when that commit's CI is not known to have passed or its `ci.yml` differs. **Catch-up:** GitHub keeps one pending run per concurrency group and cancels it when another arrives (in any commit order), so the step also fast-forwards to the newest first-parent commit on `origin/main` (≤20 back) whose push run passed every job except "Deploy to Jupiter" and whose `.github/workflows/ci.yml` is identical to the running one. Reads runs via `gh api` with the job's read-only token (`contents: read`, `actions: read`); an API failure → warning, only `github.sha` ships.
2. **Rebuild changed services.** Diffs `.last-deployed-sha` (what's running) → HEAD with `git diff --no-renames` and rebuilds/recreates only what changed (orchestrator, frontend, SearXNG, Grist, monitoring — per-service rules in the step). A change confined to `orchestrator/tests/` does not rebuild the orchestrator (not in the image; a recreate drops RAM-only state). Writes the marker on success.
3. **Health check.** Orchestrator `:8888/health` and frontend `:3001`, ~40 s each.

**Operator rules:**

- Never `git pull` or edit files in the live checkout. `git status` there can say "behind origin/main" while newer commits are still in CI — normal; their deploys (or a later catch-up) ship them.
- To hold deploys, stop the runner service (`sudo systemctl stop actions.runner.mexirab-brain_gateway.jupiter.service`). Cancelling a deploy run does not hold its commit back if an older deploy is still to run (the catch-up ships it).
- A commit that changes `ci.yml` is never caught up by an older run; if its own deploy run shows as cancelled, re-run it.
- Adding a CI job: list it in the deploy job's `needs:` (the catch-up counts passed jobs against that list). Renaming the deploy job: update the `"Deploy to Jupiter"` string in the catch-up's jq filter.
- A timed-out deploy writes no marker: the next deploy (or a re-run) rebuilds the same changes; until then the box may be half-deployed.

Why (2026-10-26): a deploy failed with "Cannot fast-forward to multiple branches" because a `git fetch` run in the live checkout rewrote `.git/FETCH_HEAD` under the job's `git pull`, and deploys could ship commits whose CI hadn't finished.

---

## Reference cluster

The maintainer's deployment is a 4-node cluster centered on Helios. Brain Gateway itself runs as a single-box appliance — the cluster shape is **not** required for end users.

| Node | IP | GPU | Role |
|------|-----|-----|------|
| Helios | 10.0.0.195 | RTX 5090 + RTX PRO 5000 | GPU model layer only: primary LLM, TTS, STT (the code agent model was retired 2026-10-16 and the `code_agent` tool removed 2026-10-21). Power-tiered — asleep most of the time, woken on demand via an HA smart plug. |
| Jupiter | 10.0.0.248 | — | Always-on hub: orchestrator (:8888), frontend (:3001, served as https://<host>.<tailnet>.ts.net via Tailscale Serve), Home Assistant (:8123), monitoring stack (Prometheus, Grafana, Loki), Pi-hole primary |
| Saturn | 10.0.0.58 | RTX 3080 + RTX 3090 | Vision model (3080), expert reasoner Qwen3-32B (3090), Pi-hole secondary, off-box backup target |
| Uranus | 10.0.0.173 | 2× RTX 5080 | Test box; currently unreachable (not part of the runtime path) |

For Helios-specific details (Tailscale cert, GPU layout, kiosk display, etc.), see [`docs/internal/HELIOS_INFRASTRUCTURE.md`](internal/HELIOS_INFRASTRUCTURE.md).

---

## Contributing

1. Read [`CLAUDE.md`](../CLAUDE.md) end-to-end — it's the briefing.
2. Pick something from [`ROADMAP.md`](../ROADMAP.md) or [`jess-features/`](../jess-features/).
3. Open a branch, make the change, run the post-change pipeline.
4. Open a PR. Small, single-purpose PRs land fastest.

Bug reports and feature requests: file an issue with reproduction steps and the relevant `brain-orchestrator` logs.

---

## Where the user-facing docs live

| File | Audience |
|------|----------|
| [`README.md`](../README.md) | End user installing Brain Gateway |
| [`docs/INSTALL.md`](INSTALL.md) | Detailed install procedure |
| [`docs/HARDWARE.md`](HARDWARE.md) | GPU/VRAM tier matrix |
| [`docs/UPGRADE.md`](UPGRADE.md) | Release-to-release upgrade path |
| [`docs/JESS_QUICK_START.md`](JESS_QUICK_START.md) | Voice command reference |
| [`docs/ENV_VARS.md`](ENV_VARS.md) | Every environment variable |
| [`CHANGELOG.md`](../CHANGELOG.md) | Release notes |
