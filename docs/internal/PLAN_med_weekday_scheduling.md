# Plan: Per-medication weekday scheduling + fix `update_medication` false-success

**Status:** NOT STARTED — implementation plan only. Check in on a branch; execute from a Jupiter session that can deploy + verify against the live orchestrator.

**Origin:** User asked Jess to "update my Vyvanse schedule" — the actual intent was **"stop reminding me to take it on weekends"** (a stimulant drug-holiday pattern). Jess replied that she'd done it, but nothing changed in the data. Root-cause investigation found two independent defects (below). This plan fixes both.

**Owner note:** Nadim has ADHD and prefers step-by-step with verification. Land this in small, verifiable slices; don't batch the whole thing into one untested commit.

---

## 1. Root cause (two defects)

### Defect A — no way to express "skip weekends" for a medication
- The med reminder loop `selfcare_manager._check_meds` (`orchestrator/selfcare_manager.py:425`) walks `daily.morning` / `daily.evening` and nudges "Hey, did you take your {med}?" whenever the med is in the current time window and unconfirmed today.
- The **only** day-of-week control anywhere is `is_quiet_day()` (`selfcare_manager.py:329`), a **global** switch that mutes *all* selfcare nudges on a given weekday — it cannot target one medication.
- The med schema is `name / dose / purpose / notes` (+ `when` for weekly/as_needed). There is **no per-med day field** and `_check_meds` has **no per-med weekday logic**.
- Net: "no Vyvanse on weekends" is simply not representable today.

### Defect B — `update_medication` reports success while changing nothing
- `handle_update_data` router (`orchestrator/data_manager.py:725`) calls `update_medication(name, dose, purpose, notes)` and **drops the `schedule` argument entirely** — even though the `update_data` tool schema advertises a `schedule` enum (`orchestrator/tool_definitions.py:145`) and the model sends it.
- `update_medication` (`data_manager.py:181`) **accepts** a `schedule` parameter but its body (the inner `update_med` closure, lines 187–202) **never reads it** — dead parameter. It also cannot move a med between schedule buckets.
- When the model calls `update_medication` with only a schedule/day intent, every writable field is `None`: the med is found, `updated_fields` stays `[]`, `save_medications()` writes the **unchanged** dict and returns `True`, and the function returns `"Updated Vyvanse: ."` — a hollow success. The model truthfully relays "done." **The tool lied to the model.**

---

## 2. Design decisions

1. **Storage:** add an optional `days` list to a med dict, e.g. `days: [mon, tue, wed, thu, fri]`.
   - Canonical values: lowercase 3-letter ISO weekday abbreviations `mon,tue,wed,thu,fri,sat,sun`.
   - **Absence of `days` = every day** (backward compatible; existing meds unaffected).
   - Store canonical `days`; do **not** store a `skip_weekends` bool (derive it at the tool layer).
2. **Tool ergonomics:** `update_data` / `add_medication` accept EITHER
   - an explicit `days` list, OR
   - a `skip_weekends: true` shorthand → normalizes to `[mon,tue,wed,thu,fri]`.
   Keep the LLM-facing surface small; normalize to `days` in the handler.
3. **Honoring the field:** `_check_meds` skips a med when `days` is present and today's weekday abbrev is not in it. Applies to morning AND evening buckets.
4. **Honesty fix (Defect B) is independent** and should land first — it prevents this whole class of silent false-success, not just the weekend case.
5. **No schema migration needed** — `days` is optional and additive; old YAML stays valid.

---

## 3. Implementation slices (land + verify in order)

### Slice 1 — Fix `update_medication` false-success (Defect B) — do first
`orchestrator/data_manager.py`
- In `update_medication` (line 181): make the inner `update_med` closure actually honor `schedule` (relocate the med between `daily.morning` / `daily.evening` / `weekly` / `as_needed` buckets — pop from current list, append to target, record `schedule=<x>` in `updated_fields`) and any new fields.
- If a med is **found but nothing changed** (`updated_fields == []`), return an honest message like `"No changes to {name} — nothing to update."` and **do not** call `save_medications` (avoid a pointless write + audit entry). Never return `"Updated {name}: ."`.
- In `handle_update_data` (line 725): forward `schedule=kwargs.get("schedule")` (and, after Slice 3, `days` / `skip_weekends`) into `update_medication`.
- Keep the "found but save failed" branch returning the existing failure string.

**Verify:** unit test — `update_medication("X")` with no changed fields returns the no-op message and does NOT write; a real dose change still returns `"Updated X: dose=..."`.

### Slice 2 — Honor `days` in the nudge loop (Defect A, read side)
`orchestrator/selfcare_manager.py`
- Add a helper, e.g. `_med_allowed_today(med: dict, now_tz: datetime) -> bool`:
  - `days = med.get("days")`; if falsy → `True`.
  - Normalize each entry (lowercase, first 3 chars) and compare to `now_tz.strftime("%a").lower()` (`mon`…`sun`).
  - Malformed/empty `days` → treat as "every day" (fail open — never silently drop a med reminder due to a typo). Log a one-line WARN.
- In `_check_meds` (line 425), inside both the morning loop (line 456) and evening loop (line 468), `continue` when `not _med_allowed_today(med, now_tz)`.
- Leave the generic-confirmation short-circuit logic intact.

**Verify:** unit test — a med with `days=[mon,tue,wed,thu,fri]` returns `None` from `_check_meds` on a Saturday/Sunday and a nudge on a weekday; a med with no `days` behaves exactly as today.

### Slice 3 — Expose `days` / `skip_weekends` to the model (Defect A, write side)
`orchestrator/tool_definitions.py`
- In the `update_data` schema (around line 144–169) add:
  - `days`: array of strings, enum `[mon,tue,wed,thu,fri,sat,sun]`, description "Days this medication is taken (omit = every day)."
  - `skip_weekends`: boolean, description "Shorthand: take only Mon–Fri (drug holiday on weekends)."
- Update the `update_data` description to mention you can set which days a med is taken.

`orchestrator/tool_handlers.py`
- In `tool_update_data` (line 465) pass `days=arguments.get("days")` and `skip_weekends=arguments.get("skip_weekends")` through to `handle_update_data`.

`orchestrator/data_manager.py`
- `handle_update_data`: forward the two new kwargs into `add_medication` and `update_medication`.
- Add a normalizer: if `skip_weekends` is truthy → `days = [mon,tue,wed,thu,fri]` (explicit `days` wins if both given). Validate/clip to the 7 canonical abbrevs; drop unknowns.
- `add_medication` (line 111) and `update_medication` (line 181): accept `days`, write it onto the med dict only when provided.

**Verify:** with the orchestrator running, `POST /v1/chat/completions` "stop reminding me to take Vyvanse on weekends" → confirm the tool call carries `skip_weekends`/`days`, the YAML gains `days: [mon,tue,wed,thu,fri]` on the Vyvanse entry, and a follow-up on a weekend produces no nudge (or fast-forward the clock in a unit test).

### Slice 4 — Reflect the schedule everywhere the meds are shown (REQUIRED, not optional)
The whole point: if the data says "weekends off" but the schedule views still show Vyvanse as a plain daily med, Jess will contradict herself (tell you weekends are off, then list it as daily, then maybe nudge because a downstream reader ignored `days`). Every place that reads/renders the med schedule MUST reflect `days`. `days` is the source of truth; these are all read-side consumers of it.

- `orchestrator/data_manager.py` `render_medications_compact` (line ~643) + `get_structured_facts_block` (line 678): append a compact schedule hint per med when `days` is present — `(Mon–Fri)`, `(weekends only)`, or `(Mon/Wed/Fri)`. This block is injected into EVERY system prompt and is what `get_data` returns, so it's the primary way the model answers "when do I take X?" correctly. Add a small `_fmt_days(days)` helper that collapses `[mon,tue,wed,thu,fri]`→"Mon–Fri", `[sat,sun]`→"weekends", else a slash list.
- `orchestrator/routes_config.py` `/personal-facts` (line 196): `days` already rides along inside the `daily` projection (allowlist is by top-level key `daily`), so the API returns it. **Update `frontend/src/app/(private)/personal-facts/page.tsx`** to render the days as a badge/subtitle next to each med so the peek page visibly shows the schedule — this is the user-facing "what does the system think my schedule is" view and must match what Jess says.
- **Consistency guard:** grep for every consumer of `daily.morning`/`daily.evening` med lists (at minimum `_generate_medications_md` line 223, `render_medications_compact`, `get_structured_facts_block`, `_check_meds`) and confirm each either honors `days` or is display-only and shows it. A reader that silently ignores `days` reintroduces the contradiction.
- Docs: `CLAUDE.md` (update_data / get_data tool rows + a note that meds carry an optional `days` field), `TECHNICAL_REFERENCE.md` (update_data tool schema + med YAML shape incl. `days`). No new env var, so `docs/ENV_VARS.md` is untouched.

---

## 4. Mandated post-change review pipeline (per CLAUDE.md)
After code lands, run:
- **Phase 1 (parallel):** `code-reviewer`, `security`, `prod-support`. Add `hacker` (touches `tool_handlers.py` + a tool schema — needs orchestrator on `localhost:8888`) and `frontend` (only if the `/personal-facts` page is edited).
- **Phase 2 (sequential):** `unit-test` (Slices 1–3 each get tests; run inside the `brain-orchestrator` container), then `docs-updater` last.

## 5. Test checklist
- [ ] `update_medication` no-op returns honest "nothing to update" and does not write.
- [ ] `update_medication` with `schedule=evening` moves the med from morning→evening bucket and reports it.
- [ ] `_check_meds` suppresses a `days=[mon..fri]` med on Sat/Sun, fires Mon–Fri.
- [ ] Med with no `days` behaves identically to pre-change (regression guard).
- [ ] Malformed `days` (typo, empty list) fails open (still reminds) + logs WARN.
- [ ] End-to-end: "no Vyvanse on weekends" chat → YAML updated → no weekend nudge.
- [ ] `get_data` / the injected prompt block render Vyvanse as `(Mon–Fri)` (not plain daily).
- [ ] `/personal-facts` API returns `days` AND the page renders it as a visible badge.
- [ ] Every `daily.*` med reader (grep audit) either honors or displays `days` — no consumer silently ignores it.

## 6. Retroactive data note
This change is **forward-only** — it won't edit the existing Vyvanse record. After deploy, either re-ask Jess ("stop reminding me to take Vyvanse on weekends") or hand-edit `medications.yaml` to add `days: [mon, tue, wed, thu, fri]` to the Vyvanse entry.

## 7. Files touched (summary)
| File | Change |
|------|--------|
| `orchestrator/data_manager.py` | Fix `update_medication` honesty + schedule move; add `days`/`skip_weekends` to add/update + router normalization; `_fmt_days` helper + schedule hint in `render_medications_compact` + `get_structured_facts_block` (+ `_generate_medications_md` if RAG docs enabled) |
| `orchestrator/selfcare_manager.py` | `_med_allowed_today` helper + weekday gate in `_check_meds` |
| `orchestrator/tool_definitions.py` | `days` + `skip_weekends` params on `update_data` |
| `orchestrator/tool_handlers.py` | Pass new args through `tool_update_data` |
| `frontend/src/app/(private)/personal-facts/page.tsx` | **Required:** render each med's `days` as a badge/subtitle so the peek page shows the schedule |
| `orchestrator/tests/test_data_manager.py`, `test_selfcare_*` | New tests per Slice |
| `CLAUDE.md`, `TECHNICAL_REFERENCE.md` | Doc the med `days` field + tool params |
