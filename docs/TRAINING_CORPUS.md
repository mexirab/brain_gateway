# Training Corpus Drain

Persistent, append-only archive of user/assistant conversation turns. Built as a substrate for any future local training project (embedding personalization, mode_router classifier, persona tuning, etc.). Data accumulates continuously across container rebuilds because the output lives on a host bind mount.

## Purpose

Capture every conversation turn the orchestrator ever sees in a single normalized format, deduped, secret-filtered, and never deleted. Runs nightly so a future fine-tune can train on whatever has piled up.

## Location

| Path | What |
|------|------|
| `orchestrator/jobs_training_corpus.py` | Extractors, dedup, write path, `run_drain()` + `drain_training_corpus()` |
| `orchestrator/tests/test_training_corpus_drain.py` | 24 tests — extractors, secret filter, noise filter, dedup, oversize guard, malformed JSON |
| `/app/data/training_corpus/YYYY-MM.jsonl` | Output, one file per month, line-buffered append |
| `bgw_training_corpus_records_total{source}` | Prometheus counter; source ∈ `owui`, `state_store`, `cc_session` |

## Schedule

Registered in `orchestrator/orchestrator.py` alongside the RAG file watcher:

- `training_corpus_drain` — APScheduler cron, daily at 02:30 local
- `training_corpus_backfill` — one-shot 30s after startup. Idempotent via content-addressed dedup; also warms the Prometheus metric labels inside the FastAPI process so they show up immediately.

## Sources

| Source | Path (env var) | Extraction |
|--------|----------------|------------|
| `owui` | `TRAINING_CORPUS_OWUI_DB` = `/app/owui_data/webui.db` | `SELECT id, chat FROM chat`; walks `messages` or `history.messages`; accepts role ∈ {user, assistant}; joins list-of-parts content with spaces |
| `state_store` | `TRAINING_CORPUS_STATE_DB` = `/app/data/brain_state.db` | `SELECT conversation_id, role, content, created_at FROM chat_messages`; role ∈ {user, assistant} |
| `cc_session` | `TRAINING_CORPUS_CC_DIR` = `/root/.claude/projects/-opt-helios-gateway-mvp` | Walks `*.jsonl` sessions; user-only for v1; `_cc_extract_text()` drops `tool_result` parts, keeps `text` parts |

Output dir is `TRAINING_CORPUS_DIR` = `/app/data/training_corpus`.

The OWUI db is shared into the orchestrator container via a read-only mount on the `open-webui-data` named docker volume:

```yaml
# docker-compose.yml, orchestrator service
- open-webui-data:/app/owui_data:ro
```

SQLite is opened with `file:<path>?mode=ro&uri=true` + `PRAGMA query_only = ON` so the WAL-mode OWUI db is never upgraded to a write lock.

## Record format

`Record` dataclass in `jobs_training_corpus.py`:

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | sha1(`source|session_id|role|whitespace-normalized text`). Fingerprint used for dedup. |
| `ts` | str | ISO8601 best-effort. Unix epoch or string accepted; unknown → drain time. |
| `source` | str | `owui` \| `state_store` \| `cc_session` |
| `session_id` | str | Prefixed: `owui:<chat_id>`, `ss:<conv_id>`, `cc:<jsonl_stem>` |
| `role` | str | `user` \| `assistant` |
| `text` | str | Stripped content. Max 50000 chars (`MAX_RECORD_CHARS`). |

Serialized one-record-per-line JSONL, `ensure_ascii=False`, line-buffered writes so a mid-drain crash preserves everything up to the crash point.

## Dedup strategy

`_load_existing_ids()` scans **every** monthly file in the output dir and returns a `set[str]` of record ids. Backfills can land records in an older month, so a single-month scan is unsafe. The drain then skips any `Record` whose fingerprint already exists or was written earlier in the current run.

**Caveat / TODO:** full re-scan on every drain is fine today but becomes the bottleneck around ~100k records. Plan: sidecar `ids.idx` sorted-set file refreshed at write time.

## Secret filter

`SECRET_PATTERNS` in `jobs_training_corpus.py` — whole-record drop (not redaction) because adjacent context can re-identify a leaked secret. Matches:

- OpenAI / Anthropic `sk-...`
- GitHub `ghp_/gho_/ghu_/ghs_/ghr_`
- Google `AIza...`
- Slack `xox[baprs]-...`
- `bearer <token>`
- `api_key/token/secret/password = "..."` generic
- `-----BEGIN ... PRIVATE KEY-----`
- Fernet `gAAAAA...` (auto_learn ciphertext — useless for training and a leak vector)
- JWT `eyJ...` (HA long-lived tokens)

Shape is shared with `scripts/build_embedding_corpus.py` — keep both in sync when adding rules.

Also filtered: records shorter than `MIN_LEN` (2), Claude Code session-reminder prefixes via `_NOISE_PREFIXES` (`<system-reminder>`, `<command-*>`, `<bash-*>`, `<function_calls>`, `<ide_selection>`), oversized rows (`MAX_JSON_BLOB_BYTES` = 5 MB), oversized jsonl lines (`MAX_CC_LINE_BYTES` = 2 MB).

## Oversize-run alarm

If a single drain appends ≥ `OVERSIZE_RUN_WARN` (10000) records, `logger.error()` fires. Investigate: source injection, dedup miss, or legitimate first-time backfill.

## Inspecting the corpus

```bash
# Count by source in the current month
docker exec brain-orchestrator sh -c \
  "jq -r .source /app/data/training_corpus/$(date +%Y-%m).jsonl | sort | uniq -c"

# Sample 5 random records
docker exec brain-orchestrator sh -c \
  "shuf -n 5 /app/data/training_corpus/$(date +%Y-%m).jsonl | jq ."

# Total records across all months
docker exec brain-orchestrator sh -c \
  "wc -l /app/data/training_corpus/*.jsonl"
```

## Running manually

```bash
# Trigger a drain on demand (idempotent — dedups against existing files)
docker exec brain-orchestrator python -c \
  "from orchestrator.jobs_training_corpus import run_drain; print(run_drain())"
```

Returns `{"owui": N, "state_store": N, "cc_session": N}`.

## Companion: `scripts/build_embedding_corpus.py`

One-shot utility, **not scheduled**. Reads the raw sources plus the `~/rag/nadim_rag/` markdown documents and emits positive pairs for contrastive embedding fine-tuning to `data/embedding_finetune/pairs_v1.jsonl`. Flags: `--skip-mempalace`, `--rag-dir`, `--webui-db`, `--state-db`, `--cc-dir`, `--out`, `--sample-preview`. Use when kicking off a fine-tune experiment; the drain itself is the thing that keeps running forever.

## Known caveats

- **Full re-scan dedup** until the sidecar index lands (~100k records).
- **OWUI list-of-parts** content is joined with a single space — image parts and tool parts disappear. Fine for text-only training.
- **Assistant turns not extracted from Claude Code sessions** — trivial extension, skipped for v1 because they're voluminous technical Claude output.
- **Best-effort timestamps** — records without a source timestamp get the drain run time, which will cluster falsely. If you care about temporal ordering, filter on `source`.
