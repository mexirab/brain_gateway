# Bring-Your-Own-Model — Spec / Plan

> Status: DRAFT (2026-06-14) · Owner: Nadim
> Goal: make Brain Gateway accessible to people without a 24 GB NVIDIA box, by
> letting the orchestrator point at ANY OpenAI-compatible / cloud model — while
> keeping local-first and privacy as the default.

## 1. Why

The product is pitched as a *local ADHD assistant*, but the install floor is
Ubuntu + a 24 GB+ NVIDIA GPU. That sells a local-first promise to ~1% of the
people it would help. The orchestrator, however, only needs `MODEL_URL` → an
OpenAI-compatible endpoint; the NVIDIA coupling lives entirely in `install.sh`.
So the brain can run on a Mac (Ollama / LM Studio), another box, or a cloud API —
without abandoning local-first.

## 2. The good news — the engine already does this

`orchestrator/llm_backend.py` already implements a full multi-backend layer:
- `LLMConfig(backend, url, model, api_key)`
- `OpenAICompatibleBackend` (Bearer, key optional) — vLLM / **Ollama** / **LM Studio** / llama.cpp
- `AnthropicBackend` (native `x-api-key`, with tool-schema conversion)
- `OpenAIBackend`
- `create_backend()` factory keyed on `backend` (`_BACKENDS`)

`shared.py` builds the primary + optional fallback from `user_profile.yaml`
(`llm:` section) with env fallbacks, and `_resolve_api_key()` supports
`${ENV_VAR}` indirection so keys never get committed.

**So cloud + BYO-local are undocumented config, not missing features.**

## 3. Privacy posture (the resolution)

Local-first is the default and the headline; cloud is an opt-in. Three facts make
that honest rather than a compromise:

1. **Stored data is always local.** Memory palace (ChromaDB), RAG, reminders, and
   chat history live in SQLite/Chroma on the box. The backend is stateless
   per-call — even in cloud mode only the *current turn's prompt* is sent. Your
   corpus never leaves.
2. **Cloud = bring-your-own-key.** Opt-in means the user supplies their own
   provider key (`${ENV_VAR}`). Data goes to their own provider account under API
   terms (Anthropic/OpenAI don't train on API data by default) — no middleman.
3. **No telemetry, either mode.**

Marketing line: *private by default; your choice to use cloud; your stored life
stays on your hardware no matter what.*

## 4. The gap (what's actually missing)

| Layer | Status |
|---|---|
| Backend implementations (openai_compatible / anthropic / openai) | ✅ done |
| `user_profile.yaml` `llm:` config (backend/url/model/api_key, `${ENV}`) | ✅ done |
| **Env-var surface** for backend + api_key (`.env`-driven installs) | ⚠️ partial — only `MODEL_URL`/`MODEL_NAME` existed; **added `MODEL_BACKEND` + `MODEL_API_KEY` (+ fallback variants) 2026-06-14** |
| Onboarding (a backend chooser + privacy consent in `setup.sh`) | ❌ not started |
| Non-Ubuntu / no-GPU install path (Mac/Win via `docker compose`) | ❌ not started |
| Docs ("Run the brain on your Mac", "Use a cloud model") | ❌ not started |

## 5. Plan (two layers, local-first)

### Part 1 — Local BYO (privacy-preserving core) — ✅ DONE 2026-06-14 (pending home test)
Make "run the brain on a model you own" a first-class, documented path:
- ✅ `scripts/byo-setup.sh` — non-Ubuntu/no-GPU sibling of `install.sh`: checks
  Docker, generates tokens, asks where the model is (local Ollama/LM Studio /
  other box / Anthropic / OpenAI), writes `.env`, brings up only the CPU services
  (`docker compose up -d orchestrator frontend redis searxng`).
- ✅ `docs/BYO_MODEL.md` — guided + manual quickstart, model-choice/tool-calling
  caveats, "what works vs what's off in BYO mode", troubleshooting, privacy.
- ✅ README: "No NVIDIA GPU?" callout + docs-table entry.
- Verified: the default compose profile is entirely CPU (GPU services are
  `models`-profiled off); `frontend` depends only on `orchestrator`; the mounted
  config files all ship in the repo; `service_registry` auto-disables voice tools
  when TTS/STT are absent. **Not runtime-tested yet** — needs a real Docker/Ollama
  run at home before commit.

### Part 2 — Cloud opt-in layer
- `MODEL_BACKEND=anthropic|openai` + `MODEL_API_KEY` (done at the config layer).
- Provider presets + a `setup.sh` chooser: **Local (recommended)** / Cloud, with
  an explicit "this sends your conversations to <provider>" consent on the cloud
  branch and a pointer to the privacy posture (§3).
- Optional: a `query_budget`-style cost note for cloud users.

## 6. Done so far (2026-06-14)
- `config.py`: added `model_backend`, `model_api_key`, `fallback_model_backend`,
  `fallback_model_api_key` settings (defaults keep local behavior unchanged).
- `shared.py`: env vars now feed `LLMConfig` (YAML still wins); api_key falls back
  to `MODEL_API_KEY` after `${ENV}` resolution.
- `.env.example`: consolidated all 8 model vars into one BYO-documented block with
  Ollama / Anthropic / OpenAI examples; removed the split/duplicate `MODEL_NAME`.

Next: Part 1 docs + the Mac `docker compose` path.
