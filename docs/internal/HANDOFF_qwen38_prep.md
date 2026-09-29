# Handoff: Qwen3.8 migration prep (no Helios needed)

> Written 2026-09-26 for an unattended session on Jupiter. Helios is **unplugged** —
> do NOT try to wake it, SSH to it, or run anything GPU-side. The orchestrator
> (`brain-orchestrator`) is stopped on purpose; leave it stopped.
>
> Context: `docs/internal/LOCAL_SINGLE_BOX_PLAN.md` (plan of record — read it first).
> Target: Helios = RTX 5090 (GPU0, brain) + RTX PRO 5000 48 GB (GPU1, TTS + helpers).
> Brain moves from `Lorbus/Qwen3.6-27B-int4-AutoRound` on vLLM 0.19.1 to
> **Qwen3.8-27B NVFP4** on **vLLM 0.27.x**.

## Ground rules

- Work on a branch: `prep/qwen38-migration`. Do **not** merge to `main`, push, or open a PR.
- Do **not** start/stop/recreate any running container (HA, Pi-hole, Immich, Actual, etc.).
- Do **not** modify `.env` or anything deployed. Drafts only.
- Do not commit the unrelated untracked files (`jess-face/`, `DNS_REDUNDANCY.md`,
  `tts/stt*`, `homeassistant/docker-compose.yml`) — the owner decides those separately.
- Write findings into `docs/internal/QWEN38_PREP_RESULTS.md` as you go, so partial
  progress survives if the session stops.

## Task 1 — Stage the weights on Jupiter

1. Confirm the repo exists on Hugging Face and note its size / revision:
   `RadixArk/Qwen3.8-27B-NVFP4` (from github.com/MiaAI-Lab/Qwen3.8-27B-NVFP4-RTX-5090).
   Also note whether `Inferact/Qwen3.8-27B-NVFP4` (official vLLM recipe, 32K fallback)
   exists and its size.
2. Download **RadixArk/Qwen3.8-27B-NVFP4** (~22 GiB) to `/home/labadmin/models/Qwen3.8-27B-NVFP4/`.
   `huggingface_hub` may be in `~/venvs/ai-lab`; otherwise create a small venv in the
   scratch area. Jupiter has ~526 GB free. Run it in the background and verify
   completion (file count + sizes match the repo manifest).
3. Do **not** download the 32K fallback unless task 1 fails.
4. Record: exact repo, revision/commit hash, total size, local path.

## Task 2 — Tool-call parser compatibility audit (read-only)

The new setups use `--tool-call-parser qwen3_xml` (vLLM recipe / MiaAI) or `qwen3_coder`
(HF discussion). Today's config is in `docker-compose.yml` (`vllm-primary`,
`VLLM_EXTRA_ARGS`) and on Helios in `vllm-primary.service`.

Answer, with `file:line` references:
- Which parser does the current deployment use, and what does `.env.example` / compose set?
- How does `orchestrator/unified_loop.py` consume tool calls — structured `tool_calls`
  deltas vs. the XML-fallback path in content? What does `StreamGate` suppress?
- With `qwen3_xml`, vLLM should return structured `tool_calls`. Does anything in
  `unified_loop.py` / `cloud_brain.py` / `llm_backend.py` assume the old parser's
  behavior (e.g. hermes-style `<tool_call>` JSON in content, specific finish_reason,
  reasoning in `reasoning_content`)?
- Any risk from Qwen3.8's thinking controls (e.g. `enable_thinking` in `extra_body` /
  chat template kwargs)? Grep for how thinking is toggled today.
- Verdict: works unchanged / needs changes (list them) / unknown until tested on GPU.

Do not change code in this task — report only.

## Task 3 — Draft the deploy changes (branch only, not deployed)

On `prep/qwen38-migration`:
1. `docker-compose.yml` `vllm-primary` stanza: parameterize for vLLM 0.27.x + Qwen3.8
   NVFP4 (image tag, model path, `--kv-cache-dtype`, `--max-model-len`,
   `--tool-call-parser`, `--reasoning-parser qwen3`, MTP `--speculative-config`,
   `--max-num-seqs 1`). Keep it env-driven with the current defaults overridable —
   don't break the `models` profile for fresh installs.
2. `.env.example` / `docs/ENV_VARS.md`: document any new or changed vars.
3. A draft systemd unit for Helios, `tts/vllm-primary-qwen38.service.draft`, mirroring
   the current `vllm-primary.service` pattern (docker run, GPU0, port 8080) — note in
   the results doc that the live unit is on Helios and wasn't inspectable.
4. Note the PR #40914 patch requirement: find what it is (vLLM GitHub), whether it has
   merged into a released vLLM version by now, and which image tag to use.
5. Commit on the branch with a clear message. Run `ruff check` on any Python touched.

## Task 4 — Results write-up

`docs/internal/QWEN38_PREP_RESULTS.md` should end with:
- What was done (weights path/size, branch name, commits).
- Parser audit verdict.
- Remaining steps that need Helios powered on (in order).
- Anything that failed or was skipped, stated plainly.

Commit it on the branch too.
