# F-012: Paperless-ngx Bridge

**Priority:** P2 — QoL
**Status:** Done
**Depends on:** Paperless-ngx running on Jupiter, `document_vault` tool (untouched)
**Blocks:** None

---

## ADHD Insight

Scanning paper (receipts, medical, tax) belongs in Paperless — it has the OCR, the phone scanner, the auto-tagging. Typed notes belong in `document_vault`/mempalace — they're voice-searchable and tied into Jess's memory. The friction today is "where do I put this PDF?" Without a bridge, the Paperless side of that decision requires leaving Jess entirely. F-012 closes that by letting Jess hand files off to Paperless without trying to mirror its state.

## What Jess Does

- Gains a `paperless_save` tool: given a filename in the staging inbox, push it to Paperless with optional title/tags. Returns the Paperless task id so the user knows ingestion is queued.
- Exposes `POST /api/paperless/upload` for the frontend to stream file uploads straight to Paperless. No local copy on Helios, no `document_vault` row, no `mempalace` embed — Paperless owns the file and its metadata end-to-end.
- `document_vault` is **untouched**. It continues to handle typed/pasted notes. The split is deliberate: "text notes to Jess's brain" vs. "paper / PDFs to Paperless."

This is intentionally the *minimum* bridge. Things explicitly out of scope:
- **Jess doesn't index Paperless OCR into mempalace.** Cross-system search ("Jess, find that medical letter") requires a pull-side job polling Paperless; that's a future F-012a if the gap bites.
- **No download proxy.** Frontend links straight to Paperless's web UI for retrieval.
- **No file-path mirroring / file watchers.** Paperless owns its media tree.

---

## Interaction Examples

### Voice: save a staged scan with a title

```
User: "Jess, there's a PDF named tax-q3-2026.pdf in the inbox. Save it to
       Paperless, tag it taxes."
Jess: [tool_paperless_save(filename="tax-q3-2026.pdf",
                            title="Q3 2026 tax statement",
                            tags=["taxes"])]
Jess: "Queued tax-q3-2026.pdf for ingestion. Paperless will OCR and tag
       it in a moment."
```

### Frontend: drag-drop upload

```
User opens /paperless page (or a dialog wired from /documents).
Drags a PDF.
Frontend → POST /api/paperless/upload (multipart) → orchestrator forwards
to Paperless's POST /api/documents/post_document/ → returns task_id.
Dialog shows a link to the Paperless web UI.
```

### Paperless already handles (outside F-012):

- Consume dir scan (drop a file into `/mnt/documents/paperless/consume/` on Jupiter — Paperless picks it up automatically without touching the orchestrator at all).
- Paperless Mobile iPhone app: scans via phone camera, uploads via Tailscale. Never touches Jess.

---

## Tool schema

```json
{
  "name": "paperless_save",
  "description": "Send a file from the Paperless inbox (/app/data/paperless_inbox/) to Paperless-ngx for OCR and tagging. Use for scanned receipts, bills, tax documents, medical records, and other paper — NOT for typed notes (use document_vault for those).",
  "parameters": {
    "type": "object",
    "properties": {
      "filename": {
        "type": "string",
        "description": "Basename of the file inside the inbox dir. Must not contain path separators."
      },
      "title": {
        "type": "string",
        "description": "Optional title for the document. Paperless will infer one if omitted."
      },
      "correspondent": {
        "type": "string",
        "description": "Optional sender/author (e.g., 'IRS', 'Dr Smith Clinic')."
      },
      "document_type": {
        "type": "string",
        "description": "Optional doc type ('invoice', 'statement', 'medical', etc.)."
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional tags (strings)."
      }
    },
    "required": ["filename"]
  }
}
```

---

## New route

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/paperless/upload` | Bearer | Multipart file upload → forwarded to Paperless |

Form fields:
- `file` (required) — the file bytes
- `title`, `correspondent`, `document_type` (optional strings)
- `tags` (optional, repeated form field or comma-separated — handled server-side)

Response: `{"ok": true, "task_id": "uuid", "paperless_status_url": "..."}` or `{"ok": false, "error": "..."}`.

---

## Modified files

- NEW `orchestrator/paperless_manager.py` — async httpx client: `upload_file(content, filename, title=None, correspondent=None, document_type=None, tags=None)`. Always returns a dict, never raises.
- NEW `orchestrator/routes_paperless.py` — REST router with `POST /api/paperless/upload`.
- NEW `jess-features/F-012-paperless-bridge.md` (this file).
- `orchestrator/config.py` — new `paperless_*` settings + `model_validator` that auto-disables on missing token (same shape as F-011's ntfy secret check).
- `orchestrator/tool_handlers.py` — new `tool_paperless_save` handler + `@register_tool("paperless_save")` wrapper.
- `orchestrator/tool_definitions.py` — new `paperless_save` tool schema added to the list the model sees.
- `orchestrator/api_routes.py` — include the new paperless router.
- `orchestrator/metrics.py` — `bgw_paperless_upload_total{result}`, `bgw_paperless_upload_latency_seconds`.
- `docker-compose.yml` — pass through `PAPERLESS_*` env vars to the orchestrator service, add bind mount for the inbox staging dir.

---

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PAPERLESS_ENABLED` | `false` | Master switch — tool and route return "disabled" when false |
| `PAPERLESS_URL` | *(empty)* | Paperless server base URL, e.g. `http://10.0.0.248:8777` |
| `PAPERLESS_API_TOKEN` | *(empty)* | Paperless API token (from Settings → API Tokens in Paperless UI); ≥8 chars required when enabled |
| `PAPERLESS_INBOX_PATH` | `/app/data/paperless_inbox` | Container-side staging dir; host side is bind-mounted in docker-compose |
| `PAPERLESS_DEFAULT_TAGS` | *(empty)* | Optional comma-separated tags applied to every upload (e.g. `jess-ingested`) |
| `PAPERLESS_UPLOAD_TIMEOUT_SECONDS` | `30` | httpx timeout for the upload call |

---

## Security model

- Bearer auth on `/api/paperless/upload` (same as other `/api/*` routes; **not** in `PUBLIC_PREFIXES`).
- `paperless_save` tool validates `filename` is a **basename only** (no `/` or `..`). File is opened under `PAPERLESS_INBOX_PATH`; a resolved-path sanity check rejects any path that escapes the inbox root (defense against symlink attacks inside the staging dir).
- Max file size `DOCUMENT_MAX_SIZE_MB` (reuses the existing 100 MB cap that already applies to `/api/paperless/upload` via `_LARGE_UPLOAD_PATHS` in `orchestrator.py`).
- `PAPERLESS_API_TOKEN` is only sent in the `Authorization` header to the Paperless URL; never logged.
- Feature-flag auto-disable: if `PAPERLESS_ENABLED=true` but `PAPERLESS_API_TOKEN` is missing/short, the `model_validator` logs an error and flips `paperless_enabled=false`. Orchestrator boots normally.

---

## Metrics

- `bgw_paperless_upload_total{result="ok|fail|skipped"}` — Counter. `skipped` fires when the feature is disabled or config is incomplete.
- `bgw_paperless_upload_latency_seconds` — Histogram of orchestrator → Paperless upload round-trip.

---

## Testing checklist

- [ ] `paperless_save` with `PAPERLESS_ENABLED=false` returns the "disabled" message, no HTTP made.
- [ ] Invalid filename (`../etc/passwd`, absolute path, contains `/`) → rejected before reading the file.
- [ ] Valid filename, file missing in inbox → "not_found" error surfaced to the LLM.
- [ ] Happy path: file exists, httpx POST mocked 200 with `task_id` payload → success, metrics incremented.
- [ ] Paperless unreachable → `fail` metric incremented, never raises.
- [ ] REST route multipart upload happy path.
- [ ] REST route rejects oversize upload (handled by `RequestSizeLimitMiddleware`).
- [ ] `PAPERLESS_ENABLED=true` + empty token → `model_validator` logs + auto-disables; tool/route return "disabled".

---

## Future (not in this increment)

- **F-012a — OCR→mempalace indexer**: background job polls Paperless for new docs, pulls OCR text, writes into `mempalace` so Jess's `search_memory` finds them by voice. Only build if the cross-search gap actually bites.
- **Frontend `/documents` gains a "Push to Paperless" button**: currently `/documents` is tied to `document_vault`. Adding a tab or button that calls `/api/paperless/upload` is a small frontend change once this is shipped.
- **Replace `document_vault` file uploads entirely**: if after a month you only ever use `document_vault` for pasted text, remove its file-upload path and redirect all files to Paperless.
