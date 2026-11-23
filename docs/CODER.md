# Coder box (`jess-coder`)

A sandboxed [Qwen Code](https://github.com/QwenLM/qwen-code) on Jupiter that uses Jess's brain. You open it over SSH, tell it what to change, and get a **patch** back to review and apply yourself. It never commits, pushes or deploys.

The design and the measurements behind it are in [`internal/CODER_PLAN.md`](internal/CODER_PLAN.md) and [`internal/CODER_SPIKE.md`](internal/CODER_SPIKE.md). The orchestrator side (the coder gate) is in `orchestrator/coder_gate.py`; its docstring has the gate's guarantees.

**Status:**
- **Done:** the interactive box (this doc). Jess-proposed coding jobs (dashboard Run button, `code_task` tool) come in later PRs.
- **Default:** off (`CODER_ENABLED=false`).

## Why this isn't `code_agent` coming back

`code_agent` (removed 2026-10-21) gave a model a shell, file writes and git commits **on the host**. The coder box gives the model a shell **only inside a disposable container** with these limits:

- **Network:** it can't reach the internet, the LAN, Helios, Home Assistant or any Jupiter service.
- **Brain:** it reaches the brain only through the orchestrator's coder gate, with its own short-lived token. Jess's chat and voice always go first.
- **Repo:** it sees a git bundle of `main`, never the live checkout (`.env`, `credentials/`, `data/`, the live `.git`).
- **Output:** its changes leave as a text diff. The host checks the diff and saves it outside the box. You apply it in a worktree and open a PR as usual.

## Threat model

Assume the model inside the box is hostile: prompt-injected by a file it read, or just wrong. **Inside the box it can do anything:** run any command, rewrite its own copy of the repo, and lie in its summary. The design limits where that reaches.

| Boundary | What enforces it | Checked by |
|---|---|---|
| **No network out.** coder-net is `internal` with `gateway_mode_ipv4: isolated`: no route, and no host address on the bridge (a plain internal network still reaches Jupiter's SSH, DNS, HA… through its bridge address). No IPv6. | `docker-compose.yml` (`coder-net`), `test_coder_hardening.py` | the self-test, every session + every deploy |
| **Only the gate.** The orchestrator answers a request from coder-net on its two gate paths only (403 elsewhere). The gate wants a coder token, rebuilds every request from an allow-list, and strips cache counters from replies. | `BearerAuthMiddleware` + `coder_gate.py` | the self-test (403/401 checks), PR 1 tests |
| **No foreign neighbours.** coder-net's only members are the orchestrator and boxes. | `jess-coder` refuses to open a box otherwise | `check_network` |
| **Container.** Read-only root, no capabilities, `no-new-privileges`, its own uid (61001, no account on Jupiter has it), memory/CPU/pids limits, no logs, no Docker socket, a fresh `docker run --rm` every time. | `box_command()` in `scripts/jess-coder` | `test_coder_box.py`, the self-test |
| **Disk.** The workspace lives on its own 20 GB filesystem, so a runaway box can't fill Jupiter's root disk. | `/srv/jess-coder` loop mount (Setup below) | `jess-coder` refuses an unmounted `/srv/jess-coder` |
| **Token.** One per session, 4 h by default (`CODER_SESSION_HOURS`), with a request and token budget. It reaches the box through the environment (never argv) and is revoked when the session ends. The box is killed at expiry. | `jess-coder` + the gate's token store | PR 1 tests |
| **Host side of the patch.** The host never runs git inside a box's folder. The box's files belong to its uid, so host git refuses them as "dubious ownership" instead of running a planted `.git/config` hook or fsmonitor. `jess-coder patch` gets the diff from a second, **offline**, read-only box, which uses a fresh git dir, so the workspace's own config, filter drivers and attributes never run. The host still treats that text as hostile: it applies it to a clean clone of the base (from its own bundle) and takes the file list, the modes and the diff you review and save from **what git shows there**, never the box's bytes. So odd mode lines, decoy hunk lines, "no newline" joins or forged renames can't make what you read differ from what applies. It then refuses files that would run before you review them (deleting one too), symlinks, executables, submodules, binary changes and terminal/Trojan-Source characters on added lines. The output is capped at 8 MB and 10 min. | `check_box_diff` / `review_changes` | `test_coder_box.py` (incl. the reviewers' hostile patches) |

**Refused by default** (`--allow PATH` overrides one, `--allow dir/` a prefix). These would run on Jupiter, in CI, or in your next Claude Code or Qwen Code session before a human read them:
- `.github/`, `.claude/`, `.qwen/`, `.vscode/`, `.idea/`, `.devcontainer/`
- `scripts/`, `coder/`, `tts/`, `monitoring/`, `homeassistant/`, `radicale/`, `actual/`, `grist/`, `models/`
- other agents' and editors' folders: `.gemini/`, `.codex/`, `.cursor/`, `.continue/`, `.zed/`, `.husky/`, `.githooks/`
- `CLAUDE*.md` (incl. `CLAUDE.local.md`), `QWEN*.md`, `AGENTS.md`, `.mcp.json`, `.cursorrules` and the like
- `install.sh`, `Makefile`, `conftest.py`, `pyproject.toml`, `setup.py`, `noxfile.py`, `mypy.ini`, `.pylintrc`, `.coveragerc`
- any `docker-compose*.yml`, `Dockerfile*`, `requirements*.txt`, `*.service`/`*.socket`/`*.timer`/`*.sh`, `.env*`
- JS/TS tool configs that run on lint/test/build: `*.config.{js,mjs,cjs,ts}`, `*.setup.ts`, `.eslintrc*`, `.prettierrc*`, `.babelrc*`
- `package.json`, `package-lock.json`, `.npmrc`, `.yarnrc*`, `.gitattributes`, `.gitmodules`, `.gitignore`, `.lfsconfig`
- any path with characters outside `A-Z a-z 0-9 . _ @ + / -`

Matching ignores case (a macOS checkout folds `.Claude/` into `.claude/`).

**Treat a coder patch like a pull request from a stranger.** The next tools to touch it run on Jupiter as you:
- Read it yourself (`jess-coder patch <name> --show`) before you hand the worktree to Claude Code. Review agents with a shell read the code too, and a planted docstring is a prompt injection.
- Run its tests **offline**, in a box-like container, never on the default bridge (which reaches the LAN, the internet and host services):

  ```bash
  docker run --rm --network none --read-only --cap-drop ALL --user 61001:61001 --tmpfs /tmp --tmpfs /home/coder:uid=61001,gid=61001 --tmpfs /app/data:uid=61001,gid=61001 -v "$PWD":/w:ro -w /w --entrypoint python3 jess-coder:current -m pytest -o asyncio_mode=auto -p no:cacheprovider orchestrator/tests -q
  ```

- Frontend changes: `frontend/` configs are refused, but the repo's Claude Code lint hook runs `eslint` in a worktree that has `node_modules`. Read the patch first.

**Known limits (accepted):**
- **Your terminal.** An interactive session draws on your terminal, so a hostile box can send it escape sequences. Use a terminal that doesn't let remote programs write your clipboard (OSC 52) or change settings (iTerm2: Settings → General → Selection, "Applications in terminal may access clipboard" off), and preferably run sessions inside `tmux` on Jupiter. `jess-coder` flushes pending terminal input when a session ends and makes headless output terminal-safe.
- **Boxes on coder-net can reach each other** (Docker inter-container traffic is on; turning it off would cut the gate too). At most 2 sessions are open at a time.
- **IPv6 rests on Docker's default.** Docker 29 turns IPv6 off in endpoints of IPv4-only networks. With it on, a box would reach Jupiter's SSH and Home Assistant through the bridge's link-local address (measured). The self-test fails in that case. Optional host backstop: a udev rule that sets `net.ipv6.conf.br-jesscoder.disable_ipv6=1`.
- **The workspace persists** between runs of the same session. A box can leave things for its own next run (only its own: each session has its own folder).
- **The brain is shared.** A coder request waits for chat to be idle and gives way to it, so chat and voice stay fast. A long coding burst still makes Jess's background jobs (auto_learn, meal planner, self-audit) wait longer. Watch the Grafana row.
- **The box can read the whole repo history of `main`.** The repo is public anyway.
- **What the model says it did is not evidence.** Read the patch.
- **No internet inside:** `pip install` and `npm install` fail. The box has what the image ships: Jess's Python dependencies, pytest, pytest-asyncio, respx, git and Node 22.

## Setup (owner, one time)

1. **The coder filesystem** (root, 20 GB on the NVMe, mounted `nosuid,nodev`):

   ```bash
   sudo fallocate -l 20G /var/lib/jess-coder.img
   ```

   ```bash
   sudo mkfs.ext4 -q -m 0 -L jess-coder /var/lib/jess-coder.img
   ```

   ```bash
   sudo mkdir -p /srv/jess-coder && echo '/var/lib/jess-coder.img /srv/jess-coder ext4 loop,nosuid,nodev,noatime,nofail 0 0' | sudo tee -a /etc/fstab
   ```

   `nofail`: if the image ever goes missing or needs a repair, Jupiter still boots (HA, Jess and monitoring come up; only `jess-coder` refuses, because `/srv/jess-coder` isn't mounted).

   ```bash
   sudo systemctl daemon-reload && sudo mount /srv/jess-coder && sudo chown labadmin:labadmin /srv/jess-coder && sudo chmod 0700 /srv/jess-coder
   ```

   Check: `findmnt /srv/jess-coder` shows `nosuid,nodev` (`jess-coder` refuses to run without them).

2. **Firewall backstop.** Nothing to add: the container firewall (`jess-docker-input`, PR #176, installed on Jupiter in observe mode) puts every `br-+` bridge, `br-jesscoder` included, through its INPUT allow-list once it enforces. coder-net needs no allow line, because boxes must reach nothing on the host. Until it enforces, the isolated gateway mode is the control (the self-test proves it).

3. **Turn the gate on.** Add `CODER_ENABLED=true` to Jupiter's `.env`, then recreate the orchestrator:

   ```bash
   cd ~/gateway_nerves && docker compose up -d --force-recreate orchestrator
   ```

   Its log says `[CODER] Gate enabled on 10.213.250.2`.

4. **Build the image.** It's labelled with a hash of its inputs (`coder/`, `orchestrator/requirements.txt`, `embedding_pin.py`, the gate address, `CODER_CONTEXT_WINDOW`); `jess-coder` refuses an image whose label no longer matches, and every deploy with `CODER_ENABLED=true` rebuilds it then (`build --if-stale`). Old box images are pruned after a build:

   ```bash
   cd ~/gateway_nerves && python3 scripts/jess-coder build
   ```

5. **Install the command.** It's a symlink into the live checkout, so deploys update it:

   ```bash
   ln -sf ~/gateway_nerves/scripts/jess-coder ~/.local/bin/jess-coder
   ```

6. **Check:** `jess-coder selftest` must end with `selftest: PASS`.

## Using it

```bash
jess-coder fix-grocery
```

That opens session `fix-grocery`: a fresh box on `main` as deployed. Qwen Code starts in **auto-approve** mode, which is safe because the box is the boundary. Inside the box:

- Talk to it as usual; it can run the repo's tests: `python -m pytest -o asyncio_mode=auto -p no:cacheprovider orchestrator/tests/<file> -q`.
- Leave with `/quit` or Ctrl-C twice.

| Command | What it does |
|---|---|
| `jess-coder <name>` | Open or resume a session. The workspace persists in `/srv/jess-coder/sessions/<name>` |
| `jess-coder <name> --continue` | Also resume the last conversation |
| `jess-coder <name> --ask` | Confirm each action instead of auto-approve |
| `jess-coder <name> --task "…"` | Run one instruction headless (no UI), then exit. The changes stay in the workspace |
| `jess-coder patch <name>` | Export the changes as a checked patch (prints a diffstat and the apply commands) |
| `jess-coder patch <name> --show` | The same, plus the whole diff (control characters made visible) |
| `jess-coder ls` | Sessions, open boxes, live tokens |
| `jess-coder rm <name>` | Delete a session (its saved patches stay) |
| `jess-coder selftest` | Run the isolation self-test (writes the metric) |
| `jess-coder build [--if-stale]` | Build the box image |

**Applying a patch.** `jess-coder patch` prints the exact commands. They:
- create a worktree on the session's base commit,
- run `git apply --index` there,
- leave you to review, test and open a PR as usual.

The change then goes through the normal pipeline: CLAUDE.md's review agents and CI.

**Sessions start from `main` as deployed** (the live checkout's `main`), not from your feature branch. A session keeps its base commit until you `rm` it.

**The brain asleep?** `jess-coder` checks the brain's `/health` first and wakes Helios (through the orchestrator, when `HELIOS_WAKE_ENABLED`) and waits up to 6 min. A session ends itself at its token's expiry (4 h by default), even if `jess-coder` is gone.

## Self-test

`coder/selftest.py` runs **inside** a box on coder-net, at the start of every session (before Qwen Code starts) and on every deploy while `CODER_ENABLED=true`. If it fails, the box doesn't open and the deploy fails. It checks:

- **Identity:** the box uid, no capabilities, `no_new_privs`, seccomp on.
- **Filesystem:** root mount read-only; writable mounts only `/work`, `/tmp`, `/home/coder`, `/app/data`; no Docker socket.
- **Network:** only `lo` + `eth0`; an address in `CODER_BOX_RANGE`; no default route; no routable IPv6.
- **Reach:**
  - external DNS doesn't resolve (a lookup that never finishes counts as a failure);
  - every Jupiter address (each interface, each Docker bridge gateway, given by the launcher), Helios, Saturn, the router, public resolvers, public IPv6 and the bridge's own IPv6 link-local must answer a TCP connect with an immediate **"no route"**. That proves no port is reachable, not just the 18 probed: a refusal (something answered) or a timeout (packets left) counts as reach. The gate host may refuse its other ports; the bridge's `.1` may only be absent;
  - UDP DNS gets no answer.
- **IPv6:** off on `eth0`, no address but `::1`.
- **Gate:** 403 on other paths (`/health`, `/v1/chat/completions`, a `..` path: 3 guard hits per run), 401 without a valid token, 200 with the session's token.

It writes `jess_coder_selftest_ok` + `jess_coder_selftest_last_run_timestamp_seconds` (node_exporter textfile) on every run, also when `jess-coder` refuses coder-net before running it. A failure in the last 7 days fires `CoderSelfTestFailing`.

**When it fails:** run `jess-coder selftest` and read the FAIL lines.
- `gate is off` → step 3 of Setup.
- A TCP or DNS hit → coder-net isn't isolated any more. Check `docker network inspect gateway_nerves_coder-net` for `gateway_mode_ipv4: isolated`, `Internal: true` and its members. If it was recreated by hand without those options, compose won't fix it in place (a same-name network blocks it). Close every box, then:

  ```bash
  docker network disconnect -f gateway_nerves_coder-net brain-orchestrator && docker network rm gateway_nerves_coder-net && cd ~/gateway_nerves && docker compose up -d --force-recreate orchestrator
  ```

- `IPv6 disabled on eth0` / `no IPv6 address but ::1` → Docker started giving endpoints IPv6 (a Docker upgrade?). Don't open boxes; add the host backstop (Threat model → Known limits).

Negative controls: the same container on Docker's default bridge fails 6 checks (internet, LAN, Helios, DNS, host bridge gateways), and one with IPv6 forced on in its endpoint fails the IPv6 checks, so the test does notice a box with network access.

## Monitoring

**Grafana:** row "Coder Box (jess-coder)" on the Brain Gateway dashboard:
- gate requests by result
- wait-for-chat and brain time
- preemptions, guard blocks and filter refusals
- brain tokens
- the self-test

**Alerts** (all quiet Pushover):

| Alert | Fires when |
|---|---|
| `CoderSelfTestFailing` | The last self-test failed |
| `CoderGuardBlocked` | More than 15 requests from coder-net to other paths in 10 min. A box is probing the API (each self-test makes 3) |
| `CoderUpstreamErrors` | 3+ gate requests failed at the brain or inside the gate in an hour (inhibited while a brain alert explains it) |

Metric details: `monitoring/README.md` → Coder Gate.

## Context window

`CODER_CONTEXT_WINDOW` (`.env`, default 131072) is baked into the image's Qwen Code settings. Qwen Code compacts its conversation before that many tokens.
- **Never set it above the brain's real context** (`-c` in `tts/llama-server-primary.service`, 262144 now), or Qwen Code overflows instead of compacting.
- **If the brain is rolled back to 64K,** set 65536 and `jess-coder build`.
- **Why 128K and not 262K:** long coder prompts take the brain longer to prefill and press harder on the prompt cache Jess's own turns rely on.

## Upgrading Qwen Code

1. Edit the version in `coder/package.json`, then regenerate the lock with Node 22 (on the host: `PATH=~/.nvm/versions/node/v22.23.4/bin:$PATH`):

   ```bash
   cd coder && npm install --package-lock-only --ignore-scripts --no-audit --no-fund
   ```

2. Rebuild, then run `jess-coder selftest` and a short `--task` (e.g. "reply READY") **and** an interactive session. Interactive mode sends more tools than headless (0.25.3: `ask_user_question`, plan mode, `propose_goal`, `artifact`, `display_image`), and the gate refuses a whole request if one tool's schema is out of bounds. 0.25.3's `propose_goal` was (`maxLength` 1500).
3. If a session shows `request refused by the coder gate (<reason>)`, find the tool and add it to `EXCLUDE_TOOLS` in `coder/entrypoint.py`, or loosen the gate deliberately in `coder_gate.py` with a security review.
4. Check that the background features are still off. New releases add settings; the spike (`internal/CODER_SPIKE.md` §2) shows how to measure request size and extra calls.

Node: bump `NODE_VERSION` + `NODE_SHA256` in `coder/Dockerfile` from the release's `SHASUMS256.txt`.

## Testing against a lab gate

`jess-coder` reads these overrides, so a throwaway gate on its own isolated network can be tested without turning on the live one:

| Variable | Override |
|---|---|
| `JESS_GATEWAY_ROOT` | Checkout + `.env` to read |
| `JESS_CODER_HOME` | Instead of `/srv/jess-coder`; add `JESS_CODER_ALLOW_UNMOUNTED=1` for a plain folder |
| `JESS_CODER_NETWORK` | The box network |
| `JESS_CODER_IMAGE` | An image built with `docker build --build-context orchestrator=orchestrator/ --build-arg GATE_URL=http://<lab gate>:8888/api/coder/v1 coder/` (the input-hash check is skipped for an override) |
| `JESS_CODER_GATE_CONTAINER` | The only non-box member allowed on the network |
| `JESS_CODER_METRICS_DIR` | Where the self-test metric goes |

The PR 2 lab used:
- the real `routes_coder` router plus the guard, in a container from the orchestrator image;
- the real brain;
- a planted one-character bug.

Qwen Code found it, ran the test file inside the box (7 failed → 44 passed) and the patch applied cleanly, in about 21 s. **A lab gate doesn't see Jess's live chat**, so keep lab runs short.

## Files

| File | What |
|---|---|
| `coder/Dockerfile` | The box image: the orchestrator's dependency lines (shared build cache), none of its code; `requirements.txt` + `embedding_pin.py` come in through the compose build context `orchestrator` |
| `coder/package.json`, `package-lock.json` | Pinned Qwen Code |
| `coder/settings.json` | Locked Qwen Code settings (`/etc/qwen-code/settings.json`, system scope) |
| `coder/system-prompt.md` | The lean system prompt (the spike measured ~4.4K tokens per request with it) |
| `coder/entrypoint.py` | In-box modes: `selftest`, `session`, `patch`, `clean` |
| `coder/selftest.py` | The isolation self-test |
| `scripts/jess-coder` | Host launcher (stdlib Python): `box_command()` is the only `docker run`; `check_box_diff()` the patch check |
| `orchestrator/tests/test_coder_box.py` | Static guards + launcher logic |
