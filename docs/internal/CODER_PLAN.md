# Coder box plan (approved Nov 2026)

> Status: approved. PR 0 (spike) done; results and the plan changes they caused are in [`CODER_SPIKE.md`](CODER_SPIKE.md) — read that first. Nothing below is built yet.


## Context

You want Jess to be able to code. Today Qwen Code 0.25.3 runs on the Jupiter host (`~/.local/bin/qwen`) with your full user account, talking straight to the Helios brain.

What you asked for:
1. **Jess proposes coding jobs** of two kinds:
   - **scratch:** a small script in an empty throwaway folder.
   - **repo:** a change to Jess's code, handed back as a **patch** you review. It never commits, pushes or deploys.
2. **Nothing runs until you tap Run** on the dashboard. The model has no way to press it.
3. **You can open the same box yourself** over SSH (`jess-coder` on Jupiter) and direct small changes interactively.

Why it's built this way:
- `code_agent` was removed on 2026-10-21 for giving a model shell access, file writes and git commits. Here the model gets a shell **only inside a disposable container** that can't reach the internet, the LAN, Helios or Jupiter's own services.
- Everything shares the brain's single slot, so **Jess's chat and voice always go first.**

## How it works

### The box: a fresh container per session or job
- **One image, `jess-coder`, built from `coder/Dockerfile`.** It starts from the orchestrator image so Jess's pytest suite runs inside. The base comes in through a `BASE_IMAGE` build arg or Compose `additional_contexts`, not the floating local tag.
- **Added to the image:**
  - Node 22, official tarball, sha256-pinned
  - `git`
  - qwen-code, pinned through a committed `package-lock.json` and installed with `npm ci --ignore-scripts`
  - pytest, pytest-asyncio and respx
- **Cleaned out of the image:** `PYTHONPATH` emptied, `/app/orchestrator` removed (so the clone's code is what runs), `HEALTHCHECK NONE`.
- **A read-only system `settings.json` is baked in:**
  - telemetry and update checks off
  - web tools and MCP off
  - a 64K context window
  - long timeouts and retries
  - no key: `envKey JESS_BRAIN_KEY`
  - `baseUrl` set to the orchestrator gate
- **A fresh container every time, never a long-running one.** Each session and each job gets its own `docker run --rm --init`. Nothing a session leaves behind (a background process, an edited file) can reach the next one. The service is build-only under profile `coder` and is never started with `up`.
- **Run flags:**
  - uid 1001
  - `--read-only`, with sized tmpfs for `/tmp` and HOME
  - `--cap-drop ALL`, `no-new-privileges`
  - `--memory`, `--pids-limit`, `--cpus 4`
  - `--log-driver none`, so nothing reaches Loki or the self-audit
  - never named `brain-*`
- **The work folder lives on a fixed-size filesystem**, a 20 GB loop ext4 at `/srv/jess-coder` set up as a one-time root step. A runaway job can't fill the disk the whole stack writes to.
- **The repo comes in as a `git bundle` of `main`**, mounted read-only and cloned inside. The box never sees the checkout (`.env`, `credentials/`, `data/`) or the live `.git` (stray objects, reflogs, unmerged branches).

### The network: `coder-net`
- `internal: true`, with a fixed name, a fixed subnet and a fixed bridge name. `enable_ipv6: false`.
- **The orchestrator joins with a fixed IP** and is the only other member.
- **No host address on the bridge.** I checked: today's internal `searxng-probe-net` still puts 192.168.16.1 on the host. Through that address a container could reach Pi-hole :53 (DNS out to the internet), :22, :8123 and :8888. The fix is `com.docker.network.bridge.gateway_mode_ipv4: isolated` (Docker 29.1.3 is installed), plus a host `INPUT -i <bridge> -j DROP` as a backstop.
- **An isolation self-test is built into the image.** It checks: no external DNS; host and LAN ports, Helios and the internet unreachable; no IPv6; the gate gives 401 without a token and 403 on any other path. It runs in the spike, after every image build on deploy, and at every `jess-coder` start, which refuses to open on failure.

### The gate: the box's only way to the brain (`orchestrator/coder_gate.py`, `routes_coder.py`)
- **Endpoints:** `POST /api/coder/v1/chat/completions` and `GET /api/coder/v1/models`.
- **Tokens:**
  - one random token per session or job
  - stored hashed in SQLite with expiry and revocation, so a deploy restart doesn't kill a 4-hour session
  - accepted only when the request arrives on the orchestrator's coder-net address
- **Guard in `BearerAuthMiddleware` (`orchestrator.py:121`), run before every other exemption** (OPTIONS, `/health`, `/api/audio/`): any request to the coder-net address (`scope["server"]`) may only hit those two exact paths.
- **The upstream request is built fresh:**
  - no client headers are forwarded; `model_auth` attaches the brain key
  - the model name is forced
  - only listed keys pass: messages, tools, tool_choice, stream, temperature (≤1.0), top_p, max_tokens (≤4096), stop
  - `repeat_penalty` 1.0 and `reasoning_effort` low are forced
  - **text-only content**: llama-server fetches image URLs, which is SSRF from Helios
  - tool count, schema size and large min/max bounds are capped
  - `grammar`, `json_schema`, `id_slot`, `cache_prompt`, `logit_bias`, `n>1` and similar knobs are dropped
- **What comes back is stripped:** `timings` and `prompt_tokens_details` are removed. Cached-token counts would let the box guess Jess's cached prompt, which holds your memories and meds, one token at a time.
- **Budgets per token:** requests, tokens and wall time. At wall time the launcher also runs `docker kill`. CLI flags like `--max-tool-calls` are treated as advisory only.

### Chat goes first (new `orchestrator/brain_slot.py`)
- **One shared module for "is the brain busy?"** It replaces the three copies in `self_investigator._slot_busy`, `meal_planner._wait_for_idle_chat` and `auto_learn._wait_for_idle_chat`. Busy means:
  - a chat turn is active (`metrics.ACTIVE_REQUESTS`)
  - a `look_it_up` lookup is running
  - or a coder request is in flight
- **A coder request starts only after chat has been idle for about 20–30 s**, which stops thrashing between turns of one conversation. One coder request at a time.
- **Preemption:**
  - the gate streams from llama-server and buffers the reply, then hands Qwen Code the finished answer in one go
  - if you start talking mid-request, it aborts upstream (llama.cpp stops on disconnect), waits for idle, and re-sends internally
  - Qwen Code never sees a broken stream
  - cost: no live typing in the Qwen Code terminal (1–3 s on tool turns, up to ~30 s on long writes)
- **Background jobs** (auto_learn, meal planner, investigator) queue behind a coder request (≤30 s) instead of backing off for a whole job.
- **Helios:** the launcher wakes it and waits. `helios_power` sleep **refuses** while a session or job is active, because sleep is a hard power cut.
- **Prompt-cache cost gets measured in the spike.** If Jess's cached prefix doesn't survive a coder burst (`--cache-ram`), the plan stretches the idle delay and re-warms Jess afterwards.

### Jess's context for Qwen Code: a compact `QWEN.md` (you asked for this)
- **Loading CLAUDE.md whole won't fit.** It's 147,504 chars, about 37K tokens. With Qwen Code's own ~15K that's ~52K of the brain's 64K window, leaving almost nothing for work.
- **Instead, commit a ~10K-char (~3K-token) `QWEN.md` at the repo root.** Qwen Code loads `QWEN.md` automatically (`context.fileName`), so every clone in the box and every worktree you run host `qwen` in gets it. It contains:
  - what Jess is, and the v7 loop in one paragraph
  - the ~15 load-bearing files
  - the hard rules:
    - no personal content in logs (`# log-safe:`)
    - prompt-cache layout: nothing volatile above `DYNAMIC_CONTEXT_MARKER`, never a second system message
    - schemas in `tool_definitions.py` stay in sync with `tool_handlers.py`
    - env vars go through `config.py` + `shared.py`
    - public repo, no personal data
    - test ids ≤300 chars
  - the in-box test command (deps preinstalled, no `pip install`)
  - what the box can't reach (no internet, Helios, HA, Loki; don't try)
  - **how to get the rest**: CLAUDE.md is in the clone, so grep its headings and notes (e.g. `grep -n "^- \*\*" CLAUDE.md`) and read only the matching section or `docs/` router entry
  - a note to skip CLAUDE.md's Claude-agent review pipeline, which you run yourself
- **Scratch jobs** get a small baked-in `~/.qwen/QWEN.md` instead: the box rules, and where outputs go.
- **Keeping it current:**
  - the docs-updater agent maintains QWEN.md alongside CLAUDE.md
  - a test fails CI if QWEN.md exceeds its size budget or names a file that doesn't exist
  - runs never use `--safe-mode`/`--bare`, which skip context files
- **It doesn't depend on the box,** so it can land first (PR 0.5) and help your existing host `qwen` right away.

### You, interactively: `jess-coder` (`scripts/jess-coder`, installed to `~/.local/bin`)
- **`jess-coder [name]` works like this:**
  1. Reads `API_TOKEN` from the host `.env` and mints a session token (`POST /api/coder/sessions`).
  2. Builds a fresh bundle and records the base SHA.
  3. Runs the self-test.
  4. Opens `docker run --rm -it … qwen`, passing the token as `-e JESS_BRAIN_KEY` by name, so it never shows up in `ps`.
- **The session folder persists under `/srv/jess-coder/sessions/<name>`**, so `jess-coder name` resumes in a fresh container. Inside the box Qwen Code can run with auto-approve, since the box is the safety boundary.
- **`jess-coder patch name`:**
  - the diff comes out as text on stdout; the host never runs git inside the box's folder (a planted `.git/config` could run code on the host)
  - the host checks it with `git apply --stat` against the recorded base
  - by default it refuses: `.github/`, `.claude/`, `CLAUDE.md`, `.mcp.json`, `scripts/`, `install.sh`, compose files, Dockerfiles, `*.service`, `conftest.py`, `requirements*`, `package*.json`, git attribute/module files, symlinks and mode changes
  - those files would execute (CI on Jupiter, Claude Code, pytest) before you've reviewed them. `--allow <path>` overrides one at a time.
- **Then you apply it** in a worktree (`git worktree add … && git apply`) and open a PR as usual.
- **Also:** `jess-coder ls`, `jess-coder rm name`, `jess-coder selftest`.

### Jess's jobs (later PRs)
- **Host dispatcher.** `scripts/coder_dispatcher.py` is stdlib Python running as a systemd **user** unit. It long-polls `GET /api/coder/jobs/next` over localhost with a scoped `CODER_DISPATCH_TOKEN` path token. For each approved job it runs the same `docker run --rm` with the task in a read-only file (never argv), then uploads capped results.
  - The orchestrator still has no docker access, and nothing listens inside the box.
- **Table `coder_jobs`** in `state_store`. Proposals are immutable and carry a content hash. States: `proposed → approved → running → done | failed | cancelled | expired`, with expiry after 24 h. Only a few proposals can be pending at once.
- **Approval:**
  - an inbox card (new kind `coder_job`) says "Jess wants to run a coding job" and opens `/coder/<id>`
  - that page shows the full task word for word, with **Run / Cancel**
  - Run is not on the card itself: card text is cut at 500 chars, and you should approve exactly what will run
  - Run re-checks state, expiry, hash and `CODER_ENABLED`, resolving first so a double tap can't start two jobs (the selfcare `/done` pattern in `routes_inbox.py`)
  - Web Push shows generic text
- **Results:**
  - **repo jobs:** a patch on `/coder/<id>`, downloadable and shown as escaped text, with the same refused-path warnings
  - **scratch jobs:** each output file as escaped text with its own text/plain download; no tarballs (extraction tricks)
  - when a job finishes, an inbox message card links to it
- **The tool: `code_task {action: propose|status|cancel, kind, task ≤1500 chars}`.**
  - default-off `CODER_ENABLED`, hidden via `CODER_TOOL_NAMES`, not on voice
  - `status` returns **only computed fields**: state, exit reason, files changed, test exit code. Never the coder's prose, so a job can't talk Jess into anything
  - a prompt guidance line and an entry in the EXTERNAL CONTENT rule
  - `JESS_SELF_KNOWLEDGE.md` updated, since it currently says she has no code tool

## PR sequence (each one leaves a working state)

| PR | What | You can use it |
|---|---|---|
| **0: spike** (throwaway; results in `docs/internal/CODER_SPIKE.md`) | Prove `gateway_mode_ipv4: isolated` and the self-test. Run Qwen Code against llama-server through a throwaway gate: tool calls, the 0.25.3 settings keys, retry behaviour, buffered replies. Measure abort latency and the prompt-cache hit on Jess | — |
| **0.5: `QWEN.md`** | Compact Jess briefing for Qwen Code + `test_qwen_md.py` (size budget, referenced paths exist) + docs-updater instruction to keep it in sync | **Yes: host `qwen` in any worktree** |
| **1: orchestrator, dormant** | `brain_slot.py` (consolidates the three idle checks), `coder_gate.py` + `routes_coder.py`, token table, session mint, middleware guard, `coder-net` with a fixed orchestrator IP, sleep refusal, config + metrics. `CODER_ENABLED=false` → 404 | No (safe to stop here) |
| **2: interactive box** | `coder/` image + settings + self-test, `scripts/jess-coder`, CI build-only gate, `docs/CODER.md` (threat model, and why this isn't `code_agent` coming back) | **Yes: `jess-coder`** |
| **3: owner-created jobs** | Dispatcher + user unit, `coder_jobs`, `/coder` page (create, run, results), finish card. Testable end to end with no LLM deciding anything | Yes, from the dashboard |
| **4: Jess proposes** | `code_task` tool, proposal card, prompt rules, self-knowledge + CLAUDE.md/CHANGELOG | Yes, from chat |

**Files touched:**
- **Backend:** `orchestrator/{orchestrator.py, config.py, shared.py, metrics.py, state_store.py, tool_definitions.py, tool_handlers.py, prompt_builder.py, inbox.py, routes_inbox.py, webpush_manager.py, self_investigator.py, meal_planner.py, auto_learn.py, helios_power.py}`
- **Frontend:** `InboxTray.tsx`, `api.ts`, `types.ts`, `(private)/coder/`, `protected-paths.ts` + `middleware.ts`, `nav.ts`
- **Infra:** `docker-compose.yml`, `.github/workflows/ci.yml`

**Patterns reused:**
- `WEBHOOK_TOKEN_PATHS` scoped tokens (`orchestrator.py:136`)
- `validate_grist_config` auto-disable (`config.py:988`)
- `RESEARCH_TOOL_NAMES` gating (`tool_definitions.py:1702`)
- `research_helper._defang` / `lookup_active`
- `meal_planner` single-flight + restart recovery
- inbox resolve-first / `reopen_item`
- `test_searxng_hardening.py` helpers, for a new `test_coder_hardening.py`

**CI:**
- deploy builds the image only when `coder/` changes and `.env` has `CODER_ENABLED=true`, tags `:current`, runs the self-test, and fails the deploy if it fails
- it never starts a container
- `COMPOSE_PROFILES` stays empty
- the GitHub-hosted `docker-build` job also builds the image when `coder/` changes

## Steps only you can do (PR 2)
1. Create the 20 GB loop filesystem at `/srv/jess-coder` (root, one-time; exact commands go in `docs/CODER.md`).
2. Add the host firewall backstop rule for the coder bridge.
3. Add `CODER_ENABLED=true` to Jupiter `.env`. PR 3 also needs `CODER_DISPATCH_TOKEN`, ≥24 chars.
4. Install `jess-coder` (and later the dispatcher user unit).

## Verification
- **The pipeline CLAUDE.md requires on every PR:**
  - code-reviewer, security and prod-support, plus **hacker**, since routes and auth change. Hacker attacks the gate from inside the box: other paths, header tricks, image parts, grammar bombs, cached-token probing, using the token from the LAN.
  - frontend review for PRs 3 and 4
  - unit-test, in a throwaway container
  - docs-updater last
- **Tests:**
  - `test_coder_hardening.py`: static compose and run-flag asserts. The network is internal and isolated with only the orchestrator as a member; no ports, socket, `extra_hosts` or checkout mount; read-only; caps dropped; limits set.
  - `test_coder_gate.py`: tokens, address guard, key allow-list, clamps, text-only content, response stripping, budgets, preemption replay.
  - `test_brain_slot.py`
  - `test_coder_jobs.py`: state machine, double tap, expiry, hash mismatch, restart recovery.
  - the no-personal-content-in-logs guard covers the new modules (ids and counters only)
- **Live checks:**
  - PR 2: `jess-coder selftest` passes. Plant a one-line bug, have Qwen Code fix it and run pytest inside, then `jess-coder patch` and `git apply --check` in a worktree.
  - Ask Jess a voice question mid-session: the answer arrives at normal speed, and `bgw_chat_ttft_seconds` stays flat.
  - The dispatcher kills a job at wall time.
- **Metrics:**
  - `bgw_coder_requests_total{origin,result}`, `bgw_coder_wait_seconds`, `bgw_coder_preempted_total`, `bgw_coder_tokens_total`, `bgw_coder_jobs_total{kind,result}`
  - a Grafana row
  - a quiet `CoderSelfTestFailing` alert

## Known limits (accepted)
- **Inside the box the model can do anything:** read and break its own copy, and lie in its summary. You review the patch, and Jess sees only computed fields.
- **Context is the tight resource.** The fix is to shrink Qwen Code's ~15K-token overhead:
  - `--exclude-tools` for everything the box can't use: web, MCP, subagents, workflows, goals, channels
  - a lean `--append-system-prompt` instead of extras
  - the compact QWEN.md
  - target ≤6K tokens, measured in the spike
  - more brain context is coming separately: draft PR #172 takes the brain to 262K once VoxCPM2 moves to Saturn's RTX 3090. Until that cutover, the box's `contextWindowSize` stays at 65536; afterwards it follows the brain's real `-c`. Never set it higher than the brain has, or Qwen Code overflows instead of compacting.
  - big repo tasks may still come back partial
- **No internet inside the box:** `pip`/`npm install` fail. It has only what the image ships.
- **A coding job makes Jess's background work slower** (by ≤30 s per call), never her chat.
