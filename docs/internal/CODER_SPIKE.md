# Coder box spike (2026-11-20)

PR 0 of [`CODER_PLAN.md`](CODER_PLAN.md). The spike settles the plan's open questions before anything is built.

All spike code was throwaway: two test networks, a test gate and test containers. It was removed afterwards and nothing here is deployed. The test gate logged request shapes and token counts only, never content. The brain tests used made-up prompts, not Jess's.

## 1. Network isolation

**Setup.** Two throwaway internal bridge networks on Jupiter (Docker 29.1.3):
- one plain;
- one with `com.docker.network.bridge.gateway_mode_ipv4=isolated`.

A probe container from the orchestrator image (`--cap-drop ALL`, `no-new-privileges`) tried TCP connects, DNS and IPv6 from each.

| From the container | Plain `internal: true` | `internal: true` + `isolated` |
|---|---|---|
| Host address on the bridge | yes (`.1`) | **none** |
| Jupiter via the bridge address: SSH 22, DNS 53, SMB 445, HA 8123, node-exporter 9100, Netdata 19999 | **open** | refused |
| Jupiter LAN IP, Helios :8080, router, other bridges' gateways | network unreachable | network unreachable |
| Internet (1.1.1.1:443, 8.8.8.8:53), external DNS, IPv6 | no | no |
| Another container on the same network, by IP and by name | — | **works** |

**Decisions:**
- `coder-net` uses `gateway_mode_ipv4: isolated`. The host `INPUT` drop rule stays as a backstop.
- Subnet `10.213.250.0/24`. That is outside Docker's default pools; `172.31.0.0/16` is already taken by `grist-net`. Docker refused an overlapping pool.
- The orchestrator still reaches the box (and back) container-to-container.

### Finding outside this project (not changed here)

A plain internal network gives its bridge a host address. Any container on one can then reach every Jupiter service that listens on all interfaces. That includes Pi-hole DNS, which forwards to the internet, so "internal" does not mean "offline" there.

The only internal network on Jupiter today is `searxng-probe-net`. It has 192.168.16.1 on the host, and its members are searxng and blackbox-exporter. The other probe networks (`frontend-probe-net`, `grist-probe-net`) are ordinary NAT bridges.

Not tested here:
- NAT bridges, which very likely reach the same host services through their gateway;
- which containers that handle outside content (searxng, paperless, immich) can reach which services.

The likely fix is `isolated` gateway mode on the probe networks, plus host `INPUT` rules for traffic from Docker bridges. It is a separate task.

## 2. Qwen Code 0.25.3 through a gate

**The test gate:**
- FastAPI in the orchestrator image.
- Streams from llama-server and buffers the reply, then hands it to Qwen Code as SSE in one burst.
- Strips `timings` and `usage.prompt_tokens_details`.
- Forces the model name, `max_tokens` ≤ 4096, `temperature` ≤ 1.0 and `repeat_penalty` 1.0.

**Qwen Code:**
- Run from the host's Node 22 install, mounted read-only into the container.
- First runs were file-edit-only on the host. After that, yolo inside a sandbox container on the isolated network: uid 1001, `--read-only`, tmpfs `/tmp` and HOME, `--cap-drop ALL`, `--log-driver none`.

### What works
- **Buffered replies:** Qwen Code is fine getting the whole reply at once.
- **Request keys it sends:** `max_tokens, messages, model, stream, stream_options, temperature, tools`. All other keys can be dropped.
- **429 + `Retry-After`:** it waits exactly as told (3.0 s gaps measured).
  - Plain mode gives up after `maxRetries` (3 in our settings).
  - `QWEN_CODE_UNATTENDED_RETRY=1` keeps retrying, with backoff capped at 5 min.

### Settings (system scope)
- **Locked config goes in `/etc/qwen-code/settings.json`**, mounted read-only. That scope overrides every other one, including a `.qwen/settings.json` inside the repo being worked on.
- **HOME must be writable.** Mounting the settings file into `~/.qwen` makes Docker create that folder as root, and Qwen Code then dies with `EACCES` on startup.
- **Four defaults have to be turned off.** Each makes its own calls on the shared brain slot:

  | Setting | Default | What it does |
  |---|---|---|
  | `memory.enableManagedAutoMemory` | true | After **every** answer, a background agent with write tools makes a second ~7K-token call |
  | `memory.enableManagedAutoDream` | true | Memory consolidation agent |
  | `memory.enableAutoSkill` | true | Skill review after tool-heavy sessions |
  | `ui.enableFollowupSuggestions` | true | "Next prompt" predictions |

  `model.skipNextSpeakerCheck` is already true by default.
- **Needed env:**
  - `QWEN_CODE_SUPPRESS_YOLO_WARNING=1`
  - no proxy settings
  - nothing from Qwen Code's own `tools.executionSandbox` (bwrap/landlock): the container is the sandbox
- **Context files:** keep `skipStartupContext` false. It only costs about 560 chars.

### Tools and request size

| Configuration | Tools | Request chars (≈ tokens) | Extra call after each answer |
|---|---|---|---|
| Headless `auto-edit` (no shell) | 9 | 58.7K | yes |
| Yolo, defaults | 10 | 67.2K (~15K) | yes, ~27K chars |
| Yolo, excluding `tool_search`/`skill` only | **23** | 78.3K | yes |
| Yolo, exact exclude list (below) | 6 | 50.2K | yes |
| … plus lean `--system-prompt` | 6 | 34.6K | yes |
| … plus the four settings above off | 6 | **17.6K (~4.4K)** | **no** |

- **Excluding `tool_search` alone backfires.** Qwen Code then loads all its deferred tools up front (cron, worktree, goals, monitor, messaging…), so the exclude list has to name them.
- **The exact list** keeps `read_file, write_file, edit, glob, grep_search, run_shell_command`:

  ```text
  notebook_edit,skill,tool_search,tool_call,cron_create,cron_delete,cron_list,enter_worktree,
  exit_worktree,get_goal,update_goal,list_agents,loop_wakeup,monitor,read_mcp_resource,
  record_artifact,report_findings,send_message,task_stop,zoom_image,web_fetch,web_search,
  todo_write,save_memory,agent,task
  ```

- **The six tool schemas are 14.9K chars.** `run_shell_command` alone is 5.5K. With the lean setup they are most of what remains, and they can't be trimmed without patching Qwen Code.
- **`--system-prompt` replaces only Qwen Code's main prompt.** The memory and skills instructions it adds stay until those features are turned off. The system message then drops to 1.7K chars.

### Quality check: planted bug, yolo, inside the box

The task was a two-file Python project where `is_weekend` used `weekday() > 5`. Prompt: "The tests in this folder fail. Find and fix the bug in the code (do not change the tests). Run python -m unittest to check, then summarise what you changed."

| System prompt | Result | Requests | Time | Prompt tokens per request |
|---|---|---|---|---|
| Lean (below) | fixed (`>= 5`), tests untouched, OK | 8 | 15 s | 4.4K → 6.4K |
| Qwen Code's own | fixed, tests untouched, OK | 7 | 17 s | 7.5K → 9.9K |

The prompt cache also carries over between the coder's own turns (`cache_n` ≈ the previous prompt). One easy task proves little about quality, so repeat the comparison on a real repo task in PR 2.

The lean prompt as tested:

```text
You are a coding agent working in a sandboxed, offline Linux container. The current directory is your workspace.
Tools: read_file, write_file, edit, glob, grep_search, run_shell_command. Use them to inspect and change files and to run commands (python, pytest, git).
Rules:
- There is no internet and no network: do not try pip/npm install, curl, or network calls. Use only what is installed.
- Work step by step: look before you change, keep changes minimal, run the relevant tests after a change.
- Never run git commit, git push, or anything that deploys. Leave changes in the working tree.
- When finished, reply with a short summary: what you changed (files), what you ran, and the result.
```

## 3. Sharing the brain slot

Measured directly against llama-server on Helios with made-up prompts. `--cache-ram` was at its default of 8192 MiB.

| Step | Prompt tokens | Read from cache | Time |
|---|---|---|---|
| Prompt A (stands in for Jess), first time | 26,612 | 0 | 11.2 s |
| A again | 4 new | 26,608 | 0.07 s |
| Prompt B (coder-sized, different) | 39,762 | 0 | 17.1 s |
| **A after B** | 4 new | **26,608** | **0.34 s** |

- **Jess's cached prompt survives a coder request.** `--cache-ram` restores it from host RAM. No re-warming is needed after a coder burst.
- **Prefill runs at about 2,400 tokens/s.** A cold 40K-token coder prompt therefore holds the slot for about 17 s, and a chat turn arriving then would wait that long. So preemption is needed.
- **Cancel on disconnect works.** We cut a streaming request after 2 s; the next small request finished 0.21 s later.
- **Not tested:** several big coder prompts in a row. `--cache-ram` holds only a limited number of prompt states, so a long burst could still push Jess's out. Watch `prompt_tokens_cached_total` once the gate is live.

## 4. Changes to the plan

1. **`coder-net`:** `gateway_mode_ipv4: isolated`, subnet `10.213.250.0/24`, a fixed bridge name, and the orchestrator at a fixed address.
2. **Box settings:**
   - read-only `/etc/qwen-code/settings.json`, with the four background features off
   - HOME on a tmpfs owned by the box uid
   - `contextWindowSize` equal to the brain's real `-c` (65536 now; PR #172 raises it at its cutover)
3. **Box run flags:**
   - the exact `--exclude-tools` list
   - the lean `--system-prompt`, plus the repo's compact `QWEN.md` (PR 0.5)
   - `QWEN_CODE_SUPPRESS_YOLO_WARNING=1`
   - `QWEN_CODE_UNATTENDED_RETRY=1` for jobs; interactive sessions keep `maxRetries`
4. **The gate holds instead of refusing.** It holds a request while chat is busy, for up to ~90 s, and only then answers 429 + `Retry-After`. That way an interactive session never runs out of retries during a long chat. To preempt, it aborts upstream (the slot is free in ~0.2 s) and re-sends once chat is idle.
5. **No re-warm step** after coder bursts.
6. **Dropped from the spike:** checking prompt order via `/apply-template`. The box controls its whole conversation, so the order has no security value.

## 5. Your host `qwen`

`~/.qwen/settings.json` on Jupiter still has the four defaults on. So every answer from your host `qwen` also starts a background memory agent on the shared brain slot: about 7K more tokens, writing memory files under `~/.qwen`. Turning them off is a four-line settings change:

```json
"memory": {"enableManagedAutoMemory": false, "enableManagedAutoDream": false, "enableAutoSkill": false},
"ui": {"enableFollowupSuggestions": false}
```
