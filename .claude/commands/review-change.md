---
description: Run code-reviewer, security, and prod-support in parallel on the current working-tree changes. Adds frontend and hacker agents when the diff warrants them.
---

Review the current change set across all relevant angles.

**Step 1 — Figure out what changed.** Run `git status` and `git diff` (both staged and unstaged). Build a short mental list:
- Which files changed
- Which directories those files live in (`orchestrator/`, `frontend/`, `scripts/`, docs, etc.)
- Whether any routes, auth paths, tool schemas, or input-handling code were touched
- Whether anything under `frontend/` changed

**Step 2 — Decide which agents to run.**
- Always: `code-reviewer`, `security`, `prod-support`
- Add `frontend` if `frontend/` was touched or a backend route shape changed that the dashboard consumes
- Add `hacker` ONLY if the orchestrator is running on `localhost:8888` (check with `curl -sf http://localhost:8888/health`) AND a route / auth path / tool schema / input handler changed. If it should run but the server is down, say so and skip it.

**Step 3 — Invoke them in parallel.** Send one message with multiple Agent tool calls. Each prompt must be self-contained:
- Name the specific files and line ranges that changed
- Paste or summarize the actual diff for those lines
- State what the change is trying to accomplish
- Ask for the agent's standard output format

Do NOT say "review my changes" — the subagent has no access to this conversation and cannot see the diff unless you include it.

**Step 4 — Synthesize.** When all agents return, produce a single consolidated report:

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

**Step 5 — Act.** Fix every BLOCKING finding immediately, then re-run only the agents whose findings you touched. Surface NON-BLOCKING findings to the user with a one-line recommendation each and let them choose.
