# Brain Gateway (Jess): briefing for Qwen Code

This file is short on purpose. `CLAUDE.md` is the full briefing, but it is
about 37K tokens, too big for your context. Read only the parts you need (see
"Getting more" below).

## What Jess is

Jess is a self-hosted personal assistant for ADHD support. The backend is a
FastAPI app, the orchestrator, in `orchestrator/`. It runs 24/7 in Docker on
the host Jupiter. The language model (the "brain") runs on a separate GPU box,
Helios, behind an OpenAI-compatible API. Helios is asleep most of the time.

The chat flow is the "v7 unified loop": one model does both the conversation
and the tool calls, in one agentic loop. A request comes in (dashboard, voice,
Telegram), the orchestrator builds one system prompt, sends it with the tool
schemas, runs every tool call the model makes, feeds the results back, and
repeats until the model answers in plain text. There is no second model and no
delegation. Tool results are capped at 8000 chars each.

## Load-bearing files

- `orchestrator/orchestrator.py`: FastAPI app, `/v1/chat/completions`, startup/shutdown, scheduler job registration.
- `orchestrator/unified_loop.py`: the agentic loop (`run_unified_tool_loop`), tool-result cap, markup neutralizing, per-round call limit.
- `orchestrator/cloud_brain.py`: model routing and fallback, client-history cleanup, streaming to the client.
- `orchestrator/prompt_builder.py`: builds the system prompt (`get_unified_system_prompt`, `DYNAMIC_CONTEXT_MARKER`).
- `orchestrator/tool_handlers.py`: almost every tool's `@register_tool` handler; many call into a `<feature>_manager.py`. Find one with `grep -rn 'register_tool("name")' orchestrator`.
- `orchestrator/tool_registry.py`: `register_tool` decorator and `execute_tool` dispatch, with per-tool metrics.
- `orchestrator/tool_definitions.py`: the tool JSON schemas the model sees; `get_all_tools()` hides gated tools.
- `orchestrator/config.py`: Pydantic `Settings`, every env var, validators that auto-disable a half-configured feature.
- `orchestrator/shared.py`: module-level state and the env-var aliases read from `orchestrator/config.py`.
- `orchestrator/state_store.py`: SQLite persistence (reminders, focus, chat, inbox and more).
- `orchestrator/mempalace.py`: the memory system (one ChromaDB collection, wing/room routing).
- `orchestrator/metrics.py`: Prometheus metrics, all named `bgw_*`.
- `orchestrator/tests/`: pytest suite; `orchestrator/tests/conftest.py` sets test env vars and guards test-id length.
- `docker-compose.yml`: the service stack, configured by env vars.
- `frontend/`: the Next.js dashboard.

Most features follow the naming `<feature>_manager.py`, `routes_<feature>.py`
and `jobs_<feature>.py` in `orchestrator/`. Use `grep` to find a feature.

## Hard rules

Some are enforced by tests. The rest keep the owner's data and the live system safe.

1. **No personal content in logs.** Log ids, lengths, counts, categories and
   dict keys only. Never message text, queries, reminder or calendar text,
   meds, tool arguments or the user's name. A reviewed exception needs a
   `# log-safe: <reason>` comment on the line or the line above. Enforced by
   `orchestrator/tests/test_no_user_content_in_logs.py`.
2. **Prompt cache layout.** In `orchestrator/prompt_builder.py`, nothing that
   changes per turn (time, retrieved memory, tasks) may go above
   `DYNAMIC_CONTEXT_MARKER`. Never add a second system message: the model's
   chat template rejects it.
3. **Schemas match handlers.** A tool's schema in
   `orchestrator/tool_definitions.py` and its handler in
   `orchestrator/tool_handlers.py` change together: same name, same
   arguments.
4. **Env vars.** Every new env var is a field of `Settings` in
   `orchestrator/config.py`, exposed through an alias in
   `orchestrator/shared.py` (for example
   `RESEARCH_HELPER_ENABLED = settings.research_helper_enabled`), and listed
   in `docs/ENV_VARS.md`. Some older modules still read `os.environ`
   directly; don't copy that in new code.
5. **New feature flags default off** (`False`), so a fresh install doesn't
   change behaviour.
6. **Public repo.** No personal data anywhere: no real names, meds, schedules,
   addresses, calendar entries or tokens, in code, tests, fixtures or docs.
   Use made-up examples.
7. **Test node ids stay ≤300 chars.** `orchestrator/tests/conftest.py` fails
   collection otherwise. Give a long parametrize case a short id with
   `pytest.param(value, id="short-name")`.
8. **Don't hand-edit generated files.** Grafana dashboard JSON comes from
   `monitoring/grafana/dashgen/`. Theme outputs come from `theme/tokens.json`
   via `theme/build.py`.

## Running tests

Dependencies are already installed. There is no internet: never run
`pip install` or `npm install`. Run from the repo root (where this file is):

```bash
python -m pytest -p no:cacheprovider -q orchestrator/tests
```

One file, or one test:

```bash
python -m pytest -p no:cacheprovider -q orchestrator/tests/test_calculator.py
python -m pytest -p no:cacheprovider -q orchestrator/tests/test_calculator.py -k "some_name"
```

`pyproject.toml` already sets `asyncio_mode = "auto"`. Tests mock outside
services (HTTP, Home Assistant, the brain), so they need no network. One
exception: importing `orchestrator/shared.py` loads the embedding model from
the local Hugging Face cache. Write new tests in
`orchestrator/tests/test_<feature>.py` and mock HTTP with `respx`.

Lint, if `ruff` is installed: `ruff check orchestrator` and
`ruff format --check orchestrator`.

If the test command doesn't run, or collection fails (a missing module, or
the embedding model won't load), say so and stop. Don't install anything,
don't run `docker` and don't edit `orchestrator/shared.py` to get around it:
the owner fixes the setup.

## What you can't reach

There is no network. You can't reach the internet, Helios (the brain), Home
Assistant, Loki, Prometheus, Grafana or any running Jess service. Don't try:
no curl, no `docker`, no ssh, no live API calls. Even where one of these would
work (the owner also runs you on their host), don't use it. Work from the
code, the docs and the tests.

Don't read secrets: no `.env` files, nothing under credentials/, and nothing
outside the repo folder, including `~/.qwen`.

## Getting more

`CLAUDE.md` is in the repo. Don't read it whole. Find the part you need:

```bash
grep -no '^## .*\|^- \*\*[^*]*\*\*' CLAUDE.md
```

That prints its section headings and the titles of its `- **Topic**` notes,
with line numbers (about 3.5K chars). Each note is ONE long line, up to ~8K
chars, so read one note at a time (`sed -n 373p CLAUDE.md`) or one short
section (`sed -n 238,284p CLAUDE.md`), using the line numbers from the grep.
Its "Detailed Docs (router)" table says which file in `docs/` covers which
subsystem.

`TECHNICAL_REFERENCE.md` (API and tool-schema details, ~39K tokens) and
`docs/ENV_VARS.md` (~21K tokens) are too big to read whole as well. List their
headings with `grep -n '^#' TECHNICAL_REFERENCE.md`, then read one section
with `sed -n`.

Skip CLAUDE.md's "Post-change review workflow". It names Claude Code
subagents you don't have, and the owner runs it after you hand back.

## Handing back

Never run `git commit`, `git push` or anything that deploys. Deploying means
merging to `main`, and CI does that after the owner reviews. Use git only to
look: `git status`, `git diff`, `git log`, `git show`. Never run `git stash`,
`git reset`, `git checkout`, `git clean` or `git config`, and never write
inside the .git folder. Leave your changes in the working tree. When done, say
in a few lines which files you changed, which tests you ran and what they
printed.
