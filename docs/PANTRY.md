# Pantry (F-015, Phase 1 — inventory)

Kitchen inventory for fridge / freezer / pantry: chat updates, photo stock-takes, barcode lookups. Default-OFF behind `PANTRY_ENABLED`. Spec + later phases (weekly plan, receipts, consumption tracking with decay): `jess-features/F-015-pantry-meal-prep.md`.

**Built:** inventory CRUD + event ledger, `pantry` tool, photo stock-take with review/commit, barcode lookup, `/pantry` page.
**Not built (Phases 2–4):** diet profile + meal plans, receipt scans, cook/portion events, decay job, weekly reconcile, Grafana panels/alerts. `shelf_life_days` is stored but nothing reads it yet; nothing sets `status=probably_gone` yet.

## Data model

Tables live in `brain_state.db` (`state_store.SCHEMA_SQL`), created unconditionally so flipping the flag needs no migration. Columns: `TECHNICAL_REFERENCE.md` → Pantry tables.

| Concept | Behavior |
|---------|----------|
| Locations | `fridge` \| `freezer` \| `pantry` |
| `track_mode` | `count` (eggs, cans — `qty`), `level` (`full/half/low/out`), `staple` (have/out only — spices, oil). Inferred from category + qty when not given |
| `shelf_life_days` | Per item; category default per location + name hints (e.g. eggs 28 d in the fridge). For the future decay job |
| Ledger | Every write appends a `pantry_events` row with `before_json`/`after_json` snapshots (`kind` add/consume/adjust/remove, `source` manual/chat/scan/barcode) |
| Names | Allowlist-sanitised, ≤80 chars; same barcode, or same normalised name + location, merges (restock) instead of duplicating |

## Chat tool (`pantry`)

Hidden from the schema unless `PANTRY_ENABLED` (`PANTRY_TOOL_NAMES`, in the tool-cache key); handler self-gates. Actions: `view | add | use | out | set | search | remove`. Schema: `TECHNICAL_REFERENCE.md` → pantry.

- `out` marks the item out **and** adds it to the `grocery` shopping list (dedup against unchecked entries) unless `add_to_shopping=false`. Also adds to the list when the item isn't tracked.
- `use` on a count item subtracts `count` (default 1); on a level item steps down one level (low stays low — only `out` empties it); staples stay "have".
- Fuzzy name resolution; ambiguous matches return the candidates. `remove` requires the exact name (fuzzy match → "did you mean").
- `prompt_builder._PANTRY_GUIDANCE` adds one routing line (out → pantry, not shopping_list) when enabled; `pantry` is in the EXTERNAL CONTENT rule's state-changing tool list.

## Photo stock-take

1. `POST /api/pantry/scan` — multipart, 1–8 photos + `kind` (`fridge|freezer|pantry`). JPEG/PNG/WebP by magic bytes (HEIC rejected — the frontend converts). Per-photo cap = `min(PANTRY_MAX_UPLOAD_MB, VISION_MAX_IMAGE_SIZE)`. Chunked uploads without `Content-Length` → 411. Max 3 open scans → 429. Returns 202 + `scan_id`.
2. Background `process_scan`: if `check_vision_health()` fails → scan `failed` with "Helios is probably asleep" (fires `wake_helios()` when `HELIOS_WAKE_ENABLED`). Photos go to the vision model **one at a time** behind a module lock (single llama slot) via `analyze_image(..., thinking=False)`.
3. Lines are merged across photos, matched to existing items **in the same location only**, and stored as `pending_review`. Low-confidence lines start unticked.
4. User edits the checklist and commits: matched lines **set** the item (a stock-take is the truth), unmatched lines add. Commit / discard / retry use atomic status transitions, so a double tap can't apply twice.
5. Photos are deleted on commit/discard. Startup `recover_interrupted_scans()` flips scans left `queued`/`processing` to `failed` (Retry offered). Scans stuck >10 min can be discarded.

**Why `thinking=False`:** with reasoning on, the Qwen3.8 brain spent the whole 2048 `max_tokens` budget thinking on a busy shelf photo and returned empty content. `vision_handler.analyze_image` gained a `thinking: bool = True` param that sends `chat_template_kwargs: {enable_thinking: false}`; use it for structured (JSON) extraction.

**Photo retention:** daily APScheduler job `pantry_sweep` (04:20, only when enabled) discards `failed`/`pending_review` scans older than 72 h and deletes unreferenced files in `PANTRY_PHOTOS_DIR` older than 24 h. `pantry_photos/` is excluded from `scripts/backup_state.py` (photos of the inside of the home, transient).

## Barcode lookup

`GET /api/pantry/barcode/{code}` — 8–14 ASCII digits only. Checks `barcode_cache`, else GETs Open Food Facts (`PANTRY_OFF_URL`, 5 s timeout, descriptive User-Agent). Only the digits leave the network. Hits cached; misses cached 7 days. Separate opt-in: `PANTRY_BARCODE_LOOKUP_ENABLED`.

Frontend decode uses `@zxing/browser` (iOS Safari has no `BarcodeDetector`). Live camera needs a secure context — see `docs/FRONTEND.md` → HTTPS. Over plain HTTP it falls back to decoding a photo of the barcode, or typed digits.

## Config

| Variable | Default | Notes |
|----------|---------|-------|
| `PANTRY_ENABLED` | `false` | Gates tool schema, `/api/pantry/*` router, sweep job, scan recovery, nav link |
| `PANTRY_PHOTOS_DIR` | `/app/data/pantry_photos` | Scan photo staging (uuid names) |
| `PANTRY_MAX_UPLOAD_MB` | `15` | Per photo; clamped 1–50 (out of range → 15); never above `VISION_MAX_IMAGE_SIZE` |
| `PANTRY_BARCODE_LOOKUP_ENABLED` | `false` | Open Food Facts lookups |
| `PANTRY_OFF_URL` | `https://world.openfoodfacts.org` | Must be http(s), else barcode lookup auto-disables (`validate_pantry_config`) |

Scans also need `VISION_ENABLED` (409 otherwise) and the Helios brain awake.

## Metrics

`bgw_pantry_scan_total{kind,result}` (`proposed|empty|failed|committed|discarded`), `bgw_pantry_scan_latency_seconds{kind,result}`, `bgw_pantry_events_total{kind,source}`, `bgw_barcode_lookup_total{result}` (`hit|cache|miss|error|disabled|invalid`). No Grafana panels or alerts yet.

## Key files

| File | Purpose |
|------|---------|
| `orchestrator/pantry_manager.py` | CRUD, ledger, scan pipeline, sweep, barcode client |
| `orchestrator/routes_pantry.py` | `/api/pantry/*` router (mounted only when enabled) |
| `orchestrator/tool_handlers.py` → `_handle_pantry` | `pantry` tool |
| `frontend/src/app/(private)/pantry/` | Page + `FeatureGate` layout |
| `frontend/src/components/pantry/` | `InventoryTab`, `ScanTab`, `AddItemForm`, `BarcodeScanner` |
| `frontend/src/lib/pantry-api.ts` | Typed client via `/api/proxy` |
| `frontend/src/lib/image-resize.ts` | Client-side downscale to 2048 px JPEG |
| `orchestrator/tests/test_pantry*.py` | Manager, scan pipeline, routes |
