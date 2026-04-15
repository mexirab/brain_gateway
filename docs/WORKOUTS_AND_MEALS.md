# Workouts & Meals

Adaptive gym workout generator and calorie-only meal logging with optional photo-based estimation.

## Workout Generator

### Adaptive split logic

The generator is recency-aware and chooses a plan based on recent session history:

| Sessions in last N days | Plan type |
|-------------------------|-----------|
| < 1 in last 4 days | `full_body` |
| 1 in last 3 days | `full_body_complement` — skewed toward muscles undertrained in the prior session |
| 2+ in last 4 days | `push` / `pull` / `legs` — whichever split best complements recent sessions |

Full-body is the floor. The push/pull/legs split is a reward for consistency. The generator is idempotent: calling `generate_workout` (or `POST /api/workouts/generate`) when a plan already exists for today returns the existing plan rather than creating a duplicate.

### Exercise catalog

~52 exercises seeded into the `exercises` table by `exercises_seed.py` on startup (idempotent). Each exercise has a `movement_pattern`: `squat`, `hinge`, `push`, `pull`, `lunge`, `core`, or `isolation`.

### Tool behavior

`generate_workout` returns the full plan text so the model retains it in context for follow-up questions (e.g., "swap squats for leg press"). The tool description instructs the model NOT to read the plan aloud — the user is at the gym.

### Database tables

- `exercises` — catalog (id, name, movement_pattern, equipment, notes)
- `workouts` — session header (id, date, split_type, ended_at)
- `workout_sets` — logged sets (id, workout_id, exercise_id, set_number, weight_lbs, reps, logged_at)

All weights are stored and returned in **lbs**.

## Meal Logging

### v1 scope

Calories-only. No protein/carb/fat breakdown in v1. Meal logging is independent of `selfcare_log` — `selfcare_log` is nudge tracking (did you eat?), meal logging is calorie accounting.

### Photo estimation flow

1. User uploads a photo to `POST /api/meals/photo` (multipart `file` field).
2. Photo is saved to `MEAL_PHOTOS_DIR` with a uuid4 filename. Extension allowlist enforced: `jpg`, `jpeg`, `png`, `gif`, `webp`.
3. Image is sent to Qwen3-VL-8B (Saturn, port 8010) with a strict-JSON prompt.
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

Vision model config (shared with `analyze_image`): see `docs/ENV_VARS.md` → Vision section.

## Key Files

| File | Purpose |
|------|---------|
| `orchestrator/workout_manager.py` | Adaptive plan generation, set logging, modify_workout, get_history, get_exercise_prs |
| `orchestrator/meal_manager.py` | Meal CRUD, photo save, calorie estimation via Qwen3-VL-8B |
| `orchestrator/exercises_seed.py` | Static exercise catalog, idempotent seed on startup |
| `orchestrator/routes_workout.py` | Workout API routes |
| `orchestrator/routes_meals.py` | Meal API routes (photo upload + serve included) |
| `orchestrator/tests/test_workout_manager.py` | Workout manager unit tests |
| `orchestrator/tests/test_meal_manager.py` | Meal manager unit tests |
| `orchestrator/tests/test_state_store_workouts.py` | state_store workout/meal table tests |
| `frontend/src/app/(private)/workouts/page.tsx` | Workouts dashboard page |
| `frontend/src/app/(private)/meals/page.tsx` | Meals dashboard page |

## API Quick Reference

See `TECHNICAL_REFERENCE.md` → Workouts and Meals sections for full endpoint list and example payloads.
