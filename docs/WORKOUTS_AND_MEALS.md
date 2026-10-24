# Workouts & Meals

Gym workout generator (weekly program + per-location equipment) and calorie-only meal logging with optional photo-based estimation.

Both features are gated: `WORKOUTS_ENABLED` / `MEALS_ENABLED` (default `false` — tools hidden, `/api/workouts/*` / `/api/meals/*` 404, dashboard links + the Settings → Workouts tab hidden). To turn workouts on: set `WORKOUTS_ENABLED=true` in `.env`, then `docker compose up -d --force-recreate orchestrator`. See `docs/ENV_VARS.md`.

## Workout Generator

Two ways a day gets planned (`workout_manager.generate_workout(location_id)`):

- **Program** — when a weekly program is active (`workout_setup`). Follows a fixed split.
- **No program** — the original adaptive day-by-day planner.

Either way, a **workout location** restricts exercises to the gear that place has and caps loads. No location → whole catalog, no caps.

### Weekly program

Set via the `workout_setup` tool (`action=set_program`), `PUT /api/workouts/program`, or Settings → Workouts. Fields: `goal` (`strength` | `muscle` | `general`), `days_per_week` 2–6, `session_minutes` 20–120, `deload_every_weeks` (default 6; `0` = off; 3–12; omitted/`null` keeps the current value; non-integer floats rejected).

| Days/week | Split |
|-----------|-------|
| 2 | Full Body A / B |
| 3 | Full Body A / B / C |
| 4 | Upper / Lower / Upper / Lower |
| 5 | Upper / Lower / Push / Pull / Legs |
| 6 | Push / Pull / Legs / Push / Pull / Legs |

- **Exercises per session** = `round(session_minutes / 9)`, clamped 3–8. Each day template lists slots (movement patterns) in priority order; a short session takes the first N, a long one pads with isolation slots.
- **Rep schemes** (sets × reps, compound / isolation): `general` 3×8 / 3×12 (also the no-program default), `strength` 4×5 / 3×10, `muscle` 3×10 / 3×12.
- **Next day** = the day after the last program day with ≥1 completed set. A generated-but-untouched (skipped) day is offered again. Not calendar-based.
- **Changing goal, minutes or deload cadence** updates the active program in place — the split position is kept. **Changing days/week** starts a new program at day 1 (old row kept inactive). Identical values → no write (`unchanged`).
- **Training blocks (sticky days):** a program day reuses the exercises from the last trained workout for the same day — same location preferred, then any — within the current block. A block ends at a deload week, so exercises rotate after each deload; during the deload week the block's lifts are kept. Reasoning adds "Same lifts as last time so you can progress; they rotate after the next deload week." The no-program path is unaffected (still varied day to day).
- Weight/reps targets: see **Progression** below.

### Progression

Deterministic progressive overload per exercise (`workout_manager._plan_target`), applied to both the program and no-program paths. History = the exercise's last 8 non-deload sessions, each reduced by `_session_summary` to a working weight + outcome.

**Loaded lifts:**

| Last session | Next target | `progression` |
|--------------|-------------|---------------|
| No history | Scheme reps, no weight (user picks; a single owned kettlebell is used) | `new` |
| Every planned set at the weight + target reps | Weight up: +10 lb lower-body barbell/machine compounds, +5 lb otherwise, the location's `dumbbell_step_lbs` for dumbbells, +9 lb (~4 kg) for kettlebells with no bell list | `up` |
| Missed a set | Same weight | `hold` |
| Missed twice in a row at the same weight | −10% (rounded to 5 lb) | `stall_deload` |
| Hit, but a set logged at RPE 10 ("grinder") | Same weight | `hold` |
| ≥21 days since the lift was last done (deload sessions count) | ~90% of the reference weight | `comeback` |
| Goal change moved the rep range (>1 rep) | Re-based via Epley: more reps → lighter; fewer reps → same weight | `hold` |

- Extra heavier sets beyond the plan don't count toward the hit/miss decision or the working weight.
- A 0 lb log on a loaded lift is ignored (session dropped if that's all there is).
- If the increment doesn't fit the gear's step (fitted weight ≤ last weight), the target becomes +1 rep instead (`reps`).

**Rep-mode progression** (reps carried forward instead of the scheme):

| Case | Rule |
|------|------|
| Bodyweight | +1 rep per session, capped at 20 → note "try a harder variation" |
| Owned kettlebells (`kettlebell_weights_lbs`) | +1 rep until 4 over the scheme, then the next heavier owned bell at scheme reps |
| At the location's gear ceiling (dumbbell max / heaviest bell) | +1 rep vs the last session done at that ceiling (same reps if that one was missed); first time capped → scheme +2 reps. `progression="capped"` |

A session that was capped at a place whose gear tops out lower than today's (home dumbbells vs a gym rack) is not used as the reference.

Each planned exercise carries `progression` (`new|up|reps|hold|stall_deload|comeback|capped|deload_week`) and a short `note` ("+5 lb from last time", "same as last time — finish every set to move up"; none for `new`). The note is stored on set 1 in `workout_sets.notes` and surfaces as `exercises[].note` in `GET /api/workouts/today` / `workout_status`, under the exercise name on `/workouts`, and in the `generate_workout` tool text. Added exercises (`modify_workout`) get the same targeting.

### Deload weeks

Program-only. The cycle counts **trained** program sessions (≥1 completed set), not calendar weeks: after `days_per_week × deload_every_weeks` trained sessions, the next `days_per_week` sessions are a deload (`workout_manager.deload_status`).

- Deload session: sets × 0.6 rounded (min 2), scheme reps, ~85% of the reference weight fit to the gear; `progression="deload_week"`, note "deload week — lighter on purpose"; reasoning "Deload week: fewer sets, lighter weights, on purpose."
- Marked by `workouts.is_deload = 1`. Deload sessions are excluded from progression history but count toward comeback timing and mark the block boundary.
- Changing the cadence in place re-reads the position against the new cycle length (the next deload can move earlier or later).
- `deload_every_weeks = 0` → no scheduled deloads; blocks never end, so program days keep their lifts.

### No-program path (adaptive)

Logic unchanged. The pool now includes the new kettlebell/bodyweight exercises.

| Sessions in last N days | Plan type |
|-------------------------|-----------|
| < 1 in last 4 days | `full_body` |
| 1 in last 3 days | `full_body_complement` — skewed toward muscles undertrained in the prior session |
| 2+ in last 4 days | `push` / `pull` / `legs` — whichever split best complements recent sessions |

Full-body is the floor; the split is a reward for consistency.

Idempotent for both paths: calling `generate_workout` (or `POST /api/workouts/generate`) when today's plan exists returns it instead of creating a duplicate (`already_exists: true`; `location_mismatch: true` if the dashboard picked a different location than today's plan uses — delete today's workout to re-plan).

### Workout locations

Each location (a gym, home) has a name, an equipment list and optional limits. An exercise is available at a location when its `requires` ⊆ the location's equipment.

**Equipment vocabulary** (12 tokens, `exercises_seed.EQUIPMENT_TOKENS`): `barbell`, `squat_rack`, `bench`, `trap_bar`, `dumbbells`, `kettlebell`, `cable`, `machines`, `leg_press`, `pull_up_bar`, `dip_station`, `ab_wheel`.

**Templates** (`LOCATION_TEMPLATES`, starting point for a new location): `commercial_gym` (all 12, default), `home_basic` (`dumbbells`, `kettlebell`), `empty` (bodyweight only). Edit the differences with add/remove equipment.

**Limits** (lb): `dumbbell_max_lbs` (1–300), `dumbbell_step_lbs` (0.5–50), `kettlebell_weights_lbs` (list of owned bells, ≤12). Patches merge; `null` clears a key.

**Load caps:**

| Gear | Rule |
|------|------|
| Dumbbell | Clamp to `dumbbell_max_lbs`, then floor to a multiple of `dumbbell_step_lbs` (min one step) |
| Kettlebell | Snap down to the heaviest owned bell ≤ target (lightest bell if none fit). Single bell + no history → that bell is the target |
| Capped load | +2 reps (max 20) and the note "maxed out on this gear — go for extra reps" |

Limits: ≤20 locations, names ≤60 chars, unique case-insensitively, control/invisible chars stripped.

**Choosing the location:**

| Path | Behavior |
|------|----------|
| `generate_workout` tool, `location` named | Exact (case/punctuation-insensitive) match, else a unique containment match ("downtown" → "Downtown Gym", "at home" → "Home"). Unknown → not generated, known locations listed |
| Tool, none named, 1 location | Uses it |
| Tool, none named, ≥2 locations | Not generated — the model asks "where are you training today" (with "last time: X") and calls again. Metric `result="ask_location"` |
| `POST /api/workouts/generate`, no `location_id` | Only location, else last-used, else none (whole catalog) |

`workout_setup` `remove_location` requires the exact name — no fuzzy match.

### Exercise picker

`_pick_for_slots` fills one exercise per slot:

1. **Pass 1** — every slot tries its own movement pattern first (so an early slot's fallback can't steal a later slot's only option).
2. **Pass 2** — still-empty slots try `_PATTERN_FALLBACK` (e.g. vertical pull ↔ row, squat ↔ lunge, core ↔ carry; hinge and isolation have none). At a location this adds a note ("No vertical pull option here — used a row instead."); a slot with nothing adds "Skipped … — nothing for it here."
- Isolation slots on a focused day (Upper/Lower/Push/Pull/Legs) only pick exercises whose primary muscle is in that day's focus.
- Selection prefers the freshest muscles (recent-set bias), avoids repeating a primary muscle, random within the best bucket.
- `FALLBACK_VARIANTS` (floor press / floor pullover / floor skull crusher variants) are only picked when nothing else fits the slot, so a full gym never gets them.
- Nothing fits at all → `{ok: false}` "Add equipment to that location first" (`result="no_exercises"`).

### Exercise catalog

~72 exercises in `exercises_seed.py`. Movement patterns: `squat`, `hinge`, `lunge`, `push_horizontal`, `push_vertical`, `pull_horizontal`, `pull_vertical`, `core`, `carry`, `isolation`. `requires` is derived from `equipment` (`dumbbell` → `dumbbells`, `machine` → `machines`, `bodyweight` → none, …) unless spelled out (bench, rack, bar).

**The seed is an UPSERT and the only writer** — catalog fixes reach existing DBs, and manual edits to the `exercises` table are reverted at the next startup. Unchanged rows are skipped. First startup after the program-builder deploy logs (expected):

- `[STATE] Exercise catalog: N rows inserted or updated from the seed`
- 6 × `[STATE] Migrated <table>: added <col>` (`exercises.requires`, `workouts.location_id`, `workouts.program_id`, `workouts.program_day`, `workouts.is_deload`, `workout_programs.deload_every_weeks`)

Rolling back to `main` with the new columns present is safe (verified).

### Tool behavior

- `generate_workout` (`location` optional) returns the full plan text (location, program day, reasoning incl. deload week, per-exercise sets×reps @ weight + progression note) so the model keeps it in context for follow-ups. The description tells the model NOT to read the plan aloud — the user is at the gym.
- `workout_setup` — view/change the program and locations (`get`, `set_program`, `clear_program`, `add_location`, `update_location`, `remove_location`). `set_program` takes optional `deload_every_weeks`; its reply states the cadence; `get` includes `deload_every_weeks` + `next_is_deload`. Schema: `TECHNICAL_REFERENCE.md` → workout_setup.
- `modify_workout` — validates adds BEFORE removing: catalog misses, duplicates already in today's plan, and exercises needing gear the workout's location lacks come back in `skipped_exercises` (`reason` + up to 2 same-pattern `alternatives`). If adds were requested and none fit, the removal is held (`removal_held: true`) — a failed swap never just deletes the original. Added exercises use the program's rep scheme, progression, the location's load caps, and the deload rules if today is a deload session.
- `workout_status` — actions `today`, `history` (`days`), `how_to` (`exercise`): up to 8 steps (≤500 chars each) + primary muscles from the how-to library; no guide → `steps: null` + `video_url` + a note telling the model to say so and not improvise form cues.

`orchestrator/workout_setup.py` holds all validation + CRUD for programs and locations, shared by the REST routes and the tool. Every write is audited via `config_writer.log_config_change` (panel `workouts`; `changed_by` `user` or `jess`).

### Movement nudge bridge

`log_set` calls `selfcare_manager.record_movement_logged(f"set:{exercise_name}")` after persisting the set, resetting both `last_movement_nudge` and `sitting_since`. This prevents the "you've been sitting for 274 minutes" nudge from firing while the user is actively at the gym. Wrapped in try/except with `logger.error(exc_info=True)`; never blocks the set write.

### Database tables

- `exercises` — catalog (name PK, primary_muscle, secondary_muscles, equipment, is_compound, movement_pattern, `requires` JSON list)
- `workouts` — session header (+ nullable `location_id`, `program_id`, `program_day`; `is_deload` 0/1)
- `workout_sets` — planned + logged sets (`notes` on set 1 holds the progression note)
- `workout_locations` — name, equipment JSON, limits JSON, `last_used_at`
- `workout_programs` — goal, days_per_week, session_minutes, `deload_every_weeks` (default 6), `active`

Column detail: `TECHNICAL_REFERENCE.md` → Workout program builder tables. All weights are stored and returned in **lbs**.

### Exercise how-to library (2026-10-11)

Start/end photos + numbered steps per exercise, from [free-exercise-db](https://github.com/yuhonas/free-exercise-db) (The Unlicense — public domain), pinned to commit `f00c92c7` (`exercise_library.SOURCE_SHA`). **Not in the repo** — download onto the data volume:

```bash
docker exec brain-orchestrator python -m orchestrator.exercise_library fetch          # ~100 MB, 876 exercises, 1746 JPEGs, ~30 s
docker exec brain-orchestrator python -m orchestrator.exercise_library fetch --force  # re-download
docker exec brain-orchestrator python -m orchestrator.exercise_library check          # seed-catalog coverage
```

- Layout under `EXERCISE_LIBRARY_PATH`: `library.json` (trimmed entries), `meta.json` (`source_sha`, `fetched_at`, `count`), `images/<id>/<n>.jpg`.
- Coverage: 66/72 seed exercises have photos; the other 6 are deliberately mapped to `None` and get a YouTube-search link instead.
- Lookup: hand-checked `_ALIASES` (seed name → library id; `None` = no faithful match), then exact normalized-name match (a future catalog addition named like a library entry gets photos automatically). No fuzzy matching — a wrong-movement photo is worse than none.
- Fetch safety: streamed with size caps (20 MB JSON, 3 MB/photo), JPEG magic check, no redirects, staged in `exercise_library.new` then swapped in via `os.replace` (previous library moved to `.old`, restored if the swap fails, deleted after success). Network/JSON error or >5% failed photos → existing library untouched, exit 1. Smaller partial failure → installed, exit 1.
- Runtime loader is mtime-cached; missing library = feature dark (`library_available: false`); corrupt file = feature dark, logged once.
- Excluded from `scripts/backup_state.py` (reconstructable) — see `docs/BACKUP.md`.

### Metrics

`bgw_exercise_info_lookups_total{result}` — `found` | `not_found` (no faithful match → video link) | `library_missing` (not fetched). Labels pre-initialized.

`bgw_workout_generated_total{result}` — `ok` | `no_exercises` (location gear too thin / empty catalog) | `bad_location` (unknown `location_id`) | `ask_location` (tool asked where). Labels pre-initialized at 0.

`bgw_workout_progression_total{progression}` — one per planned exercise, labelled with the decision (8 labels, pre-initialized). INFO log `[WORKOUT] <exercise>: stall_deload|comeback → <w> lb` for those two decisions only; the `[WORKOUT] Generated …` line includes `deload=`.

### Dashboard UI

`/workouts` page:

- **No workout yet:** "Next in your program: <day> (split)" line when a program is set (adds ", deload week, lighter on purpose" when the next session is a deload); a "Where are you training?" picker when locations exist (preselects the only or last-used location; "Not sure (full gym)" sends `location_id: null`, which plans from the whole catalog — omitting `location_id` is what falls back to the only/last-used location). Generate shows a message on `location_mismatch`.
- Today's header shows "· at <location>" and "· deload week" on deload sessions.
- Each exercise shows its progression note under the name.
- Exercise names are buttons (dotted underline + info icon) opening `components/workouts/ExerciseInfoModal.tsx` (`GET /api/workouts/exercise-info`): photos flip every 1 s (pause/play; starts paused under `prefers-reduced-motion`), numbered steps, "Works:" muscles, "Watch a video" link; error state with retry.
- Add-exercise picker lists only exercises today's location supports (`GET /api/workouts/exercises?location_id=`); a skipped add shows the reason + alternatives.
- Delete today's in-progress workout (trash icon next to "End session"); remove an exercise (X on its header → `PATCH /api/workouts/{id}` `remove_exercises`); delete past workouts from history.

**Settings → Workouts** (`frontend/src/components/settings/WorkoutsPanel.tsx`, seventh `/settings` panel, hidden when `workouts_enabled` is false via `GET /api/config/features`): program editor (goal, days/week, minutes, "Lighter deload week" select: Never / after every 4, 5, 6, 8 weeks of training — a chat-set value like 10 stays selectable — with help text; summary shows "deload week in N sessions" / "this week is a deload week") + location list (add from template, equipment toggles, dumbbell max/step, kettlebell weights, delete; limits for gear toggled off are cleared on save; rename is chat/API only).

The `modify_workout` / `workout_setup` tools are the voice/chat path to the same backend.

### Known limits

- No Grafana Workouts panels yet (metrics exist, incl. `bgw_exercise_info_lookups_total`; dashgen has no Workouts row).
- Planned-set inserts are one commit per set (pre-existing).

## Meal Logging

### v1 scope

Calories-only. No protein/carb/fat breakdown in v1. Meal logging is independent of `selfcare_log` — `selfcare_log` is nudge tracking (did you eat?), meal logging is calorie accounting.

### Photo estimation flow

1. User uploads a photo to `POST /api/meals/photo` (multipart `file` field).
2. Photo is saved to `MEAL_PHOTOS_DIR` with a uuid4 filename. Extension allowlist enforced: `jpg`, `jpeg`, `png`, `gif`, `webp`.
3. Image is sent to the vision model at `VISION_MODEL_URL` with a strict-JSON prompt. Live deployment: the primary brain on Helios (`http://10.0.0.195:8080/v1`; since 2026-10-24 the llama.cpp Qwen3.8-Flash-Next GGUF + `mmproj-F16`, `VISION_MODEL_NAME=qwen3.8-flash-next-q3kxl`; 2026-10-02 → 2026-10-24 the Qwen3.8-27B TURBO Q6_K GGUF, verified on a 3024×4032 photo in 5.7 s; 2026-09-28 → 2026-10-02 the vLLM NVFP4 brain at 2–5 s). It replaced Qwen3-VL-8B on Saturn (:8010). The brain runs `--parallel 1`, so a photo estimate queues behind any in-flight chat turn. Photo estimation therefore needs Helios awake (power-tiered), same as chat.
4. Response `{calories_estimate, description, confidence}` is returned to the caller.
5. User confirms in the frontend UI before the meal is saved (or the tool passes `auto_log=true` to skip confirmation).

### Security constraints (locked by tests)

- `photo_path` in POST/PATCH `/api/meals/` body is silently dropped — only the upload route may set it.
- `update_meal` allowlist excludes `photo_path`.
- `delete_meal` will not `os.remove` paths outside `MEAL_PHOTOS_DIR`.
- `save_photo_bytes` forces the extension into `{jpg, jpeg, png, gif, webp}` regardless of the uploaded filename.
- `days` query param on history endpoints is clamped 1–365.
- All numeric inputs (calories, weight_lbs, reps) are range-validated.

## Config

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEAL_PHOTOS_DIR` | `/app/data/meal_photos` | Photo storage directory. Created on startup if absent. |
| `EXERCISE_LIBRARY_PATH` | `/app/data/exercise_library` | Exercise how-to library (filled by `python -m orchestrator.exercise_library fetch`). Empty dir = feature dark. |

Vision model config (shared with `analyze_image`): see `docs/ENV_VARS.md` → Vision section. Rollback to Saturn: restore the two `VISION_*` lines from Jupiter `.env.bak-vision-saturn`.

## Key Files

| File | Purpose |
|------|---------|
| `orchestrator/workout_manager.py` | Plan generation (program split or adaptive), training blocks, progression (`_plan_target`), deload cycle (`deload_status`), equipment-filtered picker, load caps, set logging, modify_workout, get_history, `get_program_overview` |
| `orchestrator/workout_setup.py` | Program + location validation and CRUD shared by routes and the `workout_setup` tool; audited (panel `workouts`) |
| `orchestrator/meal_manager.py` | Meal CRUD, photo save, calorie estimation via the vision model (`VISION_*`) |
| `orchestrator/exercises_seed.py` | ~72-entry catalog (upserted at startup), `EQUIPMENT_TOKENS`, `LOCATION_TEMPLATES`, `FALLBACK_VARIANTS`, `exercise_requires()` |
| `orchestrator/exercise_library.py` | How-to library: pinned free-exercise-db fetch CLI (`fetch`/`check`), `_ALIASES`, `get_info()`, `image_file()` |
| `orchestrator/routes_workout.py` | Workout API routes (incl. `/api/workouts/program`, `/api/workouts/locations`, `/api/workouts/exercise-info`, `/api/workouts/exercise-library/…`) |
| `orchestrator/routes_meals.py` | Meal API routes (photo upload + serve included) |
| `orchestrator/tests/test_workout_manager.py` | Workout manager unit tests |
| `orchestrator/tests/test_meal_manager.py` | Meal manager unit tests |
| `orchestrator/tests/test_state_store_workouts.py` | state_store workout/meal table tests (incl. locations, programs, seed upsert, migrations) |
| `orchestrator/tests/test_workout_setup.py` | workout_setup validation, CRUD, name resolution, audit + `generate_workout`/`workout_setup` tool handlers |
| `orchestrator/tests/test_exercise_library.py` | How-to library: lookup, fetch safety, routes, `workout_status how_to` (93 cases) |
| `orchestrator/tests/test_routes_workout_program.py` | Program/location routes + generate `location_id` handling |
| `frontend/src/app/(private)/workouts/page.tsx` | Workouts dashboard page (location picker, program line) |
| `frontend/src/components/workouts/ExerciseInfoModal.tsx` | Exercise how-to modal (photo flip, steps, muscles, video link) |
| `frontend/src/components/settings/WorkoutsPanel.tsx` | Settings → Workouts panel (program + locations) |
| `frontend/src/app/(private)/meals/page.tsx` | Meals dashboard page |

## API Quick Reference

See `TECHNICAL_REFERENCE.md` → Workouts and Meals sections for full endpoint list and example payloads.
