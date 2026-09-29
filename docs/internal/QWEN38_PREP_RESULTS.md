# Qwen3.8 migration prep — results

> Session: 2026-09-26, unattended, on Jupiter. Helios stayed unplugged the whole
> time; nothing GPU-side was run, woken, or SSH'd to. `brain-orchestrator` was
> left stopped. No running container was started, stopped, or recreated. `.env`
> was not modified.
>
> Branch: **`prep/qwen38-migration`** (not merged, not pushed, no PR).
> Task list: `docs/internal/HANDOFF_qwen38_prep.md`.
> Plan of record: `docs/internal/LOCAL_SINGLE_BOX_PLAN.md`.

---

## Task 1 — Weights staged ✅

| | |
|---|---|
| Repo | `RadixArk/Qwen3.8-27B-NVFP4` (exists, public, not gated) |
| Revision | `319f741cce68d7914884900c138a1fbb70a42f30` (pinned on download) |
| Repo last modified | 2026-08-22 (card says HF release 2026-08-14) |
| Size | **20.44 GiB** / 21,945,295,265 bytes across 22 files |
| Local path | **`/home/labadmin/models/Qwen3.8-27B-NVFP4/`** (Jupiter) |
| Verification | all 22/22 files present, every byte size matches the HF manifest exactly |
| Disk after | 506 GB free on `/` (was 526 GB) |

Downloaded with `huggingface_hub` 2.0.0 in a throwaway venv in the session
scratchpad (`~/venvs/ai-lab` exists but has no `huggingface_hub`; nothing was
installed into it). The 32K fallback was **not** downloaded — task 1 succeeded.

`Inferact/Qwen3.8-27B-NVFP4` (the official vLLM-recipe checkpoint) also exists:
revision `6128240ebaf4eaa7bad2b3d1c72c37d677c5f462`, **24.59 GiB**, 23 files,
last modified 2026-08-14. Notably it ships MTP weights as a separate file
(`nvfp4_experts_mtp.safetensors`, 849 MB) and splits into 6 shards.

### What the checkpoint actually is (from the staged files)

Read out of `config.json` / `chat_template.jinja` — this materially changes some
of the plan's assumptions, so it is worth recording:

- `architectures: ["Qwen3_5ForConditionalGeneration"]`, `model_type: qwen3_5`.
- **Hybrid attention**: `layer_types` is 64 entries — **48 `linear_attention` +
  16 `full_attention`** (`full_attention_interval: 4`). This is a hybrid
  GDN-style model, which matters a lot for the KV-cache decision below.
- `max_position_embeddings: 262144`, mRoPE (`mrope_interleaved: true`), so the
  262K claim is real at the checkpoint level.
- `mtp_num_hidden_layers: 1` → MTP weights are present, so
  `--speculative-config '{"method":"mtp",...}'` is applicable. The RadixArk card
  states MTP and vision tensors are kept at **source BF16** while MLP +
  `lm_head` are NVFP4 W4A4 and attention weights are FP8.
- `vision_config` is present → natively multimodal, which is what would restore
  `analyze_image` off the brain instead of Saturn's Qwen3-VL-8B.
- ⚠️ The RadixArk model card lists **SGLang** as the only supported runtime
  ("Supported Runtime Engine(s): SGLang", validated on 4×GB300 at TP4) and its
  only example command is `sglang serve`. It says nothing about vLLM. The
  vLLM-on-one-5090 story for this checkpoint comes entirely from the MiaAI-Lab
  repo, not from the publisher.
- The card's own parser recommendation is `--tool-call-parser qwen3_coder`
  (+ `--reasoning-parser qwen3`), i.e. the parser we already run.

---

## Task 2 — Tool-call parser compatibility audit (read-only)

**Verdict: works unchanged on the structured path; two real changes needed and
one piece of dead safety netting to stop trusting.** Nothing in this task
modified code.

### 2a. What the current deployment uses

| Where | Value |
|---|---|
| `docker-compose.yml:424` (`vllm-primary`, before this branch's edit) | `--tool-call-parser qwen3_coder --reasoning-parser qwen3`, inside the `VLLM_EXTRA_ARGS` default |
| `.env.example` | did **not** set `VLLM_EXTRA_ARGS` at all → the compose default applied |
| `docs/ENV_VARS.md` (model-layer table) | documents the same default string |
| Helios `vllm-primary.service` | **not inspectable** — Helios is unplugged. `CLAUDE.md` records it as `docker run vllm/vllm-openai:v0.19.1` on GPU0 port 8080 with `--speculative-config mtp`; `monitoring/prometheus/alert-rules.yml:442` independently confirms "`vllm-primary.service` runs `--speculative-config mtp`". |
| Image | `vllm/vllm-openai:v0.19.1` |

So: today's parser is **`qwen3_coder`**, not `qwen3_xml`.

### 2b. How `unified_loop.py` consumes tool calls

Two paths, and only the first one actually works for this model family.

**Structured path (the live one).** `_stream_model_round`
(`orchestrator/unified_loop.py:218`) reads SSE deltas and assembles
`delta.tool_calls` by `index`, accumulating `function.arguments` string
fragments (`unified_loop.py:287-303`). `delta.reasoning_content` (with a
`delta.reasoning` alias) is consumed at `unified_loop.py:282` and deliberately
discarded except for one boolean. Assembled calls are **only trusted from a
cleanly finished stream** — on any mid-stream exception they are dropped
(`unified_loop.py:310-325`), because half-parsed JSON arguments are worse than
no call. Results then go back as proper `role: "tool"` messages keyed by
`tool_call_id` (`unified_loop.py:849-858`).

**XML fallback path.** If `tool_calls` is empty but `content` is not,
`parse_xml_tool_calls` (`unified_loop.py:351`) runs; results are appended as a
`role: "user"` `<tool_response>` block instead (`unified_loop.py:860-868`).

**`StreamGate`** (`unified_loop.py:108-215`) suppresses `<think>…</think>` and
`<tool_call>…</tool_call>` spans from the live token stream, attribute-tolerant
(`<think reason=1>`), with partial-tag holdback bounded at 320 chars, and
`clean_response` (`unified_loop.py:84`) does the same on whole strings.

### 2c. Does anything assume the old parser's behavior?

- **`parse_xml_tool_calls` is dead code for Qwen3.8 — same as it already is for
  Qwen3.6.** It does `json.loads()` on the tag body, i.e. it expects
  `<tool_call>{"name":…}</tool_call>`. The staged `chat_template.jinja` (line 68,
  and the assistant-message branch at lines 128-143) emits
  `<tool_call>\n<function=NAME>\n<parameter=KEY>\nvalue\n</parameter>\n</function>\n</tool_call>`
  — **not JSON**. So the fallback raises `JSONDecodeError` and recovers nothing.
  This is already documented for `qwen3_coder` in `orchestrator/metrics.py:78-93`
  and stays true under `qwen3_xml`: both parsers decode the *same* wire format.
  No change needed, but do not treat the fallback as a safety net for this
  migration — the only real protection is vLLM's own parser working.
- **`finish_reason` is not branched on anywhere.** It is carried through
  (`unified_loop.py:589-599`) and logged for the `TOOL_CALL_SOURCE`
  classification only (`unified_loop.py:658-710`). Nothing requires
  `finish_reason == "tool_calls"`. Good — no assumption to break.
- **Reasoning is read from both `reasoning_content` and `reasoning`**
  (`unified_loop.py:282`, plus the buffered read at `unified_loop.py:695`), so
  either spelling works. With `--reasoning-parser qwen3` the thinking never
  reaches `content`, and `StreamGate` remains as the defensive second layer.
- **Streaming backend gating is by class, not by model** —
  `get_stream_capable_backend` (`orchestrator/orchestrator.py:335-345`) returns
  the backend iff it is `OpenAICompatibleBackend`, and
  `OpenAICompatibleBackend.stream_chat_completion`
  (`orchestrator/llm_backend.py:151-190`) forwards `tools`/`tool_choice`/
  `extra_body` and force-sets `stream: true`. Nothing model-specific.
- **No `<tool_call>`-in-content parsing is required**, so the `qwen3_coder` →
  `qwen3_xml` switch is invisible to the orchestrator as long as vLLM emits
  structured `tool_calls` deltas. `qwen3_xml` is the newer "simplified streaming
  XML tool call parser" and is the only Qwen3-XML-format parser listed in the
  current vLLM tool-calling docs; the vLLM Qwen3.8 launch post still shows
  `qwen3_coder`. Both are acceptable; this branch makes it an env var.

### 2d. Thinking controls — this is a real change

The staged chat template takes **both** `enable_thinking` and `reasoning_effort`:

```jinja
{%- if enable_thinking is undefined or enable_thinking is true %}
    {%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}
    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}
        {{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ ...) }}
```

Consequences for this codebase:

1. **The default is `xhigh`.** Today only the voice path disables thinking
   (`unified_loop.py:530-536` sets `{"chat_template_kwargs": {"enable_thinking":
   False}}`, with `max_tokens: 1024`), and the comment there records exactly what
   goes wrong when a reasoning phase eats the token budget: Qwen3.6 emitted
   700–2000 reasoning tokens before any `content`, producing empty replies. On
   Qwen3.8 every **non-voice** turn would default to `xhigh`. Fix server-side
   with `--default-chat-template-kwargs '{"reasoning_effort":"low"}'` (drafted)
   rather than touching the request path.
2. **A bad value is a hard 400, not a degradation** — the template calls
   `raise_exception`. So `reasoning_effort` must be exactly `xhigh|medium|low`.
3. The existing per-request `chat_template_kwargs` mechanism still works:
   `extra_body` is merged into the payload verbatim
   (`llm_backend.py:136-137`, `llm_backend.py:178-180`), and
   `chat_template_kwargs` is listed in `_VLLM_ONLY_KEYS`
   (`llm_backend.py:29-40`) so the cloud-fallback backends drop it instead of
   400ing. `enable_thinking: false` is still honored by the Qwen3.8 template
   (line 165), so the voice path needs no change.

### 2e. Changes required (the list)

1. `--tool-call-parser` → `qwen3_xml` (or keep `qwen3_coder`; both decode the
   same format). **Config only.**
2. Add `--default-chat-template-kwargs '{"reasoning_effort":"low"}'` so
   non-voice turns don't run `xhigh` by default. **Config only.**
3. `MODEL_NAME` / `FALLBACK_MODEL_NAME` → the new `--served-model-name`.
   **Config only.**
4. Stop counting on `parse_xml_tool_calls` (2c). **No code change; expectation
   change.** If you want a real net, it would have to parse the
   `<function=…><parameter=…>` form — deliberately not written here, since the
   supported fix is a working vLLM parser.
5. Watch `bgw_tool_calls_source_total{source="dropped"}` across the cutover.
   The alert and its runbook already exist (`alert-rules.yml:442`) and that
   runbook text is now partly stale — it is written for the 0.19.1→0.20.0
   upgrade.

### 2f. Unknown until tested on GPU

- Whether vLLM 0.27.x actually loads this **SGLang-targeted** NVFP4 checkpoint
  on one 5090 at all, and which `--quantization` value it wants (`auto` is the
  safe first try; the checkpoint carries both a ModelOpt `hf_quant_config.json`
  and a compressed-tensors-style `quantization_config` in `config.json`).
- Whether MTP speculative decoding and tool calling coexist on 0.27.x. There is
  a documented history of them not: `alert-rules.yml:442` cites open vLLM issue
  **#46249** — Qwen3.6-27B tool calls failing when MTP is enabled on 0.23.x.
  Test tool calls **with MTP on**, not just with MTP off.
- Real achievable `--max-model-len` with fp8 KV under `--enforce-eager`.
- Whether vision works through the brain well enough to repoint `VISION_*`.

---

## Task 3 — Draft deploy changes (branch only, nothing deployed)

### 3.1 `docker-compose.yml` — `vllm-primary` parameterized

- `image: vllm/vllm-openai:${VLLM_IMAGE_TAG:-v0.19.1}` (`docker-compose.yml:359`).
- New read-only bind mount `${VLLM_MODELS_DIR:-./data/models}:/models:ro`
  (`docker-compose.yml:379`) so hand-staged weights can be served as
  `VLLM_MODEL=/models/Qwen3.8-27B-NVFP4` with no re-download.
- New `environment:` block passing `PYTORCH_CUDA_ALLOC_CONF` and
  `VLLM_USE_FLASHINFER_SAMPLER` (`docker-compose.yml:381-386`), both empty by
  default so vLLM's own defaults apply.
- The `VLLM_EXTRA_ARGS` default block is now built from individually overridable
  vars (`docker-compose.yml:424`): `VLLM_KV_CACHE_DTYPE`, `VLLM_MAX_NUM_SEQS`,
  `VLLM_TOOL_CALL_PARSER`, `VLLM_REASONING_PARSER`, `VLLM_SPECULATIVE_CONFIG`,
  `VLLM_MM_ARGS`.

**Verified non-breaking.** `docker compose --profile models config` with no
overrides renders a command **byte-identical** to the pre-change one:

```
exec vllm serve "Lorbus/Qwen3.6-27B-int4-AutoRound" --served-model-name "qwen3.6-27b-int4"
  --quantization "auto_round" --max-model-len "153600" --gpu-memory-utilization "0.93"
  --enable-auto-tool-choice --enable-prefix-caching --host 0.0.0.0 --port 8000
  --kv-cache-dtype fp8_e4m3 --max-num-seqs 2 --tool-call-parser qwen3_coder
  --reasoning-parser qwen3 --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
  --language-model-only --skip-mm-profiling --performance-mode interactivity
```

Also verified: the Qwen3.8 override set renders correctly; nested
`${VAR:-…}` defaults (including the brace-containing `--speculative-config`
JSON) interpolate correctly on Docker Compose 2.40.3; and `/bin/sh -c`
word-splitting yields the right `argv` for single-quoted JSON flags.

Two semantics worth knowing, both now documented in the stanza comment and in
`docs/ENV_VARS.md`:

- `install.sh`'s below-floor branch writes `VLLM_EXTRA_ARGS=--tool-call-parser
  hermes` (`install.sh:415`) — **non-empty**, so it still replaces the entire
  default block and still drops the MTP flags the 8B AWQ model crashes on. The
  in-file comment claiming it "overrides this to a blank string" was wrong and
  is corrected: an empty value would fall back to the default (`${VAR:-…}`
  treats empty as unset), so blanking would *not* have worked.
- To drop a flag entirely you must override `VLLM_EXTRA_ARGS` wholesale. Which
  is why the Qwen3.8 recommendation below is a whole-string override: two of
  its flags (`--enforce-eager`, `--default-chat-template-kwargs`) have no
  dedicated slot.

### 3.2 `.env.example` + `docs/ENV_VARS.md`

`.env.example:233-281` documents the new vars and carries a commented,
uncomment-as-a-set **Qwen3.8 override block**. `docs/ENV_VARS.md` model-layer
table gains a row per new var, rewrites the `VLLM_EXTRA_ARGS` row for the new
splice/emptiness semantics, and points here.

The recommended starting config (drafted, **not validated on hardware**):

```
VLLM_IMAGE_TAG=v0.27.1
VLLM_MODELS_DIR=/home/labadmin/models
VLLM_MODEL=/models/Qwen3.8-27B-NVFP4
VLLM_SERVED_NAME=qwen3.8-27b-nvfp4
VLLM_QUANTIZATION=auto
VLLM_MAX_MODEL_LEN=32768
VLLM_CUDA_ALLOC_CONF=expandable_segments:True
VLLM_USE_FLASHINFER_SAMPLER=0
VLLM_EXTRA_ARGS=--kv-cache-dtype fp8 --max-num-seqs 2 --tool-call-parser qwen3_xml --reasoning-parser qwen3 --speculative-config '{"method":"mtp","num_speculative_tokens":3}' --default-chat-template-kwargs '{"reasoning_effort":"low"}' --enforce-eager --skip-mm-profiling
MODEL_NAME=qwen3.8-27b-nvfp4
```

**This is deliberately not the 262K config the plan called the recommended
starting point.** See 3.4.

### 3.3 Draft systemd unit

`tts/vllm-primary-qwen38.service.draft` — docker-run, GPU0, mirrors the
documented `vllm-primary.service` pattern and `tts/nemotron-vllm.service`'s
scaffolding. **The live unit is on Helios and was not inspectable** (box
unplugged), so this is reconstructed from `CLAUDE.md` +
`alert-rules.yml:442` and must be diffed against the real file before use.

Two deliberate choices: it listens on **port 8085**, not 8080, so it can run
beside the existing Qwen3.6 unit for the acceptance test (plan deploy step 4);
and it is documented as `start`-only, not `enable`d, until it replaces the 3.6
unit.

### 3.4 The PR #40914 requirement — answered

**What it is:** vLLM PR #40914, *"[Bugfix][Spec-Decode] TurboQuant K+1
spec-verify routing (fixes #40880)"*, opened 2026-04-26. It fixes speculative
decoding + TurboQuant KV compression under CUDA-graph capture producing
degenerate token cascades — a GPU→CPU sync (`.tolist()`) illegal during stream
capture made the verify pass ignore cached KV, so drafter and verifier collapsed
onto high-bias tokens (reportedly including `<tool_call>`). MiaAI-Lab measures
stock 0.27.1 garbling **13 of 15** runs, and 0/15 with the patch at ~160 tok/s.

**Has it merged?** **No.** As of 2026-09-26 the PR is still **open**. It is in no
released vLLM — current releases are v0.29.0 (2026-09-09) and **v0.30.0
(2026-09-22)**, well past it. Its linked issue #40880 was closed by the reporter
the day after filing, which is presumably why it drifted; but a *second*,
independent issue **#53180** (filed 2026-08-21, **still open**) reports the same
class of corruption against **stock v0.27.1** on **hybrid GDN models** — which is
exactly what this checkpoint is (48 linear + 16 full attention layers). Its
advice is explicit:

> "Do not combine `turboquant_k8v4` with speculative decoding on stock vLLM
> until this is fixed upstream; use `fp8` KV + MTP (clean here) or TurboQuant
> without spec-dec (clean here)."

**Which image tag to use:** there is no official tag that contains the patch.
The 262K/4-bit-KV/MTP config therefore requires **building a custom vLLM image**
(0.27.1 + an unmerged patch) and re-doing that on every upgrade. For an
assistant whose main job is tool calls, a failure mode that is *silent
corruption while all health signals stay green* is the worst one available, so
the draft takes the documented clean pairing instead: **stock
`vllm/vllm-openai:v0.27.1`, `--kv-cache-dtype fp8`, MTP on, `--enforce-eager`,
32K context** — the official vLLM recipe's verified single-5090 shape.

Be clear-eyed about the cost: **32,768 is a context *regression* from today's
153,600.** The plan doc's "32K is still 2× today's single-GPU plan" was written
against the older one-GPU draft, not against the deployed Qwen3.6 config. So the
conservative path trades context for correctness on day one, and raising
`--max-model-len` (via `--language-model-only`, a larger KV budget, or 4-bit KV
*without* MTP) is the first thing to tune once it boots. If 32K proves too tight
in practice, the honest options are: build the patched image, drop MTP and keep
4-bit KV, or stay on Qwen3.6 — not "hope stock TurboQuant+MTP is fine".

For reference, the recipe/official path: `vllm serve Inferact/Qwen3.8-27B-NVFP4
--tensor-parallel-size 1 --max-model-len 32768 --kv-cache-dtype fp8
--enforce-eager --reasoning-parser qwen3`, plus `--tool-call-parser qwen3_xml`
for tool use; `--enforce-eager` is described as mandatory (CUDA-graph capture
OOMs on 32 GB).

### 3.5 Lint

No Python was touched, so there was nothing new for `ruff` to check. Ran anyway
against the repo's config to confirm the branch is clean: `ruff check
orchestrator/` → **All checks passed** (ruff 0.16.9, run from a throwaway venv in
the session scratchpad; nothing installed into a project env).

---

## Remaining steps that need Helios powered on (in order)

1. Power Helios on, confirm both GPUs enumerate (`nvidia-smi`), and capture the
   real `/etc/systemd/system/vllm-primary.service` — diff it against
   `tts/vllm-primary-qwen38.service.draft` and fix the draft's assumptions.
2. `rsync` the staged weights Jupiter → Helios
   (`/home/labadmin/models/Qwen3.8-27B-NVFP4/`, 20.4 GiB).
3. Start the trial unit on **port 8085** alongside the live 8080 one. Confirm
   `GET /v1/models` reports `qwen3.8-27b-nvfp4` and `/health` is 200.
4. Smoke the wire format directly with `curl` against 8085 before involving the
   orchestrator: one plain completion, one tool-calling completion with `tools`
   + `tool_choice: auto`, both with `stream: true` and `stream: false`. Confirm
   structured `tool_calls` deltas arrive (index-keyed, `arguments` in fragments)
   and that `reasoning_content` — not `content` — carries the thinking.
5. Re-run step 4 **with MTP enabled and disabled** and compare. This is the
   vLLM #46249 risk (`alert-rules.yml:442`), and it is the single most likely
   thing to break the assistant.
6. Retune `--max-model-len` upward from 32768 and record what fits.
7. Point a throwaway orchestrator config at 8085
   (`MODEL_URL`/`MODEL_NAME`/`FALLBACK_MODEL_NAME`) and run the plan's
   acceptance session: HA call, `set_reminder`, `get_data` meds, calendar,
   `search_memory`, plus one voice-path turn (which exercises
   `enable_thinking: false`). Watch `bgw_tool_calls_source_total{source="dropped"}`
   and `bgw_chat_stream_outcome_total` throughout.
8. Only then cut `.env` over, recreate the orchestrator, and keep the Qwen3.6
   unit + weights on disk as rollback.
9. Separately: test vision through the brain before repointing `VISION_*`, and
   decide the STT question (`tts/stt_server_onnx.py` is still uncommitted, and
   its unit pins Parakeet **v2** while the GPU server runs **v3**).

---

## Failed, skipped, or found broken

- **Helios systemd unit not inspectable** (box unplugged, by instruction). The
  draft unit is a reconstruction, flagged as such in its header.
- **Nothing was benchmarked or run on a GPU.** Every performance number in this
  doc is a third-party claim, not a measurement here.
- **`monitoring/promtail/promtail-helios.yml` does not match what `CLAUDE.md`
  claims about it.** `CLAUDE.md` says the Helios sidecar "now scrapes systemd
  journal for `vllm-primary`, `llama-server`, `llama-server-coder`, `qwen-tts`,
  `parakeet-stt`, `brain-gateway`" and that the Docker keep-regex was "narrowed
  … (dropped `model-server` and `pihole`)". The file in this repo (90 lines) has
  **no journal scrape config at all** and its keep-regex still contains
  `model-server|pihole`. So either the live Helios config has drifted from the
  repo or that CLAUDE.md note is wrong. Consequence for this migration: vLLM's
  own logs likely are **not** in Loki, which means the F-014 self-audit can't
  see them and a new `vllm-primary-qwen38` unit won't be shipped either — worth
  fixing before the cutover, since vLLM's log is where a parser or MTP failure
  shows up first. Left untouched: out of scope for this handoff, and unverifiable
  with Helios down.
- **`alert-rules.yml:442`'s `JessToolCallsDropped` runbook text is now stale** —
  it is written for the 0.19.1 → 0.20.0 upgrade decision. Left untouched for the
  same reason; it should be rewritten as part of the actual cutover.
- The untracked files the handoff said to leave alone (`jess-face/`,
  `DNS_REDUNDANCY.md`, `tts/stt*`, `homeassistant/docker-compose.yml`) were not
  touched or committed. `docs/internal/HANDOFF_qwen38_prep.md`,
  `LOCAL_SINGLE_BOX_PLAN.md` and `DECISION_local_vs_cloud_brain.md` are also
  still untracked — left for the owner to commit, since the handoff only asked
  for this results doc to be committed.

---

## Verification pass — 2026-09-28

A second session re-checked the 2026-09-26 work before anyone relies on it.
Helios still unplugged; orchestrator still stopped; no containers touched; `.env`
untouched.

**Confirmed:**
- **Weights: cryptographically verified.** All 22 files re-hashed; every LFS
  file matches the sha256 in the HF manifest at revision
  `319f741cce68d7914884900c138a1fbb70a42f30`, and every non-LFS file matches in
  size. That revision is still the repo HEAD (nothing newer published).
- **Audit line refs** (`unified_loop.py`, `llm_backend.py`,
  `orchestrator.py:335-345`) still point at the cited code; no orchestrator code
  differs between `main` and this branch.
- **Compose render**, re-run: no-override render is still identical to `main`'s;
  the Qwen3.8 override set renders the expected image, `/models` bind mount,
  both env vars, and `vllm serve` argv.
- **PR #40914 is still open** (last activity 2026-08-21: a report that it does
  not apply cleanly to 0.27.1's cache layout, which makes the custom-build path
  even less attractive). The fp8-KV conclusion in 3.4 stands.
- The vLLM recipe's single-5090 command still matches 3.4 (`--enforce-eager`
  mandatory, fp8 KV, 32K). The recipe itself sets no tool parser, no MTP, and no
  `--max-num-seqs`. Those three come from this draft and are
  **untested together on 0.27.1**. Remaining-steps item 5 covers exactly that.

**Fixed (commit on this branch):**
- **Removed `--linear-backend flashinfer_cutedsl`** from the `.env.example`
  override set, the draft unit and this doc. It had no source: it is not in the
  vLLM recipe, the MiaAI-Lab repo, or the RadixArk card. The option came from
  vLLM PR #50572, **merged 2026-08-27**, after v0.27.1, so v0.27.1 would most
  likely refuse to start on an unknown choice. It is also irrelevant here:
  it only swaps the GEMM for *unquantized BF16* layers on **SM100**-family GPUs.
  The RTX 5090 is SM120, and this checkpoint's big layers are NVFP4/FP8.

**Deviation from the handoff, now stated explicitly:** the handoff asked for
`--max-num-seqs 1`; the draft uses **2**. MiaAI-Lab's "MTP + concurrency
crashes" note is specific to its `turboquant_4bit_nc` KV path, which this draft
does not use. `2` also matches today's deployment. The orchestrator is not a
single caller: chat, `auto_learn` and scheduled jobs overlap, so `1` queues them
against the 120 s loop timeout. **If MTP + fp8 misbehaves under two sequences on
the GPU, drop to 1 first** (one `.env` edit).

---

## Helios live state — captured 2026-09-28 (Helios powered on)

Read-only inspection over `ssh labadmin@10.0.0.195`. Nothing below was changed.
Several findings contradict `CLAUDE.md` and assumptions made earlier in this doc.

| | What CLAUDE.md / this doc assumed | What Helios actually runs |
|---|---|---|
| `vllm-primary` context | `--max-model-len 153600` | **`16384`**, `--gpu-memory-utilization 0.70`, `--enforce-eager` |
| `vllm-primary` MTP | `--speculative-config mtp` on | **No `--speculative-config` at all** (MTP is off) |
| `vllm-primary` KV | — | 1.04 GiB KV cache (vLLM log: "Maximum concurrency for 16,384 tokens per request: 1.24x") |
| `qwen-tts` GPU | GPU1 (PRO 5000), per its own unit Description | **GPU0 (RTX 5090)**, 4.7 GB. The unit sets `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES`, so it lands on the 5090 next to vLLM |
| Code agent | CLAUDE.md table says GPU1; the unit Description says GPU0 | GPU1 (`CUDA_VISIBLE_DEVICES=1`), 4.2 GB + experts in RAM |
| STT | `parakeet-stt.service` (NeMo, GPU1, v3) | **`stt-onnx.service` (ONNX/CPU, Parakeet v2, int8)** is enabled and running; `parakeet-stt` is disabled. The plan doc's "never deployed" is stale |
| Driver / arch | — | 580.173.02; both cards compute capability 12.0 (SM120) |
| Images on disk | — | `vllm/vllm-openai:v0.19.1` and `:latest` |

GPU0 memory at capture: 27.9 / 32.6 GB used (vLLM 23.2 GB + TTS 4.7 GB).
GPU1: 4.3 / 48.9 GB used (coder only).

### What this changes

1. **Section 3.4 correction: 32K is *not* a context regression.** Production
   actually serves 16K with MTP off. So Qwen3.8 at 32K with MTP on would be 2×
   the context plus speculative decoding. The plan doc's "32K is still 2×"
   was right after all.
2. **Plan deploy step 4 can't happen as written.** A trial next to the live unit
   on GPU0 is impossible. Qwen3.8 NVFP4 needs essentially the whole 5090 (the
   recipe says 31.4 GiB usable, `--enforce-eager` mandatory), and GPU0 already
   holds vLLM + TTS. The draft unit's "port 8085 alongside" premise is wrong for
   GPU0.
3. **The GPU1 layout from the plan is not in place yet.** The TTS must move off
   the 5090 (set `CUDA_VISIBLE_DEVICES=1` in `qwen-tts.service`) before Qwen3.8
   can have GPU0 to itself. It is a one-line change, but it touches a live unit.
4. **Non-disruptive trial option:** GPU1 (PRO 5000, 44 GB free, same SM120
   arch) can host a Qwen3.8 correctness trial without touching anything
   running. Throughput there is not representative (the 2026-04 bench put it at
   28–79% of the 5090). But the migration's real risk is tool-call and parser
   correctness under MTP, and that is architecture- and software-dependent,
   not bandwidth-dependent.

---

## GPU trial — 2026-09-28 (Helios on, GPU1, nothing live touched)

**Result: the drafted config works on stock vLLM v0.27.1, with no patch.
Tool calling is clean with MTP on.**

### Setup
- Weights rsynced Jupiter → Helios `/home/labadmin/models/Qwen3.8-27B-NVFP4/`
  (21,945,295,265 bytes, exact match with the sha256-verified copy).
- `docker pull vllm/vllm-openai:v0.27.1`
  (`sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967`).
- One-off `docker run --rm` container `vllm-qwen38-trial` on **GPU1 (PRO 5000)**,
  bound to `127.0.0.1:8085`, with exactly the draft's serve flags (minus the
  removed `--linear-backend`) and `--gpu-memory-utilization 0.80`. GPU1 was
  chosen because it runs nothing that had to stop (the coder kept running).
  GPU0 would have required stopping production vLLM and moving TTS.
  **Stopped afterwards**; Helios GPU state is back to what it was.
- No systemd unit installed, no live service or container touched, no `.env`
  change.

### What vLLM reported
- Quantization auto-detected as **`modelopt_mixed`** (`--quantization auto` works).
- Weights **20.57 GiB**; 16.42 GiB KV (fp8) = **287,630 tokens**; init 62 s.
- Only warnings: undocumented `min_frames`/`max_frames` transformers noise, the
  "KV scale 1.0" fp8 note, and "num_speculative_tokens > 1 may lower acceptance".
  No tracebacks.

### Tests (scripts + logs on Helios at `~/qwen38-trial/`)
`smoke.py` uses the orchestrator's real 45 `STATIC_TOOLS` schemas plus a stub
`home_assistant` tool (~35K chars of tools). It checks what `unified_loop.py`
depends on: structured `tool_calls` (index-keyed deltas when streaming), JSON-parseable
args, the right tool and args, no `<think>`/`<tool_call>`/`<function=` markup in
`content`, a tool-result follow-up turn, and the voice path
(`enable_thinking: false`, `max_tokens: 1024`).

| Check | Qwen3.6 (live, 5090, no MTP) | Qwen3.8 trial (PRO 5000, MTP-3) |
|---|---|---|
| Smoke (stream + buffered, 6 prompts, follow-up, voice) | 30/30 | **43/43** |
| Avg reasoning chars per turn | 246 | 158 (`reasoning_effort: low` default) |
| 2 simultaneous tool calls × 5 rounds (`max-num-seqs 2` + MTP) | — | **10/10** |
| Tool call at ~16.8K / ~24.4K prompt tokens | — | **ok / ok** |
| MTP acceptance | n/a | **73.4%** (3,218 / 4,383 draft tokens) |
| Pure decode, ~850-token answer | 52.0 tok/s | 36.0 tok/s |

The third long-context case (~31K prompt) returned HTTP 400. That was a test
artifact: the prompt plus `max_tokens=4096` exceeded the 32,768 limit.

**Not a speed verdict.** The decode numbers compare different cards (the PRO 5000
has less bandwidth) and different configs. The real Qwen3.8-on-5090 speed is
still unmeasured. The KV numbers say the 5090 has headroom: after ~21 GiB of weights,
~8 GiB of fp8 KV at 0.93 utilisation is on the order of 140K tokens for this
hybrid model. So `--max-model-len` well above 32K looks plausible once GPU0 is
free. Also untested: CUDA graphs (no `--enforce-eager`), which may be the bigger
speed lever.

### Revised remaining steps (supersedes the list above)

Done: step 1 (captured the live units), step 2 (weights on Helios), steps 3–5
(the config boots and tool calls are clean with MTP on, streamed and buffered, and
under concurrency). The rest, in order. **Each one changes the live box.**

1. **Move qwen-tts to GPU1:** add `Environment="CUDA_VISIBLE_DEVICES=1"` to
   `qwen-tts.service`, restart it, and confirm TTS still works. This is the plan's
   intended layout anyway, and GPU1 has room for it next to the coder.
2. **Maintenance window on GPU0:** `systemctl stop vllm-primary`, start the
   Qwen3.8 config on GPU0 (port 8080 or 8085), and re-run
   `~/qwen38-trial/smoke.py` + `stress.py` there. Then measure decode speed and
   raise `--max-model-len` (try 65K, then 131K). Optionally try without
   `--enforce-eager`.
3. Orchestrator acceptance session (plan step 6) against the Qwen3.8 endpoint,
   watching `bgw_tool_calls_source_total{source="dropped"}`.
4. Cut over `.env` (`MODEL_NAME`/`FALLBACK_MODEL_NAME`) and replace
   `vllm-primary.service`. Keep the Qwen3.6 unit, image and weights as rollback.
5. Vision via the brain (drop `--language-model-only`, which the trial already did)
   before repointing `VISION_*`.

### Docs drift found (not fixed here; for the docs pass at cutover)
`CLAUDE.md` says vllm-primary runs 153,600 context with MTP (actual: 16,384, no
MTP), that TTS is on GPU1 (actual: GPU0), and that STT is NeMo `parakeet-stt`
(actual: ONNX/CPU `stt-onnx`, Parakeet v2). The `qwen-tts.service` and
`llama-server-coder.service` Description lines also name the wrong GPU.

---

## 5090 trial — 2026-09-28 (owner-approved maintenance window)

**Result: Qwen3.8 on the 5090 runs clean tool calls at ~111 tok/s decode
(2.1× today's Qwen3.6) with 131K context (8× today's 16K).** It is ready for
the orchestrator acceptance session.

### Live changes made (with owner approval)
1. **qwen-tts moved to GPU1 (PRO 5000)** via a drop-in:
   `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` →
   `Environment="CUDA_VISIBLE_DEVICES=1"`. Healthy, same latency (1.71 s before,
   1.66–1.78 s after, same test sentence, `jessica` voice). **This stays in
   place.** GPU1 now holds coder 4.2 GB + TTS 5.0 GB.
   Revert: delete the file, `daemon-reload`, `restart qwen-tts`.
2. `vllm-primary` was stopped for the trial, then **restarted**. Qwen3.6 is
   serving on 8080 again (17/17 smoke after restart). Nothing was cut over:
   `.env` and `vllm-primary.service` are unchanged.

### Results on GPU0 (RTX 5090), stock `vllm/vllm-openai:v0.27.1`, fp8 KV, MTP-3, max-num-seqs 2

| Config | KV tokens | Smoke | Concurrent | Long ctx | Decode | MTP accept |
|---|---|---|---|---|---|---|
| 32K, `--enforce-eager` (the recipe) | 135,623 | 43/43 | 10/10 | ok @24K | **~36 tok/s** | 66% |
| 32K, CUDA graphs | 132,892 | 69/69 | 10/10 | ok @24K | **110–116 tok/s** | 69% |
| **131K, CUDA graphs (recommended)** | 197,283 | 30/30 | — | tool + recall ok @ 57K / 104K / 121K | **~111 tok/s** | — |
| *Qwen3.6 live, for reference* | 7,840 | 30/30 | — | — | *52 tok/s* | off |

- **Eager mode was the bottleneck, not bandwidth.** Eager on the 5090 was no
  faster than eager on the PRO 5000 (~36 tok/s each).
- **The recipe's "eager is mandatory" doesn't apply at `--max-num-seqs 2`.**
  Graph capture took 0.11 GiB / 2 s. Startup is ~115 s (including ~48 s of
  torch.compile) versus ~62 s eager.
- **No corruption seen with CUDA graphs + MTP + fp8 KV**: 99 smoke checks plus the
  concurrency, long-context and decode runs were all clean. The known corruption bugs
  (#40880/#53180) are TurboQuant-KV-specific. Keep watching
  `bgw_tool_calls_source_total{source="dropped"}` after cutover anyway; if output
  ever garbles, re-adding `--enforce-eager` is the first lever.
- Long-context recall: a fact planted mid-prompt was recalled at 57K/104K/121K.
  Time to first token at 121K cold was 31.6 s (prefill), and ~2 s once
  prefix-cached.
- More context is available (197K tokens of KV at 131K) but untested. 131K
  leaves headroom for 2 concurrent sequences.

`.env.example` override set and `tts/vllm-primary-qwen38.service.draft` are
updated to this config (131072, no `--enforce-eager`). Scripts and every log:
Helios `~/qwen38-trial/` (`run_gpu0.sh LEN UTIL [flags]` reproduces any row).

### Remaining steps (supersede all earlier lists)
1. **Orchestrator acceptance session.** Start the Qwen3.8 config on GPU0 (port
   8080 with the new served name), point the orchestrator's
   `MODEL_NAME`/`FALLBACK_MODEL_NAME` at `qwen3.8-27b-nvfp4`, start
   `brain-orchestrator`, and run real turns: HA, reminder, `get_data` meds,
   calendar, `search_memory`, a voice turn and a Telegram turn. Watch
   `bgw_tool_calls_source_total` and `bgw_chat_stream_outcome_total`.
2. Replace `vllm-primary.service` with the draft (port 8080, keep the live unit's
   `ExecStartPost` readiness loop; `TimeoutStartSec` ≥ 600 for the ~115 s startup
   plus the loop). Keep the old unit file, the v0.19.1 image and the Qwen3.6 weights as
   rollback.
3. Vision through the brain, then repoint `VISION_*`.
4. Docs pass: CLAUDE.md drift (see above) plus the new TTS placement.

---

## CUTOVER — 2026-09-28 (owner-approved)

**Qwen3.8-27B NVFP4 is now the live brain.**

### Changes made
- **Helios `vllm-primary.service` replaced** with the tested config (committed
  as `tts/vllm-primary.service`: v0.27.1, port 8080, 131K, CUDA graphs, MTP-3,
  `qwen3_xml`, `reasoning_effort: low`, readiness loop, `TimeoutStartSec=900`).
  Startup takes ~2m50s. Old unit saved as
  `/etc/systemd/system/vllm-primary.service.qwen36.bak`. (An older
  `.pre-singlegpu` backup from 2026-07-24 also sits there.) Post-install smoke:
  17/17, 111.6 tok/s.
- **Jupiter `.env`**: `MODEL_NAME` and `FALLBACK_MODEL_NAME` changed
  `qwen3.6-27b-int4` → `qwen3.8-27b-nvfp4`. Backup: `.env.bak-qwen36`
  (gitignored). Nothing else in `.env` changed.
- qwen-tts on GPU1 (drop-in from the earlier step) is unchanged.

### Orchestrator acceptance session (live `brain-orchestrator`, image = `main` code)
Started with `docker compose up -d --no-deps orchestrator`. Turns were chosen for
zero real-world effect.

| Turn | Path | Result |
|---|---|---|
| "Turn off the back porch light" (already off) | fast-path (not the model) | ok, no-op |
| Rephrased office-lamp request (already off) | **model → `home_assistant`** `turn_off light.office_floor_lamp` | ok, no-op, lamp still off |
| Set a stretch reminder, then cancel it in a follow-up | model → `set_reminder`, `check_system`, `cancel_reminder` | ok; the APScheduler job was really removed |
| Evening meds / morning meds (streamed) | structured facts block | correct (matched the meds YAML, incl. a weekday-only med) |
| Calendar tomorrow | model → `check_calendar` | ok (clear) |
| Coffee preferences | model → `search_memory` ×2 | ok (honestly: nothing stored) |
| Meds left tonight + calendar tomorrow | model → `selfcare_log{action:check}` + `check_calendar` | ok. `check` is read-only; no new selfcare row |
| Plain chat, streamed + buffered | model | ok, TTFT 0.5–1.3 s |
| HA-Assist voice turn (`You are 'Al'`) | model, voice path | ok |

Metrics: `bgw_tool_call_source_total{source="native"}=7`, `{source="none"}=10`,
**no `dropped`/`xml`**. No `bgw_chat_stream_outcome_total` degradations. No
content leaks or empty replies. Service registry reported vision / searxng /
expert unhealthy. That is expected: Saturn is down and searxng was not started.

**The orchestrator was stopped again afterwards**, restoring the paused state it
was in before this session. Restart it with `docker compose up -d --no-deps orchestrator`
(or without `--no-deps` to bring back redis/searxng).

### Rollback
Helios: `sudo cp /etc/systemd/system/vllm-primary.service.qwen36.bak
/etc/systemd/system/vllm-primary.service && sudo systemctl daemon-reload &&
sudo systemctl restart vllm-primary`. Jupiter: `cp .env.bak-qwen36 .env`, then
recreate the orchestrator. The Qwen3.6 weights and the v0.19.1 image are still on Helios.

### Still open
1. Vision through the brain → repoint `VISION_*` (the unit already omits
   `--language-model-only`).
2. Docs pass: CLAUDE.md model/service tables, the Helios GPU layout (TTS on GPU1,
   ONNX STT), the vllm-primary config, and the stale `JessToolCallsDropped` runbook.
3. Merge this branch (not done: the handoff forbids merging without the owner).

---

## Follow-ups — 2026-09-28

- **Vision via the brain: verified and repointed.** The real
  `vision_handler.analyze_image` path was run in a throwaway container (image =
  `main` code, `VISION_*` overridden, no data writes):
  - A 1024×768 food photo was described correctly (5.5 s cold).
  - A generated pharmacy label: every field was read correctly, including the
    circle's colour (2.3 s).
  - `meal_manager.estimate_from_photo` returned parseable JSON
    (1000 kcal, medium confidence).
  - Then, on the owner's request, Jupiter `.env` got
    `VISION_MODEL_URL=http://10.0.0.195:8080/v1` and
    `VISION_MODEL_NAME=qwen3.8-27b-nvfp4` (backup `.env.bak-vision-saturn`). The
    service registry now reports `vision` healthy. Vision needs Helios awake, like chat.
- **Expert model deprecated** (the owner's call). `.env` now has `EXPERT_ENABLED=false`
  and a blank `EXPERT_MODEL_URL` (backup `.env.bak-expert`). `ask_expert` is gone
  from the tool list (42 tools) and from the system prompt. `query_budget` analyze
  mode now returns the aggregated data with `expert_error` set and a hint for the
  brain to write the synthesis itself. Note: this `.env` change needed
  `docker compose up -d --force-recreate orchestrator`; a plain `up -d` did not
  recreate the container.
- **Orchestrator running again** at the owner's request (no longer paused).
  searxng / redis / frontend / monitoring are still stopped, so `web_search` is
  unavailable.
- **Docs pass done**: 17 files. See the CHANGELOG 2026-09-28 entry. The alert
  touched is `ToolCallsSilentlyDropped`; earlier references in this doc to a
  "`JessToolCallsDropped`" alert were wrong about the name.

- **Draft unit removed** (2026-09-29): `tts/vllm-primary-qwen38.service.draft`
  was superseded by the deployed `tts/vllm-primary.service` and deleted. Earlier
  sections of this doc still mention it by name as a record of what was done;
  recover it from git history if needed.

## Sources

- <https://github.com/MiaAI-Lab/Qwen3.8-27B-NVFP4-RTX-5090>
- <https://github.com/vllm-project/vllm/pull/40914> (open)
- <https://github.com/vllm-project/vllm/issues/40880> (closed by reporter)
- <https://github.com/vllm-project/vllm/issues/53180> (open)
- <https://recipes.vllm.ai/Qwen/Qwen3.8-27B>
- <https://huggingface.co/RadixArk/Qwen3.8-27B-NVFP4>
- <https://huggingface.co/Inferact/Qwen3.8-27B-NVFP4>
- <https://docs.vllm.ai/en/latest/features/tool_calling/>
- <https://github.com/vllm-project/vllm/releases>
