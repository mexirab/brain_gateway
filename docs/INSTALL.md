# Install guide

End-to-end install for a single-box Brain Gateway deployment.

**The fast path** (recommended for almost everyone): `bash install.sh`. The installer handles Docker, the NVIDIA driver, the container toolkit, the orchestrator stack, and prints the wizard URL when it's done. See [The fast path](#the-fast-path).

**The manual path:** if you want to know exactly what's being installed, run a custom subset of steps, or you're on a non-standard config (different Linux distro, pre-existing Docker setup, etc.), follow the step-by-step manual install below.

If you just want hardware-by-hardware recommendations, see [`docs/HARDWARE.md`](HARDWARE.md).

---

## Pre-flight checklist

Before you start (either path), confirm:

- [ ] **OS:** Ubuntu 22.04 LTS or 24.04 LTS (see [HARDWARE.md](HARDWARE.md) for non-Ubuntu notes)
- [ ] **GPU:** at least 20 GiB VRAM (24 GB card class). Below that, manual model selection — see [HARDWARE.md](HARDWARE.md).
- [ ] **Driver:** none yet, or `nvidia-driver-580-open` or newer
- [ ] **Disk:** ~120 GiB free on the partition that holds `/var/lib/docker` and the repo
- [ ] **RAM:** 16 GiB minimum, 32 GiB if you plan to enable the advanced profile or code agent
- [ ] **Network:** the box can reach the public internet to pull container images and HuggingFace models (one-time)
- [ ] **Sudo:** you can `sudo` on the box
- [ ] **Time:** ~20 minutes for the install itself; the first browser-wizard interaction takes another ~5 minutes

---

## The fast path

```bash
# 1. Clone
git clone https://github.com/mexirab/brain_gateway.git
cd brain_gateway

# 2. Run the installer
bash install.sh
```

The script runs in two stages, separated by one reboot. **You don't have to re-run anything after the reboot** — a bash-profile hook auto-resumes Stage 2 on your next SSH login.

| Stage | What it does |
|-------|--------------|
| **1** | Installs Docker + docker-compose-v2, adds the NVIDIA container toolkit apt repo, installs `nvidia-driver-580-open` + `nvidia-container-toolkit`, configures the runtime, adds you to the `docker` group, installs a bash-profile auto-resume hook, then prompts you to reboot. |
| **2 (auto-resumes on next login)** | Verifies `nvidia-smi` works, smoke-tests Docker+GPU integration, writes a generated `API_TOKEN` to `.env`, runs `scripts/detect_hardware.sh` to append a model recommendation, brings up the stack, waits for the orchestrator to report healthy, removes the auto-resume hook, then hands off to the interactive setup CLI (`scripts/setup.sh`). |
| **Setup CLI** | 7 interactive prompts: Identity → Model → Voice → Push channels → Integrations → Selfcare → Review. Every prompt has a sensible default; Enter to accept. Saves to `.env` + YAML config via the orchestrator's REST API, then marks setup complete (kill switch flips). |

The auto-resume hook is `~/.brain-gateway-resume.sh` plus a single sourcing line appended to `~/.bash_profile`. Both are idempotent. The script file is removed by Stage 2; the sourcing line becomes a harmless no-op.

If you ever need to manually re-run Stage 2 (skipped the auto-resume, killed the script, etc.):

```bash
cd brain_gateway
bash install.sh   # detects the post-reboot marker and continues from Stage 2
```

**To re-run the setup CLI later** (change something after setup is locked):

```bash
# Option A: use the /settings page in the dashboard (recommended)
xdg-open http://<box-ip>:3001/settings

# Option B: re-open the setup CLI by clearing the kill switch first
# (data/app/setup_state.json → set setup_completed: false)
bash scripts/setup.sh
```

**To re-run the installer from scratch** (e.g. after a wipe-test):

```bash
sudo rm -rf /var/lib/brain-gateway-install
rm -f ~/.brain-gateway-resume.sh
bash install.sh
```

---

## The manual path

This is what the installer is doing under the hood. Follow it if you want full control, are debugging a failed install, or are on a non-standard config.

---

## Step 1 — Install the NVIDIA driver

Skip this section if `nvidia-smi` already prints a working table and the driver is 580 or newer.

```bash
# Remove anything older / conflicting (safe no-op if nothing is installed)
sudo apt purge 'nvidia-*' 'libnvidia-*' 2>/dev/null || true
sudo apt autoremove --purge -y

# Install the DKMS variant — rebuilds against your current kernel
sudo apt update
sudo apt install -y nvidia-driver-580-open

# Reboot so the new kernel module loads cleanly
sudo reboot
```

After reboot:

```bash
nvidia-smi   # should print the driver version + your GPU
```

**If you see "Failed to initialize NVML: Driver/library version mismatch":** you have the *prebuilt-module* package version-locked to a kernel that no longer matches the running one. Purge and reinstall the DKMS flavor:

```bash
sudo apt purge 'linux-modules-nvidia-580-open*'
sudo apt install nvidia-driver-580-open
sudo reboot
```

This bit the Uranus boot-test box (kernel 6.8.0-111 vs prebuilt module for 6.8.0-110). It is the single most common install-day NVIDIA failure on reimaged hardware.

---

## Step 2 — Install Docker + NVIDIA container toolkit

Docker:

```bash
# Engine
sudo apt install -y docker.io docker-compose-v2

# Let your user run docker without sudo
sudo usermod -aG docker "$USER"
newgrp docker      # or log out + back in
docker run --rm hello-world   # smoke test
```

NVIDIA container toolkit (lets containers see the GPU):

```bash
# Add the NVIDIA container toolkit repo
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Smoke test — should print nvidia-smi from inside a container
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

If the smoke test fails, fix it here before continuing. None of the model containers will boot if the toolkit isn't wired up.

---

## Step 3 — Clone the repo + minimum .env

```bash
sudo mkdir -p /opt/gateway_mvp
sudo chown "$USER:$USER" /opt/gateway_mvp
git clone https://github.com/mexirab/brain_gateway.git /opt/gateway_mvp
cd /opt/gateway_mvp
cp .env.example .env
```

You need exactly **one** secret in `.env` before first boot — the orchestrator's API token. Generate one:

```bash
python3 -c "import secrets; print('API_TOKEN=' + secrets.token_urlsafe(32))" >> .env
```

Everything else (Home Assistant token, push channel secrets, voice, model) gets entered through the browser wizard in Step 6.

---

## Step 4 — Hardware scan (recommended)

Append a tier + model recommendation to your `.env`:

```bash
bash scripts/detect_hardware.sh           # print the analysis
bash scripts/detect_hardware.sh >> .env   # append KEY=value to .env
```

This writes a block to the bottom of `.env`:

```
# --- recommended by scripts/detect_hardware.sh ---
JESS_VRAM_TIER=32
VLLM_MODEL=Lorbus/Qwen3.6-27B-int4-AutoRound
VLLM_QUANTIZATION=auto_round
VLLM_MAX_MODEL_LEN=153600
VLLM_GPU_MEM_UTIL=0.92
```

If your hardware is below the 20 GiB floor, the recommendation is commented out and you'll need to set `VLLM_MODEL` to a 7–8B AWQ model yourself before continuing. See [HARDWARE.md](HARDWARE.md) for picks.

---

## Step 5 — Bring up the core stack

```bash
docker compose up -d
```

This pulls + starts the **default** profile:
- `brain-orchestrator` — FastAPI app, port 8888
- `brain-frontend` — Next.js dashboard, port 3001
- `open-webui` — chat UI, port 80/443
- `redis`, `chromadb`, `searxng`, `wyoming-whisper`, `wyoming-jessica-tts`

It does **not** start (these are opt-in):
- `vllm-primary`, `qwen-tts`, `parakeet-stt` — model layer, behind the `models` profile (Helios runs these as host systemd units; new installs that want them via compose set `COMPOSE_PROFILES=models`)
- `promtail`, `nebula-sync`, `nut-exporter` — operator tooling, behind `advanced`

First-boot will take 5–15 minutes to pull all the container images. Watch progress:

```bash
docker compose logs -f --tail=20
```

Once `brain-orchestrator` reports `healthy`, you're ready for the wizard.

```bash
curl -s http://localhost:8888/health
# {"ok":true,...}
```

---

## Step 6 — Run the setup wizard

Open the wizard in any LAN browser:

```
http://<your-box-ip>:3001/setup
```

Or, from the box itself:

```bash
xdg-open http://localhost:3001/setup
```

The wizard walks 7 steps:

| Step | What it asks |
|------|--------------|
| **Welcome** | One-screen overview, "let's go" button |
| **Identity** | Your name, the assistant's name, timezone, ADHD mode toggle |
| **Model** | Confirms the recommendation from `detect_hardware.sh` (or lets you override) |
| **Voice** | Pick a TTS voice from the available set |
| **Push** | Optional: ntfy + Pushover configuration with a live "send test" button |
| **Integrations** | Optional: Home Assistant URL + token, Paperless-ngx URL + token (each has a "validate" button) |
| **Selfcare** | Optional: meal / water / med / movement nudge cadence + quiet hours |
| **Review** | Summary of every choice; click "Launch" to write `.env` and mark setup complete |

After Launch:
- The orchestrator picks up the new env via `setup_env.apply_to_environ()` on next read.
- `setup_state.json` is written to `data/app/` with `setup_completed: true` and a timestamp.
- The wizard endpoints become **immutable** — `POST /api/setup/env`, `DELETE /api/setup/env`, and `POST /api/setup/env/validate` all return HTTP 410 from this point on. To change settings later, use the `/settings` page in the dashboard.
- You're redirected to the main dashboard at `http://<your-box-ip>:3001/`.

**Wizard didn't show up?** Confirm `setup_state.json` doesn't already exist with `setup_completed: true`:

```bash
cat data/app/setup_state.json 2>/dev/null
```

If it does, either edit it (set `setup_completed: false` to re-show the wizard) or use the `/settings` page instead.

---

## Step 7 (optional) — Enable the advanced profile

Some integrations are gated behind the `advanced` Docker Compose profile and/or the `JESS_ADVANCED=true` env flag. Set them in `.env` and bring the stack up again:

```bash
# .env
COMPOSE_PROFILES=advanced
JESS_ADVANCED=true
```

```bash
docker compose up -d
```

This adds:
- `promtail` (ships container logs to a Loki instance you provide via `LOKI_PUSH_URL`)
- `nebula-sync` (multi-instance Pi-hole replication)
- `nut-exporter` (UPS monitoring)
- Owner-specific tools (`code_agent`, `ask_expert`, `query_budget`, `finance_status`, `check_claude_activity`)
- Background jobs (self-audit, training corpus drain)

Most users never need this. The default profile is the recommended starting point.

---

## Step 8 (optional) — HTTPS via Tailscale

Mobile mic access needs HTTPS. The simplest path is Tailscale Serve (free tier):

```bash
# Install Tailscale on the box (one-time)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Expose port 80 over HTTPS at https://<machine>.<tailnet>.ts.net/
sudo tailscale serve --bg http://localhost:80
```

This is the maintainer's deployment (`https://helios.tail74fc4a.ts.net`). Your iPhone / Android can hit the dashboard with a valid cert without opening any port on your router.

Cert renewal is automatic. To regenerate manually:

```bash
sudo tailscale cert --cert-file /opt/gateway_mvp/certs/<host>.crt \
  --key-file /opt/gateway_mvp/certs/<host>.key <host>.<tailnet>.ts.net
```

---

## Step 9 (optional) — Legacy bash setup helper

`scripts/setup.sh` is the **older** interactive terminal wizard that predates the browser-based one. It writes `.env` + `user_profile.yaml` from a sequence of CLI prompts. It still works and may be useful for headless / SSH-only installs, but the browser wizard is the recommended path for everyone else.

```bash
bash scripts/setup.sh
```

---

## Common failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `docker: Error response from daemon: could not select device driver "" with capabilities: [[gpu]]` | NVIDIA container toolkit not configured | Re-run `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `Failed to initialize NVML: Driver/library version mismatch` | Prebuilt NVIDIA module pinned to wrong kernel | Purge + reinstall DKMS variant (Step 1, troubleshooting) |
| `Error 804: forward compatibility was attempted on non supported HW` (vLLM on Blackwell) | Driver too old | Install driver 580+ |
| `brain-orchestrator` keeps restarting | `API_TOKEN` not set | Check `.env` has `API_TOKEN=...`; run `docker compose up -d` again |
| Wizard returns 410 on `/api/setup/env` | Setup already marked complete | This is by design (kill switch). Edit `data/app/setup_state.json` or use `/settings`. |
| `qwen-tts` crash-loops with HF 401 | Stale model name in env | Confirm `QWEN_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` (or unset to use compose default) |
| Frontend shows but `/setup` redirects to `/` | `setup_completed: true` already in `setup_state.json` | Either edit that file or accept that setup is done and use `/settings` |
| Model takes 2+ minutes to first-respond | Cold start — vLLM is loading 16+ GiB of weights into VRAM | Wait. Subsequent requests are fast. |

---

## What to do after install

- Open the dashboard, click around. Everything is keyboard-accessible.
- Talk to it: web UI, mobile browser, or via the OpenAI-compatible API (`POST /v1/chat/completions` with `Authorization: Bearer $API_TOKEN`).
- For the voice-puck path (ATOM Echo + Wyoming bridges), see [`docs/VOICE_AND_TTS.md`](VOICE_AND_TTS.md).
- For everything Jess can do voice-first, see [`docs/JESS_QUICK_START.md`](JESS_QUICK_START.md).
- To upgrade later, see [`docs/UPGRADE.md`](UPGRADE.md).
