# F-015: Pantry & Meal Prep

**Priority:** P1 — daily-life feature; supports health-motivated diets (e.g. heart-healthy)
**Status:** Phases 1 (inventory) and 2 (diet profile + weekly plan) done — Phases 3–4 not started. Built-state reference: `docs/PANTRY.md`
**Depends on:** vision (`VISION_*`, the Helios brain), `shopping_list` (state_store), `log_meal` (meal_manager), F-011/F-013 push buttons, Telegram bot
**Blocks:** None

---

## ADHD Insight

Meal prep fails in three predictable places for an ADHD brain:

1. **Inventory blindness.** Out of sight is out of mind — the spinach at the back of the fridge doesn't exist until it's slime. Buying a third jar of cumin because you couldn't remember the first two.
2. **Planning paralysis.** "What should I cook this week?" is an open-ended decision with too many inputs (what's in the fridge, what's healthy, what's going off, what I feel like). It gets deferred until it's 8pm and the answer is takeout.
3. **Logging decay.** Any system that needs you to record every egg you use lasts about four days.

The design principle for #3 is the important one: **accept that inventory will drift, make drift cheap to correct.** Consumption is inferred from events the user does anyway (cooking a planned meal, eating a prepped container, saying "finished the hummus"), uncertain items decay to "probably gone" instead of lying, and one weekly photo pass resets the error. The user never has to do bookkeeping; they only answer short yes/no questions about specific discrepancies.

## What Jess Does

- **Initial stock-take:** photograph each fridge shelf / pantry shelf / freezer drawer → vision lists items → user reviews and fixes a checklist → committed as inventory. Dry goods can be barcode-scanned instead (Open Food Facts lookup).
- **Weekly plan:** given the inventory, the diet profile (default Mediterranean), and a "mixed" cook style — one batch-prep session for lunches + several fresh-cooked dinners — Jess proposes a week's plan that uses expiring items first and lists what's missing. Missing items go on the existing grocery `shopping_list`.
- **Receipt scan:** photo of the receipt → line items → matched to pantry items (learning store abbreviations from corrections) → review → committed. Non-food lines dropped.
- **Usage tracking (layered, see below):** cook events deduct recipes, prepped portions are counted down, chat updates handle off-plan use, perishables decay, weekly fridge check reconciles.

---

## The Consumption Model (the hard part)

Six layers, cheapest first. Each writes a row to `pantry_events`, so drift is always explainable.

| # | Signal | User effort | What it changes |
|---|--------|-------------|-----------------|
| 1 | **Cook event** — "made the chickpea bowls" in chat, or Done on the prep reminder push | One tap | Deducts the recipe's ingredients. A batch recipe also creates a `prep_batches` row with N portions. |
| 2 | **Portion eaten** — "had a chickpea bowl" (chat / `log_meal` / Done on a lunch push) | One tap / sentence | Decrements `prep_batches.portions_left`. Raw ingredients untouched (already deducted at cook time). Also logs a meal. |
| 3 | **Chat corrections** — "out of olive oil", "finished the hummus", "used half the feta" | One sentence | Sets level / count. "Out" on a staple → shopping list. |
| 4 | **Coarse quantities** | None | Items are tracked by `count` (eggs, cans), `level` (full/half/low/out), or `staple` (have/out only — spices, oil, salt). No grams. |
| 5 | **Perishable decay** | None | Each item has `shelf_life_days` (category default, overridable). Past it and unconfirmed → `status=probably_gone`. Jess asks rather than assumes; probably-gone items are never planned around. Prepped portions get a fridge-life too (default 4 days). |
| 6 | **Weekly fridge check** — before generating next week's plan | ~2 min, 3–6 photos | Vision lists what it sees; diffed against expected inventory; user only answers the *differences* ("Spinach — gone?", "I see yogurt I didn't know about — add it?"). Unseen-but-expected non-perishables are NOT auto-removed (photos miss things at the back). |

Off-plan eating that the user never mentions is the residual error; layer 6 bounds it to one week.

---

## Interaction Examples

### First stock-take (dashboard)
```
/pantry → Scan → "Fridge" → takes 4 shelf photos
  [background job, ~10–20s per photo on the brain]
Review screen:
  ☑ Eggs            count 8      fridge
  ☑ Baby spinach    level: half  fridge   (fresh, ~5d)
  ☑ Feta            level: full  fridge
  ☐ "white container" — unknown   [rename / drop]
  [+ add item]                    [Commit 11 items]
```

### Planning (chat or Sunday nudge)
```
User: plan my meals for this week
Jess: Here's the week (Mediterranean). Batch prep Sunday: chickpea & roasted
      veg bowls ×4 (lunches Mon–Thu). Dinners: Mon salmon + farro, Tue lentil
      soup, Thu shakshuka, Sat leftovers night. Spinach and the half pepper get
      used first. Missing: salmon, farro, lemons — added to your grocery list.
      Want to swap anything?
```

### Usage
```
[Sun 15:00 push]  "Prep time: chickpea bowls ×4"   [Done] [Snooze]
  → Done: deducts chickpeas, peppers, olive oil(level), feta(level); creates 4 portions

User (Telegram, Tue): had a bowl for lunch
Jess: Logged. 2 bowls left, good until Thu.

User: we're out of olive oil
Jess: Marked out and added to the grocery list.
```

### Receipt
```
/pantry → Scan → "Receipt" → photo
Review:
  KS ORG BBY SPIN    → Baby spinach   (+1)        [learned alias]
  SALMON FIL ATL     → Salmon fillet  (+1)        [new — confirm]
  BAG FEE            → (dropped: non-food)
  [Commit]
```

---

## Data Model

New tables in `state_store` (same SQLite DB as `meals` / `shopping_list`). All timestamps ISO-8601 local, matching existing tables.

```sql
CREATE TABLE IF NOT EXISTS pantry_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                 -- canonical display name, e.g. "Baby spinach"
    category TEXT NOT NULL DEFAULT 'other',   -- produce|dairy|protein|grain|legume|canned|condiment|spice|frozen|snack|beverage|other
    location TEXT NOT NULL DEFAULT 'pantry',  -- fridge|freezer|pantry
    track_mode TEXT NOT NULL DEFAULT 'level', -- count|level|staple
    qty REAL,                           -- count mode only
    level TEXT,                         -- level/staple mode: full|half|low|out (staple: full|out)
    status TEXT NOT NULL DEFAULT 'active',    -- active|probably_gone|gone
    shelf_life_days INTEGER,            -- NULL = non-perishable
    acquired_at TEXT,
    last_confirmed_at TEXT NOT NULL,
    barcode TEXT,
    source TEXT NOT NULL DEFAULT 'manual',    -- manual|scan|receipt|barcode|chat
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pantry_items_status ON pantry_items(status);

CREATE TABLE IF NOT EXISTS pantry_events (      -- append-only ledger
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,                    -- NULL for batch-level events
    kind TEXT NOT NULL,                 -- add|consume|adjust|cook|portion|decay|reconcile|remove
    source TEXT NOT NULL,               -- chat|receipt|scan|cook|portion|decay|barcode|manual|reconcile
    before_json TEXT, after_json TEXT,  -- {qty,level,status}
    ref_type TEXT, ref_id INTEGER,      -- scan / recipe / prep_batch / plan_entry
    note TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry_aliases (     -- receipt abbreviation learning
    alias_norm TEXT PRIMARY KEY,        -- upper, collapsed whitespace, digits stripped
    item_name TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- fridge|pantry|freezer|receipt|reconcile
    photo_paths TEXT NOT NULL,          -- JSON list, all under PANTRY_PHOTOS_DIR
    image_sha256 TEXT,                  -- receipt dedup
    status TEXT NOT NULL,               -- queued|processing|pending_review|committed|discarded|failed
    proposed_json TEXT,                 -- parsed + matched lines for the review UI
    error TEXT,
    created_at TEXT NOT NULL, committed_at TEXT
);

CREATE TABLE IF NOT EXISTS barcode_cache (
    barcode TEXT PRIMARY KEY, name TEXT, category TEXT, payload_json TEXT, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    diet_tags TEXT,                     -- JSON list
    servings INTEGER NOT NULL DEFAULT 2,
    batchable INTEGER NOT NULL DEFAULT 0,
    method TEXT,                        -- short numbered steps (feeds decompose_task)
    est_kcal INTEGER, est_satfat_g REAL, est_fiber_g REAL,   -- per serving, ESTIMATES
    source TEXT NOT NULL DEFAULT 'llm', -- llm|user
    times_cooked INTEGER NOT NULL DEFAULT 0,
    rating INTEGER,                     -- 1-5, user feedback steers future plans
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    recipe_id INTEGER NOT NULL, item_name TEXT NOT NULL,
    amount TEXT,                        -- free text "1 can", "2 cups" — display only
    consume TEXT NOT NULL DEFAULT 'some',   -- all|some|count:N  (how cooking changes the item)
    optional INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meal_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL, diet_profile TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft|active|done
    notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meal_plan_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL, day TEXT NOT NULL,   -- ISO date
    slot TEXT NOT NULL,                 -- breakfast|lunch|dinner|snack|prep
    recipe_id INTEGER, mode TEXT NOT NULL,  -- batch|fresh|leftovers|portion
    prep_batch_id INTEGER,
    status TEXT NOT NULL DEFAULT 'planned'  -- planned|cooked|eaten|skipped
);

CREATE TABLE IF NOT EXISTS prep_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL, cooked_at TEXT NOT NULL,
    portions_made INTEGER NOT NULL, portions_left INTEGER NOT NULL,
    good_until TEXT NOT NULL, location TEXT NOT NULL DEFAULT 'fridge'
);
```

**Consume semantics on cook:** `count:N` subtracts N; `all` sets out; `some` steps a level down (full→half→low; low stays low and Jess asks "anything left of the feta?" at the next reconcile). Staples are never auto-deducted by cooking — only "out" by chat or reconcile.

**Diet profile** — runtime YAML `/app/data/diet_profile.yaml` (same pattern as `selfcare_schedule.yaml`; written via `config_writer.atomic_write_yaml`, audited with `log_config_change(panel="diet")`):

```yaml
profile: mediterranean        # mediterranean|dash|tlc|vegetarian|vegan|pescatarian|low_carb|custom
cook_style: mixed             # batch|fresh|mixed
batch_day: sun
batch_slots: [lunch]          # which slots batch prep covers
fresh_dinners_per_week: 4
servings: 1
avoid: []                     # free-text dislikes / allergies — hard constraints
extra_rules: []               # free text, e.g. "fish twice a week"
```

Built-in profiles live in code (`diet_profiles.py`) as rule lists fed to the planner prompt, e.g. Mediterranean: vegetables/legumes/whole grains daily, fish ≥2×/week, olive oil as main fat, limit red/processed meat and butter/cream; TLC/DASH as cholesterol-oriented alternatives. Profiles are preferences, not medical prescriptions — UI copy says so.

**Medication interactions:** the planner reads medication `interactions` from the meds YAML *server-side* (via `data_manager`, never through the allowlisted `/api/config/personal-facts` route that strips them). If a planned ingredient matches an interaction keyword, the plan shows a neutral flag ("grapefruit — your meds list notes an interaction; check with your prescriber") and the planner is told to avoid it. No dose/timing talk — consistent with the MEDICATION SAFETY rule.

---

## Pipelines

### Photo scan (fridge / pantry / freezer / reconcile)
1. `POST /api/pantry/scan` (multipart, 1–8 images, `kind`) → files saved under `PANTRY_PHOTOS_DIR` (uuid names, jpg/jpeg/png/webp/heic→reject, size cap) → `pantry_scans` row `queued` → returns `scan_id` immediately.
2. Background task processes photos **sequentially** (single llama slot — don't fan out) with a strict-JSON prompt: `[{name, category, track_mode, count|level, confidence}]`. Reuses `vision_handler.analyze_image` + a `_parse_json_list` helper shaped like `meal_manager._parse_estimate`.
3. Merge across photos (rapidfuzz name dedup — already a dependency via `backlog_manager`), match against existing inventory, store `proposed_json`, status `pending_review`.
4. `GET /api/pantry/scan/{id}` polled by the UI; `POST /api/pantry/scan/{id}/commit` with the user-edited list applies changes + writes events. `kind=reconcile` proposals are diffs (`add` / `confirm` / `probably_gone?`), not a full replace.
5. Helios asleep → scan stays `queued`; if `HELIOS_WAKE_ENABLED`, fire `wake_helios()` and retry; otherwise fail with an honest "brain is asleep" message.

### Receipt
Same upload/queue/review shape with `kind=receipt`. Prompt asks for `[{raw, qty, price, is_food}]` + store/date/total. Matching order: `pantry_aliases` exact → rapidfuzz vs inventory + recent names (score ≥ 85) → model's best-guess name flagged `new`. On commit, every user-confirmed raw→name pair upserts `pantry_aliases`. Duplicate guard: same `image_sha256`, or same store+date+total, → warn before commit. Receipt totals are NOT pushed to finance (Actual Budget already has the transaction).

### Barcode (pantry dry goods)
Frontend camera decode with `@zxing/browser` (iOS Safari has no `BarcodeDetector`). `GET /api/pantry/barcode/{code}` — code must be 8–14 digits; checks `barcode_cache`, else server-side GET to Open Food Facts (`PANTRY_OFF_URL`, descriptive User-Agent per their policy, 5 s timeout), caches, returns `{name, category}` for the add form. Only the barcode number leaves the network. Gated by `PANTRY_BARCODE_LOOKUP_ENABLED`.

### Plan generation
Single `call_model` (not the tool loop) with: active inventory (compact JSON, perishables sorted by days left), open prep portions, diet rules + `avoid` + interaction keywords, cook style, last 3 weeks' recipe names + ratings (variety), strict JSON output schema. Validate: every non-optional ingredient either in inventory or emitted as `missing`; drop entries containing `avoid` terms; reject + one retry on invalid JSON. Save as `draft`; user accepts (→ `active`, missing → `shopping_list`, schedules prep + dinner pushes) or asks to swap one entry (single-entry regeneration). Runs as a background job — expect 30–90 s on the brain.

### Decay job
APScheduler cron `pantry_decay` daily 04:15: items with `shelf_life_days` whose `last_confirmed_at + shelf_life` has passed → `probably_gone` (event `decay`); prep batches past `good_until` → portions_left 0. Expiring-soon perishables (≤ 2 days) feed one line into `morning_briefing` ("Use today: spinach, half a pepper").

---

## Tools (LLM-facing)

Two tools to keep the schema small (the tool schemas already cost ~33k chars). Hidden from the schema unless `PANTRY_ENABLED` (same pattern as `PAPERLESS_TOOL_NAMES`; add to the tool-cache key).

```
pantry          {action: view|add|use|set|out|search, item?, count?, level?, location?, query?}
                 view → grouped, capped list (probably_gone separated); never > 8000 chars
meal_plan       {action: view|generate|swap|cooked|ate|skip, day?, slot?, recipe?, portions?}
                 cooked → cook event (layer 1); ate → portion (layer 2) + log_meal
```

Scans/receipts are upload flows (dashboard + Telegram photo with caption `fridge` / `receipt`), not tools — the model never reads raw receipt text as instructions. `_TOOL_GUIDANCE` gets one routing line: "ate/had a prepped meal → meal_plan ate (it also logs the meal); don't also call log_meal".

## Routes (bearer-gated, proxied via `/api/proxy/*`)

`GET/POST /api/pantry/items`, `PATCH/DELETE /api/pantry/items/{id}`, `GET /api/pantry/events?item_id=`, `POST /api/pantry/scan`, `GET /api/pantry/scan/{id}`, `POST /api/pantry/scan/{id}/commit|discard`, `GET /api/pantry/barcode/{code}`, `GET/PUT /api/pantry/diet`, `GET /api/pantry/plan?week=`, `POST /api/pantry/plan/generate`, `POST /api/pantry/plan/{id}/accept`, `POST /api/pantry/plan/entry/{id}/swap|cooked|skip`, `GET /api/pantry/prep`. Uploads join `_LARGE_UPLOAD_PATHS` with a pantry-specific cap.

## Frontend

`frontend/src/app/(private)/pantry/page.tsx`, tabs: **Inventory** (grouped by location, tap to bump level/count, "probably gone" tray with Still have / Gone buttons), **Plan** (week grid, prep session card, swap button, missing-items → shopping), **Scan** (fridge / receipt / barcode / weekly check, then review checklist). Diet panel in `/settings`. Nav link hidden unless `GET /api/config/features` reports `pantry_enabled`. Mobile-first: this is used standing in the kitchen.

## Nudges

- Weekly fridge check: push on `batch_day - 1` evening (default Sat 18:00) with "3 photos, 2 minutes" → link to `/pantry?scan=reconcile`. Then plan generation.
- Prep session push at `batch_day` 15:00 with Done/Snooze (Done = cook event).
- Optional fresh-dinner push at 17:30 on planned dinner days (Done = cook event). Respects DND + active routines like other announcements.

## Env vars

`PANTRY_ENABLED` (default false), `PANTRY_PHOTOS_DIR` (`/app/data/pantry_photos`), `PANTRY_MAX_UPLOAD_MB` (15), `PANTRY_BARCODE_LOOKUP_ENABLED` (false), `PANTRY_OFF_URL` (`https://world.openfoodfacts.org`), `PANTRY_DECAY_TIME` (`04:15`), `PANTRY_RECONCILE_NUDGE` (`sat 18:00`), `PANTRY_PREP_NUDGE_TIME` (`15:00`), `DIET_PROFILE_PATH` (`/app/data/diet_profile.yaml`).

## Metrics

`bgw_pantry_scan_total{kind,result}`, `bgw_pantry_scan_latency_seconds{kind}`, `bgw_pantry_events_total{kind,source}`, `bgw_pantry_items{status}` (gauge), `bgw_meal_plan_generated_total{result}`, `bgw_barcode_lookup_total{result}` (`hit|cache|miss|error|disabled`), `bgw_pantry_decay_last_run_timestamp_seconds` (seeded at startup, like the briefing gauges).

## Security / privacy

- Uploads: reuse the meal-photo pattern — extension allowlist, uuid filenames, `Path.resolve().relative_to(PANTRY_PHOTOS_DIR)` confinement, size cap. Photos excluded from backups? **No** — include in `backup_state.py` only if small; decide at build time (receipts are the useful audit trail).
- Vision/OFF output is untrusted data: names length-capped (80), control chars stripped, run through `_neutralize_control_markup` before anything reaches the tool loop; never interpolated into prompts as instructions.
- OFF lookup: digits-only validation (no SSRF), fixed base URL from env.
- Repo is public: tests use invented inventories; never commit real receipts, photos, or the diet YAML.

---

## Build Phases

Each phase is independently useful and shippable behind `PANTRY_ENABLED`.

### Phase 1 — Inventory (done)
- [x] Tables `pantry_items`, `pantry_events`, `pantry_scans`; `pantry_manager.py` CRUD + event ledger
- [x] `pantry` tool + `_TOOL_GUIDANCE` line + schema gating
- [x] Photo scan pipeline (fridge/pantry/freezer) + review/commit
- [x] Barcode lookup (`barcode_cache`, OFF client) + zxing scanner in UI
- [x] `/pantry` Inventory + Scan tabs; `pantry_enabled` feature flag in `/api/config/features`
- [x] Tests: CRUD, event ledger, scan JSON parsing/merge, path confinement, barcode validation (`tests/test_pantry.py`, `test_pantry_scan.py`, `test_pantry_routes.py`)

**Phase 1 as built — deviations from the spec above:**
- **Vision needs `thinking=False`.** With reasoning on, the Qwen3.8 brain spent all 2048 `max_tokens` thinking on a busy shelf photo and returned empty content. `vision_handler.analyze_image` gained `thinking: bool = True`; scans pass `False` (`chat_template_kwargs: {enable_thinking: false}`). Use it for any structured extraction.
- **Helios asleep:** the scan fails fast via `check_vision_health()` (fires `wake_helios()` when `HELIOS_WAKE_ENABLED`) instead of sitting `queued`; the user taps Retry once Helios is up.
- **Matching:** same-location only (rapidfuzz `token_sort_ratio` ≥ 88); matched lines SET the item on commit (stock-take = truth); low-confidence lines start unticked.
- **Uploads:** magic-byte JPEG/PNG/WebP (not extension), 411 without `Content-Length`, max 3 open scans (429), per-photo cap bounded by `VISION_MAX_IMAGE_SIZE`. Frontend downscales to 2048 px JPEG (strips EXIF/GPS, converts HEIC).
- **Photos (open question 3 for stock-takes):** deleted on commit/discard, daily `pantry_sweep` (04:20) clears stale scans (72 h) + orphan files (24 h), excluded from `backup_state.py`. Receipts (Phase 3) still undecided.
- **Tool:** no `query` field — `search` uses `item`; added `remove` (exact name) and `add_to_shopping`; categories gained `bakery`, `prepared`.
- **Metrics:** `bgw_pantry_scan_latency_seconds` carries `{kind,result}`; `bgw_barcode_lookup_total` adds `invalid`. `bgw_pantry_items` gauge not built.
- **Barcode camera** needs HTTPS (secure context); the dashboard is served via Tailscale Serve. Over HTTP it decodes a photo of the barcode or typed digits.

### Phase 2 — Diet profile + weekly plan (done)
- [x] `diet_profiles.py`, `diet_profile.yaml` (`DIET_PROFILE_PATH`), settings UI (inline, see deviations)
- [x] Tables `recipes`, `recipe_ingredients`, `meal_plans`, `meal_plan_entries`
- [x] Plan generation (background task) + validation + swap; accept → shopping list + prep/dinner reminders
- [x] Medication-interaction flagging (server-side read)
- [x] `/pantry` Meal plan tab; `meal_plan` tool (view/generate/accept/swap/recipe)
- [x] Recipe ratings (👍/👎) fed back into the prompt
- [x] Grafana "Pantry & Meal Plan" row (pulled forward from Phase 4; no alerts by design)
- [x] Tests: `test_diet_profiles.py`, `test_meal_planner.py`, `test_meal_planner_routes.py`

**Phase 2 as built — deviations from the spec above:**
- **Diet settings live inline on the `/pantry` Meal plan tab**, not a `/settings` panel. Still atomic YAML + `config_changes` audit (panel `diet`). Added fields: `fresh_dinners_per_week`, `max_dinner_minutes`, `extra_rules`, `reminders {prep, dinners, prep_time, dinner_time}`.
- **No `prep_batches` yet** (Phase 4): batch lunches are plan entries (`slot` `prep|lunch`, `mode` `batch|portion`). The `meal_plan` tool has no `cooked|ate|skip`; routes have no `cooked|skip` or `GET /prep`, and `GET /plan` takes no `?week=`. Added: `POST /plan/{id}/discard`, `POST /recipe/{id}/rate`, `GET /plan/{id}`.
- **Slot etiquette:** each planner call waits up to 60 s for interactive chat to go idle (`bgw_active_requests`). Attempt 1 runs with thinking on (`max_tokens` 4500); any problem → one retry with thinking off. The brain is checked up front (503 "Helios asleep", wakes it when `HELIOS_WAKE_ENABLED`).
- **Avoid list is a hard rule:** offending dinners are dropped, an offending batch recipe invalidates the plan; swaps honour the current avoid list. Recipe names use a strict allowlist (spoken/pushed); model text naming a medication is dropped. Interaction flags are keyword matches → "check with your prescriber", never advice.
- **Nudges:** prep/dinner reminders go through the normal reminder pipeline (generic `reminder` speaker route, voice + phone), ids prefixed `mp-`; Done taps do **not** fire the selfcare bridge (no cook event until Phase 4). No weekly fridge-check push yet. Accept moves "already have" items into the pantry instead of the grocery list.
- **Metrics:** `bgw_meal_plan_generated_total{result}` (`ok|retry_ok|invalid|llm_error|asleep|error|interrupted`), `bgw_meal_plan_latency_seconds`, `bgw_meal_plan_swap_total{result}`, `bgw_meal_plan_swap_latency_seconds`; planner calls in `bgw_llm_calls_total{purpose=meal_plan|meal_plan_swap}`.
- **Known limits:** `recipes` never pruned; swaps/replacements don't remove previously added grocery items.

### Phase 3 — Receipts
- [ ] Receipt prompt + parsing, `pantry_aliases` learning, dedup guard
- [ ] Telegram photo caption routing (`receipt`, `fridge`)

### Phase 4 — Consumption tracking
- [ ] `prep_batches`; cook events (`meal_plan cooked`, push Done) with consume semantics
- [ ] Portion eaten (`meal_plan ate`) → decrement + `log_meal`
- [ ] Decay job + morning-briefing "use today" line
- [ ] Weekly reconcile scan (diff proposals) + Saturday nudge
- [ ] Grafana panels for consumption metrics (row exists since Phase 2)

## Open questions (decide at build time)

1. Breakfasts: plan them, or treat as staples-only (oats/yogurt/eggs tracked as levels)?
2. Recipe source: LLM-generated only, or also let the user paste/import favourite recipes (URL → text → recipe)?
3. Keep receipt photos long-term (backup + Paperless handoff) or delete after commit?
4. Nutrition estimates: LLM ballpark is fine for steering; is a real nutrient DB (USDA FoodData Central, offline) wanted later?
