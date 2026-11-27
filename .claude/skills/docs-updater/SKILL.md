---
name: docs-updater
description: Update CLAUDE.md and the docs/ topic files after a change to Brain Gateway — new files, tools, env vars, endpoints, metrics, alerts, behavior, or removed functionality. Final step of the post-change pipeline (after review and unit tests). Holds the doc map and the rules that keep CLAUDE.md short.
---

# Update the docs after a change

You made the change, so you know what changed and why. Use that to decide what a future maintainer needs, then put each fact in exactly one place.

## CLAUDE.md is a router, not a record

CLAUDE.md loads in full into every session **and every subagent** (each post-change pipeline run starts several). Every byte in it is paid on every turn of every one of them. So:

1. **CLAUDE.md gets only what every task needs:** what a thing is, the rules a maintainer must not break, and a pointer to the doc with the detail (`Detail: docs/X.md → Section`).
2. **A Notes bullet is at most 3 lines.** If a feature needs more, the extra goes in its topic doc. A table cell (Tools, Key Files, Services) is one or two sentences.
3. **Never put these in CLAUDE.md:** measurements, incident stories, PR numbers, lists of metrics / alerts / env vars / test files, "before this, it was…" history, rollback steps. They go in the topic doc, `monitoring/README.md`, `docs/ENV_VARS.md`, `CHANGELOG.md`, or `docs/internal/HISTORY.md`.
4. **Edit, don't append.** When a bullet already covers the area, rewrite it so it stays as short or gets shorter. Delete what the change made untrue.
5. **Check the size.** Run `wc -c CLAUDE.md` before and after. If it grew by more than ~300 bytes, move the detail out until it doesn't.

## Doc map

| File | What goes there |
|------|-----------------|
| `CLAUDE.md` | Cluster, services, tools, key files, commands, maintainer rules + pointers (see above) |
| `CHANGELOG.md` | Every merged change: what, why, upgrade actions, files touched (newest first) |
| `TECHNICAL_REFERENCE.md` | API endpoints, request/response shapes, tool schemas + handler behavior, table schemas, loop internals |
| `docs/ENV_VARS.md` | Every env var: default, meaning, which subsystem reads it |
| `monitoring/README.md` | Prometheus scrape jobs, alerts + routing, Grafana rows, runbooks, host units that feed metrics |
| `COMMANDS.md` | Commands not in CLAUDE.md's Common Commands |
| `docs/DEV.md` | CI jobs, auto-deploy, test-writing rules |
| `docs/INSTALL.md`, `docs/UPGRADE.md`, `docs/HARDWARE.md`, `docs/BYO_MODEL.md` | Fresh installs, upgrade steps, hardware tiers, bring-your-own model |
| `docs/PRIVACY.md` | What leaves the house and what is stored |
| `docs/MEMPALACE.md` | Memory system, embeddings, auto-learn, recall |
| `docs/VOICE_AND_TTS.md` | Voice satellite, TTS servers, voice style, STT, dashboard voice |
| `docs/FRONTEND.md` | Dashboard pages, auth, kiosk session, inbox + Web Push, theme |
| `docs/CALENDAR.md` | Radicale + calendar sources |
| `docs/PANTRY.md`, `docs/WORKOUTS_AND_MEALS.md` | Pantry + meal plan; workouts + meal logging |
| `docs/SPREADSHEETS.md`, `docs/HA.md`, `docs/CODER.md`, `docs/BACKUP.md` | Grist; Home Assistant; coder box; backups |
| `docs/GOOGLE_INTEGRATIONS.md`, `docs/FOCUS_AND_PIHOLE.md`, `docs/MODE_ROUTER.md`, `docs/TRAINING_CORPUS.md`, `docs/CLAUDE_CODE_INTEGRATION.md` | Their named subsystems |
| `docs/JESS_QUICK_START.md`, `docs/JESS_REFERENCE_CARD.md` (+ `.html`) | User-facing: what to say to Jess |
| `docs/JESS_SELF_KNOWLEDGE.md` | What Jess knows about her own infrastructure (update when models, hardware or tools change) |
| `jess-features/F-0xx-*.md` + `README.md` | Feature specs and their rationale |
| `docs/internal/HISTORY.md` | Model eras, migrations and other "how we got here" records |
| `docs/internal/*.md` | Maintainer runbooks and plans (Helios, Flash-Next trial, container firewall, …) — see `docs/internal/README.md` |
| `ROADMAP.md` | Planned vs shipped |
| `.claude/skills/observability-rules/SKILL.md` | Logging + metrics rules (never-log list, adding metrics and alerts) |

`ARCHITECTURE.md` is superseded (it describes the removed v6 loop); don't add to it.

## What to check after each change

1. **New file** → Key Files row in CLAUDE.md only if it is load-bearing; otherwise nothing (the naming convention finds it).
2. **New tool** → Tools row in CLAUDE.md (one or two sentences) + schema and behavior in `TECHNICAL_REFERENCE.md` + `docs/JESS_QUICK_START.md` if users can ask for it.
3. **New env var** → `docs/ENV_VARS.md` (+ `.env.example` when it's user-facing). Not CLAUDE.md.
4. **New service / port** → Services row in CLAUDE.md.
5. **New endpoint** → `TECHNICAL_REFERENCE.md`.
6. **New metric / alert / Grafana row** → `monitoring/README.md`. Not CLAUDE.md.
7. **Changed behavior** → the topic doc; CLAUDE.md only if a rule changed.
8. **Removed functionality** → remove it everywhere (grep the name), don't leave stale references.
9. **Always** → a `CHANGELOG.md` entry.

## Rules

- Don't duplicate: a fact lives in one place; other places point to it.
- No speculative docs: only what exists and works.
- Preserve structure: update sections in place.
- The repo is public: no personal data (real meds, schedules, names, gym or calendar details).

## Output

```
DOCS UPDATED:
- file.md: what changed and why
CLAUDE.md: <before> → <after> bytes

NO UPDATES NEEDED:
- (if the change doesn't affect any documentation)
```
