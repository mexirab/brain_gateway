# Pantry (F-015, Phases 1–3 — inventory, weekly meal plan, receipts)

Kitchen inventory for fridge / freezer / pantry (chat updates, photo stock-takes, receipt scans, barcode lookups) plus a diet-profile weekly meal-prep planner. Default-OFF behind `PANTRY_ENABLED`. Spec + later phases (consumption tracking with decay): `jess-features/F-015-pantry-meal-prep.md`.

**Built:** inventory CRUD + event ledger, `pantry` tool, photo stock-take with review/commit, receipt scans (restock + learned store abbreviations + grocery tick-off), barcode lookup, diet settings, weekly plan (generate / accept / swap / rate), `meal_plan` tool, `/pantry` page (3 tabs), Grafana row.
**Not built:** Telegram photo-caption routing (`receipt`/`fridge` — deferred from Phase 3); Phase 4: cook/portion events (`prep_batches`), decay job, weekly reconcile, alerts. `shelf_life_days` feeds only the planner's "USE SOON" hint; nothing sets `status=probably_gone` yet.

## Data model

Tables live in `brain_state.db` (`state_store.SCHEMA_SQL`), created unconditionally so flipping the flag needs no migration. Columns: `TECHNICAL_REFERENCE.md` → Pantry tables.

| Concept | Behavior |
|---------|----------|
| Locations | `fridge` \| `freezer` \| `pantry` |
| `track_mode` | `count` (eggs, cans — `qty`), `level` (`full/half/low/out`), `staple` (have/out only — spices, oil). Inferred from category + qty when not given |
| `shelf_life_days` | Per item; category default per location + name hints (e.g. eggs 28 d in the fridge). For the future decay job |
| Ledger | Every write appends a `pantry_events` row with `before_json`/`after_json` snapshots (`kind` add/consume/adjust/remove, `source` manual/chat/scan/receipt/barcode) |
| Aliases | `pantry_aliases`: receipt text (`receipt_scanner.alias_key(raw)`) → item name, learned from receipt corrections |
| Names | Allowlist-sanitised, ≤80 chars; same barcode, or same normalised name + location, merges (restock) instead of duplicating |

## Chat tool (`pantry`)

Hidden from the schema unless `PANTRY_ENABLED` (`PANTRY_TOOL_NAMES` = `{pantry, meal_plan}`, in the tool-cache key); handler self-gates. Actions: `view | add | use | out | set | search | remove`. Schema: `TECHNICAL_REFERENCE.md` → pantry.

- `out` marks the item out **and** adds it to the `grocery` shopping list (dedup against unchecked entries) unless `add_to_shopping=false`. Also adds to the list when the item isn't tracked.
- `use` on a count item subtracts `count` (default 1); on a level item steps down one level (low stays low — only `out` empties it); staples stay "have".
- Fuzzy name resolution; ambiguous matches return the candidates. `remove` requires the exact name (fuzzy match → "did you mean").
- `prompt_builder._PANTRY_GUIDANCE` adds one routing line (out → pantry, not shopping_list) when enabled; `pantry` is in the EXTERNAL CONTENT rule's state-changing tool list.

## Photo stock-take

1. `POST /api/pantry/scan` — multipart, 1–8 photos + `kind` (`fridge|freezer|pantry`; `receipt` → see Receipt scan). JPEG/PNG/WebP by magic bytes (HEIC rejected — the frontend converts). Per-photo cap = `min(PANTRY_MAX_UPLOAD_MB, VISION_MAX_IMAGE_SIZE)`. Chunked uploads without `Content-Length` → 411. Max 3 open scans → 429. Returns 202 + `scan_id`.
2. Background `process_scan`: if `check_vision_health()` fails → scan `failed` with "Helios is probably asleep" (fires `wake_helios()` when `HELIOS_WAKE_ENABLED`). Photos go to the vision model **one at a time** behind a module lock (single llama slot) via `analyze_image(..., thinking=False)`.
3. Lines are merged across photos, matched to existing items **in the same location only**, and stored as `pending_review`. Low-confidence lines start unticked.
4. User edits the checklist and commits: matched lines **set** the item (a stock-take is the truth), unmatched lines add. Commit / discard / retry use atomic status transitions, so a double tap can't apply twice.
5. Photos are deleted on commit/discard. Startup `recover_interrupted_scans()` flips scans left `queued`/`processing` to `failed` (Retry offered). Scans stuck >10 min can be discarded.

**Why `thinking=False`:** with reasoning on, the Qwen3.8 brain spent the whole 2048 `max_tokens` budget thinking on a busy shelf photo and returned empty content. `vision_handler.analyze_image` gained a `thinking: bool = True` param that sends `chat_template_kwargs: {enable_thinking: false}`; use it for structured (JSON) extraction.

**Photo retention:** daily APScheduler job `pantry_sweep` (04:20, only when enabled) discards `failed`/`pending_review` scans older than 72 h and deletes unreferenced files in `PANTRY_PHOTOS_DIR` older than 24 h. `pantry_photos/` is excluded from `scripts/backup_state.py` (photos of the inside of the home, transient).

## Receipt scan (Phase 3)

`orchestrator/receipt_scanner.py`. Same upload/queue/status machine/photo cleanup/sweep as a stock-take with `kind=receipt` (`pantry_manager.RECEIPT_KIND`); `process_scan` delegates to `receipt_scanner.process_receipt` (which records its own outcome + latency). Stock-take tab kind picker: Fridge / Freezer / Pantry / Receipt.

**Upload.** 1–8 photos of ONE receipt (a long receipt in parts). Every photo's sha256 is stored (comma-joined in `image_sha256`); a photo identical to one in an earlier receipt scan → 409 `{reason: "duplicate", duplicate_of, duplicate_status}` unless `force=true`. Failed/discarded scans don't block. The UI offers "Open it" when the earlier scan is still open. Photos are re-encoded in the browser, so this only catches the same photo re-sent from the same device.

**Reading.** One vision call per photo (sequential, vision lock), `analyze_image(..., thinking=False)`, compact-JSON prompt `{store, date, total, items_sold, lines[{raw, name, qty, food, cat, sure}]}` within the existing 2048 `max_tokens` cap (~250 tokens per receipt, 2–6 s). `parse_receipt` hardens the reply in code:
- Payment/summary lines dropped: payment words only at the start of the printed line (with context), card fragments anywhere — so food like "Kirkland Signature almonds", "Fage Total", "Kool-Aid", "Balance bar", "Authentic salsa" survives.
- Runs of ≥4 digits masked; store name kept to letters only; date `YYYY-MM-DD`; total bounded.
- Truncated JSON is salvaged line-by-line with a "photograph long receipts in two or three parts" warning.
- Timeout → "busy" (brain saturated) vs transport failure → "asleep" (wakes Helios when `HELIOS_WAKE_ENABLED`); either stops the remaining photos. Model text is never echoed into errors.

Photos are merged (seam duplicate between consecutive photos dropped; 150 lines max). Warnings: `items_sold` ≠ lines read; same store + date + total as an already committed receipt.

**Matching** (per line, food only): `pantry_aliases` (`alias_key(raw)`) → inventory via `name_score` ≥ 88 (head noun + descriptor rule; "X with Y" compares X; peanut butter ≠ butter, oat/chocolate milk ≠ milk, sweet potato ≠ potato) → new item. One inventory item restocked once per receipt. Non-food and unsure (`sure=false`) lines start unticked. Grocery tick-off uses the line's own read name (never the restocked item's), `name_score` ≥ 95, previewed as "ticks off …".

**Review UI.** Store · date · $total, printed text per line, "remembered" badge for alias matches, restock shown as "Add N" (count) / "set back to full" (level), location picker per new line, Not-food collapsible, sticky Save bar, "Nothing to add — discard".

**Commit** (`commit_scan` → `_commit_receipt_scan` → `receipt_scanner.commit_receipt`): ONE DB transaction (restocks are additive, so a half-applied retried commit would double-count; `pantry_manager.add_item(conn=…)` joins it). Matched: counts +qty (≤50 per line), levels → full. Unmatched: `add_item(source="receipt")`. Aliases are learned only from real corrections (user renamed an unmatched line) or re-confirmed remembered lines; the raw text always comes from the stored proposal, never the request. Response adds `aliases_learned`, `grocery_checked`. Receipt totals are not pushed to finance.

**Retention.** After commit or discard the scan's `proposed_json` is cut to `{receipt: {store, date, total}}` (`minimise_record` — what the duplicate check needs); photos deleted as for stock-takes. No line-level purchase history is kept.

**Known limits:** alias keys keep size words ("5OZ" vs "12OZ" → different keys); the hash duplicate check only catches the same re-encoded photo from the same device; matching heuristics are English-centric.

## Barcode lookup

`GET /api/pantry/barcode/{code}` — 8–14 ASCII digits only. Checks `barcode_cache`, else GETs Open Food Facts (`PANTRY_OFF_URL`, 5 s timeout, descriptive User-Agent). Only the digits leave the network. Hits cached; misses cached 7 days. Separate opt-in: `PANTRY_BARCODE_LOOKUP_ENABLED`.

Frontend decode uses `@zxing/browser` (iOS Safari has no `BarcodeDetector`). Live camera needs a secure context — see `docs/FRONTEND.md` → HTTPS. Over plain HTTP it falls back to decoding a photo of the barcode, or typed digits.

## Weekly meal plan (Phase 2)

### Diet settings

`orchestrator/diet_profiles.py`. Runtime YAML at `DIET_PROFILE_PATH` (default `/app/data/diet_profile.yaml`); missing/unreadable file → defaults. Partial updates are merged, validated, written via `atomic_write_yaml` and audited with `log_config_change` (panel `diet`). Edited inline on the `/pantry` Meal plan tab (not a `/settings` panel). Eating preferences, not medical advice (the UI says so).

| Field | Default | Rule |
|-------|---------|------|
| `profile` | `mediterranean` | `mediterranean\|dash\|tlc\|pescatarian\|vegetarian\|vegan\|low_carb\|balanced` — each a built-in list of prompt rules |
| `servings` | 1 | 1–8 |
| `batch_day` | `sun` | `mon`..`sun`; the plan week starts on the next batch day (after 18:00 on batch day → next week) |
| `batch_lunch_days` | 4 | 0–6 lunches covered by one batch recipe (0 = no batch prep) |
| `fresh_dinners_per_week` | 4 | 0–7 |
| `max_dinner_minutes` | 30 | 10–180 |
| `avoid` | `[]` | ≤20 terms, ≤40 chars; **hard rule** (see validation) |
| `extra_rules` | `[]` | ≤10 lines, ≤200 chars; appended to the profile rules |
| `reminders` | `{prep: true, dinners: true, prep_time: "15:00", dinner_time: "17:30"}` | HH:MM |

Free text is stripped of control characters, brackets/braces/angle brackets, backticks, pipes and backslashes (it lands in a prompt). Both counts 0 → generate returns 400.

### Generation (`orchestrator/meal_planner.py`)

1. `brain_ready()` (`GET {MODEL_URL}/models`, 5 s) — down → 503 "Helios asleep" (fires `wake_helios()` when `HELIOS_WAKE_ENABLED`).
2. `start_generation_sync()` inserts a `generating` row under `BEGIN IMMEDIATE`: one at a time (409 with the running `plan_id`); `generating` rows >15 min are auto-failed first. Settings are snapshotted into `settings_json`. `launch()` runs `generate_plan` as a background task.
3. Prompt: profile rules + extra rules, `avoid` list, medication interaction notes (read server-side from the meds YAML `interactions`), fixed dates, inventory (≤120 lines, items ≤3 days from `shelf_life_days` tagged USE SOON and listed first), last ~4 weeks of recipes + 👍/👎 ratings. Strict JSON output.
4. Before each LLM call it waits up to 60 s for interactive chat to go idle (`bgw_active_requests` ≤ 0) — the brain has one llama slot. Attempt 1: thinking on, `max_tokens` 4500. Any problem → attempt 2 with the problems listed, thinking off, `max_tokens` 3500; the better result wins. 120 s timeout per call.
5. `validate_plan`: dinners only on offered dates, one per date, trimmed to the requested count; `avoid` terms drop an offending dinner and make an offending batch recipe invalid; duplicates flagged; recipe names pass a strict allowlist (they are spoken and pushed); steps/notes that name a medication are dropped; ≤25 ingredients, ≤12 steps. "Have it" is computed live against the inventory (rapidfuzz `token_set_ratio` ≥ 85), never trusted from the model.
6. Stored as `draft` with `warnings`: avoided-food problems + interaction flags (ingredient words that appear in a medication interaction note → "check with your prescriber before eating it"; keyword match, never advice).

Startup `recover_interrupted_plans()` fails leftover `generating` rows. A `generating` plan older than 10 min can be discarded (cancel).

### Accept / swap / rate / reminders

- **Accept** (`draft` → `active`): missing ingredients go to the `grocery` list (deduped), except those in `already_have`, which are added to the pantry as on-hand items (not staples, so they can run out). Any other `active` plan for the same week becomes `replaced`.
- **Reminders:** prep reminder on the batch day at `prep_time`, dinner reminders at `dinner_time`, past times skipped; delivered as normal reminders (`both` = voice + phone via the generic `reminder` speaker route). Ids are prefixed `mp-`; text avoids the word "meal". `reminder_manager.selfcare_action_for_reminder` makes Done taps (ntfy/Pushover ack route + Telegram) skip the selfcare bridge for `mp-` reminders, so finishing a cooking reminder never logs a meal or medication. Replacing/discarding a plan cancels its *pending* reminders (+ `_retry` jobs); delivered ones are kept so their buttons still work. Changing reminder times (`PUT /diet`) reschedules active plans (`rescheduled_plans` in the response).
- **Swap** one entry (draft or active; one swap at a time → 409 `busy`): thinking off, up to 2 tries, honours the *current* `avoid` list. On an active plan, new missing items are added to the grocery list and reminders rescheduled.
- **Rate** a recipe `up|down|clear`; ratings steer future prompts.

### `meal_plan` tool

`{action: view|generate|accept|swap|recipe, day?: YYYY-MM-DD, slot?: dinner|batch}`. `generate` returns immediately (the plan starts once the reply finishes); `accept` only when the user says so (prompt guidance + EXTERNAL CONTENT state-changing list). Output labels recipe names as data. Schema: `TECHNICAL_REFERENCE.md` → meal_plan.

Routes: `GET/PUT /api/pantry/diet`, `GET /api/pantry/plan`, `POST /api/pantry/plan/generate`, `GET /api/pantry/plan/{id}`, `POST /api/pantry/plan/{id}/accept|discard`, `POST /api/pantry/plan/entry/{id}/swap`, `POST /api/pantry/recipe/{id}/rate` — shapes in `TECHNICAL_REFERENCE.md` → Pantry.

### Known limits

- `recipes` rows are never pruned.
- Swapping or replacing a plan does not remove grocery items added earlier.
- Reminders use the generic `reminder` speaker route (voice + phone); no dedicated category.
- Avoid and interaction checks are keyword-based.

## Config

| Variable | Default | Notes |
|----------|---------|-------|
| `PANTRY_ENABLED` | `false` | Gates tool schema, `/api/pantry/*` router, sweep job, scan recovery, nav link |
| `PANTRY_PHOTOS_DIR` | `/app/data/pantry_photos` | Scan photo staging (uuid names) |
| `PANTRY_MAX_UPLOAD_MB` | `15` | Per photo; clamped 1–50 (out of range → 15); never above `VISION_MAX_IMAGE_SIZE` |
| `PANTRY_BARCODE_LOOKUP_ENABLED` | `false` | Open Food Facts lookups |
| `PANTRY_OFF_URL` | `https://world.openfoodfacts.org` | Must be http(s), else barcode lookup auto-disables (`validate_pantry_config`) |
| `DIET_PROFILE_PATH` | `/app/data/diet_profile.yaml` | Diet settings YAML (meal planner) |

Scans also need `VISION_ENABLED` (409 otherwise) and the Helios brain awake; plan generation/swap need the brain awake.

## Metrics

`bgw_pantry_scan_total{kind,result}` (kind `fridge|freezer|pantry|receipt`; result `proposed|empty|failed|committed|discarded`), `bgw_pantry_scan_latency_seconds{kind,result}` (buckets from 1 s), `bgw_pantry_receipt_lines_total{result}` (`matched_alias|matched_inventory|new|non_food`), `bgw_pantry_events_total{kind,source}`, `bgw_barcode_lookup_total{result}` (`hit|cache|miss|error|disabled|invalid`), `bgw_meal_plan_generated_total{result}` (`ok|retry_ok|invalid|llm_error|asleep|error|interrupted`), `bgw_meal_plan_latency_seconds`, `bgw_meal_plan_swap_total{result}` (`ok|invalid|llm_error|asleep|busy`), `bgw_meal_plan_swap_latency_seconds`. Planner LLM calls also count in `bgw_llm_calls_total` / `bgw_llm_call_duration_seconds` with `purpose=meal_plan|meal_plan_swap`.

Grafana: "Pantry & Meal Plan" row in `brain_gateway_sre.py` (generations, latency, llama queue, scans by kind+result & swaps, "Receipt Lines (/7d)", "Scan Latency p95 by Kind", logs). No alerts by design.

## Key files

| File | Purpose |
|------|---------|
| `orchestrator/pantry_manager.py` | CRUD, ledger, scan pipeline, sweep, barcode client |
| `orchestrator/receipt_scanner.py` | Receipt prompt, `parse_receipt`, photo merge, alias/inventory/grocery matching, one-transaction commit, record minimisation |
| `orchestrator/routes_pantry.py` | `/api/pantry/*` router (mounted only when enabled) |
| `orchestrator/diet_profiles.py` | Built-in profiles, diet settings YAML load/validate/save |
| `orchestrator/meal_planner.py` | Plan generation, validation, accept/discard/swap/rate, reminders, tool text |
| `orchestrator/tool_handlers.py` → `_handle_pantry`, `_reg_meal_plan` | `pantry` + `meal_plan` tools |
| `frontend/src/app/(private)/pantry/` | Page + `FeatureGate` layout |
| `frontend/src/components/pantry/` | `InventoryTab`, `ScanTab`, `AddItemForm`, `BarcodeScanner`, `PlanTab`, `DietSettingsCard` |
| `frontend/src/lib/pantry-api.ts` | Typed client via `/api/proxy` |
| `frontend/src/lib/image-resize.ts` | Client-side downscale to 2048 px JPEG |
| `orchestrator/tests/test_pantry*.py` | Manager, scan pipeline, routes |
| `orchestrator/tests/test_receipt_scanner.py`, `test_receipt_routes.py` | Receipt parsing/matching/commit, receipt routes |
| `orchestrator/tests/test_diet_profiles.py`, `test_meal_planner*.py` | Diet settings, planner, plan routes |
