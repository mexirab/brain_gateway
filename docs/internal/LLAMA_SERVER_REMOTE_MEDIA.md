# llama-server fetches image URLs: brain-side options

Status (2026-10-27): **Option B (`--api-key-file`) applied in the repo, not yet enforced on Helios.** The live and 27B units, the orchestrator and monitoring are done (see "Applied" under Option B); the key is enforced once `tts/llama-server-primary.service` is installed on Helios, after that change deploys (the orchestrator must send the key first; steps in the unit header). **Still open, owner's call:** Option A (narrow `IPAddressAllow`) and the Loki detection alert.

Written 2026-10-25 after the security review of PR #148. The orchestrator side is fixed in `cloud_brain._process_vision` / `_flatten_content` (no image part, link or otherwise, ever reaches a model loop). This page covers what that fix cannot reach: llama-server itself.

## The finding

`llama-server-primary.service` on Helios (`tts/llama-server-primary.service`, llama.cpp checkout `/home/labadmin/llama.cpp-mtp`, commit `6805ae35d`, build 11358) listens on `0.0.0.0:8080` with no `--api-key`.

For every OpenAI content part of type `image_url` (and `input_audio` / `input_video` / `video_url` when the mmproj supports them), `handle_media()` in `tools/server/server-common.cpp` does this:

- a URL starting with `http` is downloaded: `common_remote_get_content`, cpp-httplib (the build has `LLAMA_CURL=OFF`; `LLAMA_OPENSSL=ON` was requested but CMake found no OpenSSL — `OPENSSL_SSL_LIBRARY-NOTFOUND`, no ssl/crypto libs linked — so only plain `http://` URLs are fetched; an `https://` URL fails with "HTTPS is not supported", confirmed live 2026-10-27), 10 MB / 10 s hard-coded (a `TODO` says "maybe make these params configurable"). `common_http_client` sets `set_follow_location(true)`, so redirects are followed, and `user:pass@` in the URL becomes basic auth.
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
IPAddressAllow=localhost 10.0.0.248/32
```

`localhost` is systemd's name for `127.0.0.0/8` plus `::1`. A process on Helios that calls `10.0.0.195:8080` (Helios's own LAN address) arrives from `10.0.0.195`, not loopback, and would be refused: add `10.0.0.195/32` only if something needs that.

This does two things at once:

- **Who can call the brain:** only Jupiter (the orchestrator; Prometheus' `llama-primary` scrape and the blackbox probe, which leave Jupiter with its LAN address; Qwen Code on Jupiter) and Helios itself (the unit's own `ExecStartPost` readiness check uses `127.0.0.1`).
- **Where a fetch can go:** Helios loopback and Jupiter's LAN-bound ports only, instead of the whole LAN, tailnet and Docker ranges.

Before applying, check nothing else calls the brain:

1. On Helios, sample who is connected over a normal day. A 12-second sample on 2026-10-25 showed only `10.0.0.248`:

   ```bash
   ss -Htn state established '( sport = :8080 )' | awk '{print $4}' | sed 's/:[0-9]*$//' | sort | uniq -c
   ```

2. Think of callers that only run sometimes: experiments from Saturn, the owner's laptop over Tailscale, anything using `helios.tail74fc4a.ts.net:8080` (that arrives from a `100.x` tailnet address, not `10.0.0.248`). Add each one's address as a `/32`, or accept that it stops working.

Optionally check the mechanism first without touching the brain. A throwaway unit with the same two lines should fail to reach the router and succeed to reach Jupiter:

```bash
sudo systemd-run --wait --pipe -p IPAddressDeny=any -p IPAddressAllow='localhost 10.0.0.248/32' curl -s -m3 -o /dev/null -w '%{http_code}\n' http://10.0.0.1/
sudo systemd-run --wait --pipe -p IPAddressDeny=any -p IPAddressAllow='localhost 10.0.0.248/32' curl -s -m3 -o /dev/null -w '%{http_code}\n' http://10.0.0.248:8888/health
```

The first should print `000` (no connection), the second `200`.

Apply (on Helios):

1. Edit the line in the repo copy `tts/llama-server-primary.service`, then copy it to `/etc/systemd/system/llama-server-primary.service`.
2. `sudo systemctl daemon-reload && sudo systemctl restart llama-server-primary` (the brain reloads in about 6–9 s warm; chat is down for that long).
3. Verify from Jupiter: `curl -s http://10.0.0.195:8080/health` returns `{"status":"ok"}`.
4. Verify from another LAN host (Saturn): the same `curl` now times out.
5. Check Grafana's "Brain Server (llama-server)" row still gets data (the Prometheus scrape comes from Jupiter).

Rollback: put the old line back, `daemon-reload`, restart.

What it does not cover: any process or container on Jupiter can still reach `:8080` and make Helios fetch a URL on Helios loopback or Jupiter. The orchestrator no longer forwards image URLs (this PR), so what remains is other software on Jupiter. One worth naming: Qwen Code on Jupiter is an LLM agent that talks to `:8080` directly. If it ever sends an `image_url` part with a link, Helios still fetches it; option A limits where that fetch can land.

## Option B: `--api-key` (defence in depth; more work)

**Applied 2026-10-27** (egress-audit hardening), differently from the plan below in three places:

- Key file `/etc/llama-server/api-key` (`root:llama` 640, dir 750), one key. Both llama-server units carry `--api-key-file` + an `ExecStartPre` that refuses a file with no key line (llama-server skips blank/`#` lines and would run with no auth); readiness checks poll `/health`. `voxcpm-tts` (same `llama` user) has `InaccessiblePaths=-/etc/llama-server`.
- Jupiter: instead of ~15 call sites, one origin-scoped `httpx.Auth` (`orchestrator/model_auth.py`) on the shared `_http` client and the service-registry probe clients. Every model call goes through `_http` or the chat backends (`call_model`, the one-off backend, vision, probes, `/models` checks), so `MODEL_API_KEY` reaches the model origin and nothing else; keyed 401/403 → `bgw_model_auth_rejected_total` + alert `BrainAuthRejected`.
- Monitoring: scrape `credentials_file` written by `generate-configs.sh` (`monitoring/prometheus/llama_api_key`), blackbox probe on `/health`, alert `BrainMetricsScrapeRejected`. Qwen Code on Jupiter: its `qwen` wrapper reads the key from a 600 file in `~/.qwen/`.

The original plan, kept for reference:

With a key, every endpoint except `/health` and `/v1/health` returns 401 without `Authorization: Bearer <key>` (or `X-Api-Key`). In this build that includes `/v1/models`, `/metrics`, `/apply-template`, `/tokenize` and `/props`.

Helios side:

1. Put the key in a file the sandboxed unit can read. `ProtectHome=tmpfs` hides `/home`, so not there. For example `/etc/llama-server/api-keys`, owned `root:llama`, mode `0640`, one key per line.
2. Add `--api-key-file /etc/llama-server/api-keys` to `ExecStart`. Use the file, not `--api-key KEY`: argv shows up in `ps` for every user on Helios.
3. Change the unit's `ExecStartPost` readiness check from `/v1/models` to `/health`. In this build `/v1/models` is no longer public with a key, so the unit would never become ready and systemd would kill it after 240 s. `/health` returns 503 until the model is loaded, so it still works as a readiness check. (Older checkouts differ: the Jan 2026 tree at `/home/labadmin/src/llama.cpp` leaves `/models` and `/v1/models` public. Check the build you deploy.)
4. Write the key file without a trailing space or CRLF: `--api-key-file` does not trim lines, so they become part of the key.

Jupiter side, everything that talks to `MODEL_URL` / `VISION_MODEL_URL` must send the key. `MODEL_API_KEY` and `FALLBACK_MODEL_API_KEY` already exist, but only the loop backends read them. The callers:

- through `llm_backend.OpenAICompatibleBackend` (sends `Authorization: Bearer` when `api_key` is set): the unified loop and the fallback loop. `shared.py` already passes `MODEL_API_KEY` / `FALLBACK_MODEL_API_KEY`. The one-off backend `orchestrator._resolve_backend` builds when a URL matches neither does not.
- bare httpx calls with no key plumbing today: `auto_learn.py`, `jobs_calendar.py` (`_LLM_URL`), `vision_handler.py` (`analyze_image` and `check_vision_health`; meal photos, pantry and receipt scans, document photos and Telegram photos all go through `analyze_image`), `meal_planner.py`, `jobs_self_audit.py`, `self_investigator.py`, `session_miner.py`, `task_decomposition.py`.
- health probes on `/models`: `model_manager.py` (`_models_probe_url`), `system_diagnostics.py`, `service_registry.py`, `meal_planner.py`, `self_investigator.py`, and `api_routes.py`'s two probes of `{FALLBACK_MODEL_URL}/models` (with a key and no header, `FALLBACK_ONLINE` would always read 0). Switch these to the key or to `/health`.
- monitoring: the Prometheus `llama-primary` job scrapes `/metrics` (add `authorization: {type: Bearer, credentials_file: …}`), and the blackbox probe of `http://<helios>:8080/v1/models` in `prometheus.yml.template` would see 401 (probe `/health` instead).
- Qwen Code on Jupiter: set its OpenAI API key to the same key.

Rollback: remove the flag and restore the old readiness URL; the orchestrator keeps working with or without the key sent.

What it does not cover: a caller that has the key (the orchestrator, Qwen Code on Jupiter) can still make Helios fetch a URL. That is the hole PR #148 closes on the orchestrator side. The key crosses the LAN in plain HTTP, so anyone who can read LAN traffic can take it. And the unit's `IPAddressAllow` still spans LAN/tailnet/Docker, so a fetch can still land anywhere there; DNS via the LAN resolver is the one internet-bound path it leaves. Option A narrows both.

## Detection (cheap, whichever option)

llama-server logs every fetch at INFO: `downloading image from '<url>'` (`server-common.cpp:1094`). Those lines already reach Loki through promtail-helios (`llama-server-primary` journal). A Loki ruler alert on that string for the brain unit (`monitoring/loki/rules/fake/`, next to `BrainFailedToStart`) would catch any remaining SSRF use from any caller, since no legitimate caller sends links any more. Note the flip side: fetched URLs end up in Loki (30 days).

## Recommendation

1. **Now:** Option A. One line, closes the LAN/tailnet exposure and shrinks where a fetch can land.
2. **With it:** the Loki detection alert above. One rule, no Helios change.
3. ~~**Later, if wanted:** Option B for defence in depth.~~ Done in the repo 2026-10-27; enforced once the Helios unit is installed.
4. **Skip** option C unless upstream adds a flag.

Do not test any of this by sending chat traffic to the live brain. `GET /health` and a TCP connect are enough to check who can reach it.
