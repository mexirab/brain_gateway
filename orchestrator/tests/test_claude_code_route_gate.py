"""JESS_ADVANCED gate on the Claude Code activity routes.

`/api/claude_code/turn` (Stop-hook ingest) and `/api/claude_code/recent` are
owner-specific dev surface. They must 404 on a clean shippable build
(JESS_ADVANCED=false), matching how the `check_claude_activity` tool that
consumes them is gated. Regression for the productization audit (2026-06-14).

Source: orchestrator/api_routes.py — log_claude_code_turn_endpoint /
get_recent_claude_code_activity.
"""

import json

import pytest

from orchestrator import api_routes, shared


async def _read_body(resp):
    return json.loads(bytes(resp.body).decode())


@pytest.mark.asyncio
async def test_recent_404_when_jess_advanced_off(monkeypatch):
    monkeypatch.setattr(shared, "JESS_ADVANCED", False)
    # Tripwire: the DB reader must NOT be reached when gated off.
    monkeypatch.setattr(
        "orchestrator.state_store.get_claude_code_turns",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate leaked")),
    )
    resp = await api_routes.get_recent_claude_code_activity()
    assert resp.status_code == 404
    assert (await _read_body(resp)).get("ok") is False


@pytest.mark.asyncio
async def test_recent_proceeds_when_jess_advanced_on(monkeypatch):
    monkeypatch.setattr(shared, "JESS_ADVANCED", True)
    monkeypatch.setattr("orchestrator.state_store.get_claude_code_turns", lambda *a, **k: [])
    resp = await api_routes.get_recent_claude_code_activity()
    assert resp.status_code == 200
    body = await _read_body(resp)
    assert body["count"] == 0 and body["turns"] == []
