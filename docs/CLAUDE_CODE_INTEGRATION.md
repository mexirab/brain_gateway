# Claude Code ↔ Jess Integration

Jess can see what Claude Code has been working on, via the `check_claude_activity` tool (gated by `JESS_ADVANCED`). Data flow is one-way (Claude Code → Jess) and entirely local. No Claude.ai / Anthropic API involvement.

## Two data sources

1. **Live session file** — `check_claude_activity` reads `~/.claude/projects/-opt-helios-gateway-mvp/*.jsonl` directly. No sync needed, always fresh. Works even without the Stop hook installed.
2. **SQLite rolling buffer** — a Claude Code `Stop` hook POSTs each completed turn to the orchestrator, which logs it to `brain_state.db`. Seven-day retention (cleaned up by the weekly DB maintenance job). Useful for Jess dashboards and for the `check_claude_activity` tool's `recent` action.

## Install the Stop hook

The hook is optional but recommended — it's what gives Jess proactive awareness of Claude Code work without having to read JSONL files.

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "/opt/gateway_mvp/scripts/claude_code_stop_hook.sh"
      }]
    }]
  }
}
```

**Hook environment:**
- `ORCHESTRATOR_URL` — default `http://localhost:8888`
- `API_TOKEN` — required if the orchestrator's `API_TOKEN` is set (which it is by default). Without it, the hook gets 401 and turns silently aren't logged.

## Jess tool: `check_claude_activity`

Jess can query Claude Code activity as part of normal conversation:

- `action: "recent"` — last N minutes of turns (default 120 min, configurable via `minutes_back`)
- `action: "current_session"` — all turns in the currently-active session file
- `action: "files_touched"` — files edited/created in recent turns, grouped by count

Example user-facing phrases:
- *"What has Claude Code been working on?"*
- *"Did Claude Code touch the palace code today?"*
- *"Show me the last hour of Claude Code activity"*

## Code agent (removed 2026-10-21)

The `code_agent` tool, which injected this activity into a local coder model's prompt, is removed (it gave a model `run_command`, `write_file` and git commits). Self-troubleshooting is the read-only self-audit / self-investigator (`jobs_self_audit.py`, `self_investigator.py`; see `jess-features/F-014-self-audit.md`).

## API endpoints

See [`TECHNICAL_REFERENCE.md`](../TECHNICAL_REFERENCE.md#claude-code-integration) for the `/api/claude_code/*` endpoint reference.

## Privacy

Everything is local. Claude Code → Jess is a LAN-only HTTP POST to the orchestrator on Jupiter. Neither the hook nor the tool sends anything to Anthropic or any third party. Session files live on your disk, and the rolling buffer lives in your local SQLite. This integration was explicitly designed to keep Jess's privacy guarantee intact — see the privacy intent memory for the decision rationale.
