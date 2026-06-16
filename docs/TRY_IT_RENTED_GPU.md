# Try it in 5 minutes on a rented GPU

Want to *see* Brain Gateway before buying a GPU or rebuilding your home setup?
Rent one by the hour. A 24 GB card on [RunPod](https://www.runpod.io) or
[vast.ai](https://vast.ai) runs roughly **$0.20–0.60/hour** — a full evening of
poking at it costs less than a coffee, and you tear it down when you're done.

This is a **kick-the-tires** path, not a place to keep your real data. Nothing
here is permanent: when you terminate the instance, it's gone. (That's also the
point — your throwaway test never becomes a privacy liability.)

> **Command sketches, not a turnkey script.** Provider UIs and image defaults
> drift. Treat the commands below as a sketch you adapt to whatever instance you
> rented, not a copy-paste guarantee.

---

## Pick the right kind of instance (this matters)

`install.sh` is written for a **full Ubuntu host** — it installs the NVIDIA
driver and **reboots once** so the kernel module loads. That only works on an
instance where you have a real OS, root, and the ability to reboot:

- **vast.ai:** choose an instance that boots a full VM / OS image (not just a
  command-in-a-container), or one of their bare-metal offers.
- **RunPod:** use a **bare-metal** or VM-style instance, not a serverless
  container pod.

If instead you land on a **container pod** (the common cheap default), you
**cannot reboot or install a kernel driver** inside it — but the host already
has NVIDIA drivers, so you skip that part entirely and bring the stack up by
hand (Route B below).

**Either way, pick an instance with ≥ 24 GB VRAM** so a decent model fits (see
[`docs/HARDWARE.md`](HARDWARE.md) for the tier table), and **~120 GB disk** for
images + model weights.

---

## Route A — full VM / bare-metal instance (runs `install.sh` as documented)

1. **Rent** an Ubuntu 22.04/24.04 instance with a ≥ 24 GB NVIDIA GPU, ≥ 32 GB
   RAM, ~120 GB disk, and **SSH access**.
2. **SSH in** and run the normal install:

   ```bash
   git clone https://github.com/mexirab/brain_gateway.git
   cd brain_gateway
   bash install.sh
   ```

   It installs Docker + the NVIDIA driver, **reboots once**, then auto-resumes on
   your next SSH login, brings up the full local-AI stack, and runs the
   2-question setup wizard. (Full walkthrough: [`docs/INSTALL.md`](INSTALL.md).)
3. **Reach the dashboard.** The dashboard is on port `3001` and the API on
   `8888`. On a rented box, don't expose those to the public internet — instead
   **SSH-tunnel** them to your laptop:

   ```bash
   ssh -L 3001:localhost:3001 -L 8888:localhost:8888 <user>@<instance-ip>
   ```

   Then open <http://localhost:3001/> locally and log in with the
   `DASHBOARD_TOKEN` the installer printed.

---

## Route B — container pod (drivers pre-installed; bring it up by hand)

On a container pod you skip the driver install + reboot (the host already has
them). Confirm the GPU is visible, then bring up the **models** profile directly:

```bash
# 0. Sanity check — the GPU should be visible inside the pod
nvidia-smi

# 1. Get the code
git clone https://github.com/mexirab/brain_gateway.git
cd brain_gateway

# 2. Minimal .env: copy the example, generate the two tokens, pick a model
cp .env.example .env
python3 - <<'PY' >> .env
import secrets
print("API_TOKEN=" + secrets.token_urlsafe(32))
print("DASHBOARD_TOKEN=" + secrets.token_urlsafe(24))
PY
# Turn on the GPU model layer and point paths at this checkout:
{
  echo "COMPOSE_PROFILES=models"
  echo "GATEWAY_ROOT_PATH=$(pwd)"
  echo "JESS_LAN_IP=localhost"
} >> .env
# Choose a model that fits your rented VRAM — see docs/HARDWARE.md.
# For a 24 GB card, e.g.:  VLLM_MODEL=Qwen/Qwen3-14B-Instruct-AWQ

# 3. Bring up the full stack (LLM + TTS + STT + orchestrator + dashboard)
docker compose up -d

# 4. Watch it come up; the LLM container takes a few minutes to load weights
docker compose logs -f orchestrator
curl -s http://localhost:8888/health
```

> **Caveat:** this assumes the pod has the **NVIDIA container toolkit** wired
> into Docker (so containers can see the GPU) and that you can run Docker inside
> the pod. Most GPU pods ship the toolkit; nested/Docker-in-Docker setups vary.
> If `docker compose up` can't see the GPU, that's an instance-image problem, not
> a Brain Gateway one — pick a Route A instance instead.

Then SSH-tunnel ports `3001`/`8888` to your laptop exactly as in Route A, and run
the 2-question wizard if you want to set your name/timezone:

```bash
bash scripts/setup.sh
```

---

## Cheaper still: don't rent a GPU at all

If you only want to feel the *interaction* (brain-dump → reminders → routines →
RAG) and don't care about local spoken voice, you can skip the GPU entirely and
point Brain Gateway at a **cloud model with your own API key** — runs on any
laptop with Docker, costs only the per-token API spend. See
[`docs/BYO_MODEL.md`](BYO_MODEL.md). That's also the path you'd keep if you decide
you like it but don't want to own a GPU.

---

## When you're done

```bash
docker compose down
```

…then **terminate the instance in the provider's console** so you stop paying.
Nothing synced anywhere — the throwaway box and everything on it disappear.

Liked it enough to keep it? Move to a permanent setup:
[`install.sh` on your own GPU box](INSTALL.md), the
[CPU/Mac/cloud path](BYO_MODEL.md), or a
[split CPU-node + GPU-box topology](BYO_MODEL.md#run-on-a-dedicated-linux-cpu-node).
</content>
