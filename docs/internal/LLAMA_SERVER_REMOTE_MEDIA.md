# llama-server fetches image URLs: brain-side options

Status: **recommendation only, nothing applied on Helios.** Changing the Helios unit is the owner's call.

Written 2026-10-25 after the security review of PR #148. The orchestrator side is fixed in `cloud_brain._process_vision` / `_flatten_content` (no image part, link or otherwise, ever reaches a model loop). This page covers what that fix cannot reach: llama-server itself.

## The finding

`llama-server-primary.service` on Helios (`tts/llama-server-primary.service`, llama.cpp checkout `/home/labadmin/llama.cpp-mtp`, commit `6805ae35d`, build 11358) listens on `0.0.0.0:8080` with no `--api-key`.

For every OpenAI content part of type `image_url` (and `input_audio` / `input_video` / `video_url` when the mmproj supports them), `handle_media()` in `tools/server/server-common.cpp` does this:

- a URL starting with `http` is downloaded: `common_remote_get_content`, cpp-httplib with OpenSSL (the build has `LLAMA_CURL=OFF`, `LLAMA_OPENSSL=ON`), 10 MB / 10 s hard-coded (a `TODO` says "maybe make these params configurable"). `common_http_client` sets `set_follow_location(true)`, so redirects are followed, and `user:pass@` in the URL becomes basic auth.
- `file://` is refused unless `--media-path` is set (it is not; keep it that way).
- `data:` and raw base64 are decoded locally.

The download happens while the request is being parsed, so `POST /apply-template` (no generation, no slot) is enough to make Helios fetch a URL. The reviewer confirmed it with a harmless loopback URL. That makes llama-server a blind GET SSRF proxy for anyone who can reach `:8080`. Today that is the whole LAN, the tailnet and Docker networks: `IPAddressAllow=127.0.0.0/8 10.0.0.0/24 100.64.0.0/10 172.16.0.0/12`.

The same `IPAddressAllow` line also limits where the fetch can go. systemd applies `IPAddressAllow`/`IPAddressDeny` to packets the unit sends as well as packets it receives. So today a fetch can reach Helios loopback services, any LAN host (router UI, Saturn, Jupiter's LAN-bound ports), any tailnet device and Docker networks.

## Option C: turn remote media off in llama.cpp (not available)

This build has no flag for it. `--media-path` governs `file://` only. The `http` branch in `handle_media` is unconditional. The only ways to get rid of it are:

- carry a local patch (make the `http` branch throw) and rebuild. That is a fork to re-apply on every llama.cpp update.
- wait for upstream to make it configurable, then add the flag.

Not recommended now. Options A and B cover it without a fork.

## Option A: narrow `IPAddressAllow` (recommended first; one line)

Allow only Helios loopback and Jupiter:

```
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8 10.0.0.248/32
```

This does two things at once:

- **Who can call the brain:** only Jupiter (the orchestrator; Prometheus' `llama-primary` scrape and the blackbox probe, which leave Jupiter with its LAN address; Qwen Code on Jupiter) and Helios itself (the unit's own `ExecStartPost` readiness check uses `127.0.0.1`).
- **Where a fetch can go:** Helios loopback and Jupiter's LAN-bound ports only, instead of the whole LAN, tailnet and Docker ranges.

Before applying, check nothing else calls the brain:

1. On Helios, sample who is connected over a normal day. A 12-second sample on 2026-10-25 showed only `10.0.0.248`:

   ```bash
   ss -Htn state established '( sport = :8080 )' | awk '{print $4}' | sed 's/:[0-9]*$//' | sort | uniq -c
   ```

2. Think of callers that only run sometimes: experiments from Saturn, the owner's laptop over Tailscale, anything using `helios.tail74fc4a.ts.net:8080` (that arrives from a `100.x` tailnet address, not `10.0.0.248`). Add each one's address as a `/32`, or accept that it stops working.

Apply (on Helios):

1. Edit the line in the repo copy `tts/llama-server-primary.service`, then copy it to `/etc/systemd/system/llama-server-primary.service`.
2. `sudo systemctl daemon-reload && sudo systemctl restart llama-server-primary` (the brain reloads in about 6–9 s warm; chat is down for that long).
3. Verify from Jupiter: `curl -s http://10.0.0.195:8080/health` returns `{"status":"ok"}`.
4. Verify from another LAN host (Saturn): the same `curl` now times out.
5. Check Grafana's "Brain Server (llama-server)" row still gets data (the Prometheus scrape comes from Jupiter).

Rollback: put the old line back, `daemon-reload`, restart.

What it does not cover: any process or container on Jupiter can still reach `:8080` and make Helios fetch a URL on Helios loopback or Jupiter. The orchestrator no longer forwards image URLs (this PR), so what remains is other software on Jupiter.

## Option B: `--api-key` (defence in depth; more work)

With a key, every endpoint except `/health` and `/v1/health` returns 401 without `Authorization: Bearer <key>` (or `X-Api-Key`). In this build that includes `/v1/models`, `/metrics`, `/apply-template`, `/tokenize` and `/props`.

Helios side:

1. Put the key in a file the sandboxed unit can read. `ProtectHome=tmpfs` hides `/home`, so not there. For example `/etc/llama-server/api-keys`, owned `root:llama`, mode `0640`, one key per line.
2. Add `--api-key-file /etc/llama-server/api-keys` to `ExecStart`. Use the file, not `--api-key KEY`: argv shows up in `ps` for every user on Helios.
3. Change the unit's `ExecStartPost` readiness check from `/v1/models` to `/health`. `/v1/models` is no longer public with a key, so the unit would never become ready and systemd would kill it after 240 s.

Jupiter side, everything that talks to `MODEL_URL` / `VISION_MODEL_URL` must send the key. `MODEL_API_KEY` and `FALLBACK_MODEL_API_KEY` already exist, but only the loop backends read them. The callers:

- through `llm_backend.OpenAICompatibleBackend` (sends `Authorization: Bearer` when `api_key` is set): the unified loop and the fallback loop. `shared.py` already passes `MODEL_API_KEY` / `FALLBACK_MODEL_API_KEY`. The one-off backend `orchestrator._resolve_backend` builds when a URL matches neither does not.
- bare httpx calls with no key plumbing today: `auto_learn.py`, `jobs_calendar.py` (`_LLM_URL`), `vision_handler.py` (`analyze_image` and `check_vision_health`), `meal_manager.py`, `meal_planner.py`, `jobs_self_audit.py`, `self_investigator.py`, `session_miner.py`, `task_decomposition.py`.
- health probes on `/models`: `model_manager.py` (`_models_probe_url`), `system_diagnostics.py`, `service_registry.py`, `meal_planner.py`, `self_investigator.py`. Switch these to the key or to `/health`.
- monitoring: the Prometheus `llama-primary` job scrapes `/metrics` (add `authorization: {type: Bearer, credentials_file: …}`), and the blackbox probe of `http://<helios>:8080/v1/models` in `prometheus.yml.template` would see 401 (probe `/health` instead).
- Qwen Code on Jupiter: set its OpenAI API key to the same key.

Rollback: remove the flag and restore the old readiness URL; the orchestrator keeps working with or without the key sent.

What it does not cover: a caller that has the key (the orchestrator) can still make Helios fetch a URL. That is the hole this PR closes on the orchestrator side.

## Recommendation

1. **Now:** Option A. One line, closes the LAN/tailnet exposure and shrinks where a fetch can land.
2. **Later, if wanted:** Option B for defence in depth. It is a code change across ~15 call sites plus monitoring, so it should be its own PR.
3. **Skip** option C unless upstream adds a flag.

Do not test any of this by sending chat traffic to the live brain. `GET /health` and a TCP connect are enough to check who can reach it.
