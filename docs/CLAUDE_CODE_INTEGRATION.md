# Claude Code ↔ Jess Integration

Jess can see what Claude Code has been working on, for self-troubleshooting and giving the local `code_agent` awareness of in-flight work. Data flow is one-way (Claude Code → Jess) and entirely local. No Claude.ai / Anthropic API involvement.

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

## Code agent integration

`orchestrator/code_agent.py` automatically injects recent Claude Code activity (~180 min window, capped at 1200 chars) into its system prompt. This means when Jess delegates a coding task to the local Qwen3-Coder-Next 80B/3B MoE agent (Helios GPU0 + RAM-spilled experts, port 8082), the agent already knows what Claude Code has been working on and won't step on in-flight work.

The injection happens transparently before every `code_agent` tool call. If no activity is found (e.g., hook not installed and JSONL files empty), the system prompt just omits the section rather than failing.

**Preflight reachability check:** because the code model runs on Helios (port 8082), which is power-tiered and asleep most of the time, `code_agent` first probes `GET {CODE_AGENT_MODEL_URL}/models` with a short timeout (4s / 2s-connect) before entering the agent loop. If the endpoint is unreachable (connection error, timeout, or 5xx), it fails fast with an actionable "wake Helios" message and increments `bgw_code_agent_preflight_failures_total` — instead of hanging through up to `CODE_AGENT_MAX_ROUNDS` (10) rounds of 120s connect timeouts.

## API endpoints

See [`TECHNICAL_REFERENCE.md`](../TECHNICAL_REFERENCE.md#claude-code-integration) for the `/api/claude_code/*` endpoint reference.

## Privacy

Everything is local. Claude Code → Jess is a LAN-only HTTP POST to the orchestrator on Helios. Neither the hook nor the tool sends anything to Anthropic or any third party. Session files live on your disk, the rolling buffer lives in your local SQLite, and the `code_agent` runs on your local GPU. This integration was explicitly designed to keep Jess's privacy guarantee intact — see the privacy intent memory for the decision rationale.
