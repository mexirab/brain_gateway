# Run the brain on your own model (Mac, any box, or cloud)

You don't need a 24 GB NVIDIA GPU — or even Linux — to run Brain Gateway. The
assistant (the "nervous system": reminders, calendar, brain-dump, routines, RAG
memory) is CPU-only. It just needs a **model** to think with, and that model can
live wherever you want:

- **Local, on your Mac/PC** — Ollama or LM Studio. Fully private, no NVIDIA.
- **Local, on another box** you own — point at its LAN address.
- **Cloud** — Anthropic or OpenAI, opt-in, with **your own** API key.

Brain Gateway is **local-first**: the default and recommended path is a model you
run yourself. Cloud is there so people without capable hardware aren't shut out.

> **Privacy:** whatever you choose, your *stored* data — memory palace, RAG
> knowledge, reminders, chat history — stays on your machine in local SQLite +
> ChromaDB. The model backend is stateless: even in cloud mode, only the *current
> conversation turn's* prompt is sent. Your corpus never leaves. There is no
> telemetry in either mode. In cloud mode the turns go to **your own** provider
> account (Anthropic/OpenAI don't train on API data by default) — no middleman.

---

## What you need

- **Docker Desktop** (macOS / Windows / Linux): <https://www.docker.com/products/docker-desktop/>
- **A model source** — one of:
  - [Ollama](https://ollama.com) (easiest local): `ollama pull qwen2.5:14b`
  - [LM Studio](https://lmstudio.ai) (local, with a GUI)
  - an Anthropic or OpenAI API key (cloud)
- ~8 GB free RAM for a 14B local model (use a 7B on lighter machines), or almost
  nothing if you use cloud.

You do **not** run `install.sh` for this path — that one is for a dedicated
Ubuntu + NVIDIA box. Use the steps below instead.

---

## Quick start (guided)

```bash
git clone https://github.com/mexirab/brain_gateway.git
cd brain_gateway
bash scripts/byo-setup.sh
```

`byo-setup.sh` checks Docker, generates your login tokens, asks where your model
is, writes `.env`, and brings up the CPU services. When it finishes, open
**<http://localhost:3001/>** and log in with the `DASHBOARD_TOKEN` it printed.

---

## Manual setup (if you'd rather do it by hand)

### 1. Start your model

**Ollama (recommended):**
```bash
ollama pull qwen2.5:14b   # tool-capable; see "Model choice" below
# Raise the context window — Ollama's default (4096) truncates Jess's
# tool-heavy prompt and silently breaks tool calls. See "Context window" below.
OLLAMA_CONTEXT_LENGTH=16384 ollama serve   # OpenAI-compatible API on :11434
```

**Cloud:** nothing to start — just have your API key ready.

### 2. Configure `.env`

```bash
cp .env.example .env
# generate the two tokens the app needs:
python3 - <<'PY' >> .env
import secrets
print("API_TOKEN=" + secrets.token_urlsafe(32))
print("DASHBOARD_TOKEN=" + secrets.token_urlsafe(24))
PY
```

Then set the model + a few paths in `.env`:

**Local Ollama on this machine:**
```ini
MODEL_BACKEND=openai_compatible
MODEL_URL=http://host.docker.internal:11434/v1   # 'host.docker.internal' = your Mac, from inside Docker
MODEL_NAME=qwen2.5:14b
MODEL_API_KEY=
COMPOSE_PROFILES=
GATEWAY_ROOT_PATH=/absolute/path/to/brain_gateway
JESS_LAN_IP=localhost
```

**Cloud — Anthropic:**
```ini
MODEL_BACKEND=anthropic
MODEL_URL=https://api.anthropic.com
MODEL_NAME=claude-haiku-4-5
MODEL_API_KEY=sk-ant-...        # or ${ANTHROPIC_API_KEY} to read from your shell env
```

**Cloud — OpenAI:**
```ini
MODEL_BACKEND=openai
MODEL_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o-mini
MODEL_API_KEY=sk-...
```

> Keeping the key out of the repo: `.env` is gitignored, so a key there isn't
> committed. To go further, set `MODEL_API_KEY=${ANTHROPIC_API_KEY}` and export
> the real value in your shell — the orchestrator resolves `${ENV_VAR}` at start.

### 3. Bring up the CPU services

```bash
docker compose up -d orchestrator frontend redis searxng
```

This builds + starts only the CPU services — **not** the GPU model layer (which
stays off because `COMPOSE_PROFILES` is empty). First run builds the images.

### 4. Open it

- Dashboard: <http://localhost:3001/> — log in with your `DASHBOARD_TOKEN`.
- Health check: `curl -s http://localhost:8888/health`

---

## Model choice (important — this assistant is tool-heavy)

Jess works by calling tools (set_reminder, check_calendar, brain_dump, …) in an
agentic loop. The model **must do OpenAI-style tool calls reliably**, or it will
"talk about" actions instead of performing them.

| Works well | Notes |
|---|---|
| Qwen2.5-Instruct 7B / 14B / 32B, Qwen3 | Strong tool calling; 14B is a good local sweet spot |
| Llama 3.1+ 8B / 70B Instruct | Reliable tool calling |
| Claude (Haiku/Sonnet), GPT-4o / 4o-mini | Excellent tool calling (cloud) |
| **Avoid** | tiny (<7B), base (non-instruct), or older models — they tend to hallucinate tool calls rather than emit them |

Set the model id with `MODEL_NAME` (e.g. `qwen2.5:14b` for Ollama, the exact
provider id for cloud).

### Context window — raise it for Ollama

Jess's system prompt + ~30 tool schemas is **~9–13k tokens**. Ollama defaults to
a **4096-token** context and *silently truncates* anything longer — the tool
schemas and even your message get cut off, so the model emits broken tool calls
(it says "reminder set" but nothing is saved). Serve Ollama with a bigger window:

```bash
OLLAMA_CONTEXT_LENGTH=16384 ollama serve
```

…or bake it into a Modelfile (`PARAMETER num_ctx 16384`). 16384 is plenty; larger
just costs more RAM. vLLM, LM Studio, and the cloud APIs already default high
enough — this caveat is Ollama-specific.

---

## What works in BYO mode — and what doesn't

**Works (CPU only):** text chat, reminders, calendar + email awareness, brain
dump, task decomposition, routines, focus timer, RAG/memory, decision simplifier,
shopping list, Home Assistant control (if you point `HA_URL` at your HA).

**Off by default:** spoken **voice** (TTS) and **microphone** (STT), plus the
Home-Assistant voice pucks — those need the GPU model layer or a separate TTS/STT
server. The orchestrator detects they're absent and quietly disables the voice
tools; everything else runs normally.

---

## Troubleshooting

- **Dashboard loads but chat errors / "model unavailable":** the orchestrator
  can't reach your model. From inside Docker, `localhost` is the *container*, not
  your Mac — use `host.docker.internal` (Docker Desktop) for a model on the same
  machine, or the box's LAN IP. Verify: `curl http://localhost:11434/v1/models`
  (Ollama) on the host.
- **Cloud calls 401:** wrong/missing `MODEL_API_KEY`, or `MODEL_BACKEND` doesn't
  match the provider (`anthropic` vs `openai`).
- **It replies but never actually sets reminders/etc.:** two common causes —
  (1) **Ollama's context is too small** — the default 4096 truncates the
  tool-heavy prompt; serve with `OLLAMA_CONTEXT_LENGTH=16384` (see "Context
  window"). (2) your model is too weak at tool calling — switch to one from the
  table above. Tell them apart in the logs: `docker compose logs orchestrator |
  grep 'LLM probe'` — if `prompt_toks` is pinned near 4096, it's truncation.
- **Logs:** `docker compose logs -f orchestrator`.

See also: `docs/internal/BYO_MODEL_SPEC.md` (design + the env-var reference).
