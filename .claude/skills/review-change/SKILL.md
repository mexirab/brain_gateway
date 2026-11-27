---
name: review-change
description: The mandatory post-change pipeline for Brain Gateway. Run it after any code change, before reporting the task done, even when the change looks small. Phase 1 starts the code-reviewer, security and prod-support agents in parallel (plus frontend and hacker when the diff warrants them). Phase 2 writes and runs tests (unit-test skill), then updates the docs (docs-updater skill).
argument-hint: "[phase1]  (phase1 = review agents only, skip tests and docs)"
---

# Post-change review pipeline

Small changes are where slop builds up, so don't skip this. With the argument `phase1`, stop after Step 5.

## Phase 1 — parallel review (agents)

**Step 1 — See what changed.** Run `git status` and `git diff` (staged and unstaged; against `origin/main` if the work is committed). Note:
- which files changed, and in which areas (`orchestrator/`, `frontend/`, `scripts/`, `monitoring/`, docs)
- whether routes, auth paths, tool schemas or input handling changed (anything in `tool_handlers.py`, `tool_definitions.py`, `api_routes.py`, `routes_*.py`, `bearer_auth.py`)
- whether anything under `frontend/` changed

**Step 2 — Pick the agents.**
- Always: `code-reviewer`, `security`, `prod-support`.
- Add `frontend` if `frontend/` changed, or a backend route shape the dashboard consumes changed.
- Add `hacker` if a route / auth path / tool schema / input handler changed AND the orchestrator answers on `localhost:8888` (`curl -sf http://localhost:8888/health`). If it should run but the server is down, say so and skip it. The hacker attacks the LIVE deployment, which runs what's on `main`, not your branch: say which one it tested.

**Step 3 — Start them in parallel.** One message, several Agent calls. The agents don't see this conversation, so each prompt must stand alone:
- the files and line ranges that changed
- the diff for those lines (pasted, or summarized when it's very long)
- what the change is trying to do, and anything you already ruled out
- a request for the agent's standard output format

Never write "review my changes".

**Step 4 — Consolidate.** When all agents are back:

```
REVIEW SUMMARY
Files changed: <list>
Agents run: <list>

BLOCKING (must fix before ship):
- <agent>: <finding> — <file:line>

NON-BLOCKING (user decides):
- <agent>: <finding> — <file:line>

CLEAN:
- <what each agent signed off on>
```

Check a finding against the code before you act on it. Agents can be wrong, but never hide or soften one when you report to the user: say what it was and why you think it doesn't hold.

**Step 5 — Act.** Fix every HIGH severity or EXPLOITABLE finding, then re-run only the agents whose findings you touched. Give the user each medium/low finding with a one-line recommendation and let them choose.

## Phase 2 — in this session, in order

**Step 6 — Tests.** Use the `unit-test` skill for every new or changed function, tool handler, route or background job. If tests fail, fix the code (or the test, if the test was wrong) and re-run. Don't continue with failing tests.

**Step 7 — Docs.** Use the `docs-updater` skill last, so it records everything Phases 1 and 2 changed. Report CLAUDE.md's size before and after.
