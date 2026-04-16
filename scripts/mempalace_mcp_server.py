#!/usr/bin/env python3
"""
MemPalace MCP Server for Claude Code.

Thin stdio MCP server that proxies to the orchestrator's /api/palace/* REST API.
Claude Code connects to this via `claude mcp add mempalace`.

Setup:
    pip install -r scripts/requirements-mcp.txt
    claude mcp add mempalace -- python3 scripts/mempalace_mcp_server.py

Environment:
    ORCHESTRATOR_URL  - Base URL (default: http://localhost:8888)
    API_TOKEN         - Bearer token for auth (optional)
"""

import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8888")
API_TOKEN = os.environ.get("API_TOKEN", "")

# Fail loud if the bearer token is missing. The orchestrator's
# BearerAuthMiddleware will return 401 for every request otherwise, and
# the user will see a confusing "silent failures" pattern instead of a
# clear misconfiguration error.
if not API_TOKEN:
    print(
        "FATAL: API_TOKEN environment variable is required but not set.\n"
        "       The MCP server needs a bearer token to reach the orchestrator.\n"
        "       Set it before launching Claude Code, e.g.:\n"
        "           export API_TOKEN=$(grep -E '^API_TOKEN=' /opt/gateway_mvp/.env | cut -d= -f2-)\n",
        file=sys.stderr,
    )
    sys.exit(1)

mcp = FastMCP("mempalace")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_TOKEN:
        h["Authorization"] = f"Bearer {API_TOKEN}"
    return h


def _url(path: str) -> str:
    return f"{ORCHESTRATOR_URL}{path}"


@mcp.tool()
async def palace_search(query: str, wing: str = "", room: str = "", n_results: int = 5) -> str:
    """Search the memory palace for knowledge by semantic similarity. Optionally filter by wing (jess, brain_gateway, conjure, infrastructure, personal) and room."""
    async with httpx.AsyncClient(timeout=30) as client:
        params: dict[str, Any] = {"query": query, "n": n_results}
        if wing:
            params["wing"] = wing
        if room:
            params["room"] = room
        resp = await client.get(_url("/api/palace/search"), params=params, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    if not results:
        return "No memories found."
    lines = []
    for r in results:
        loc = f"[{r['wing']}/{r['room']}]" if r.get("room") else f"[{r.get('wing', '?')}]"
        lines.append(f"- {loc} {r['text']} (score: {r['score']}, {r.get('created_at', '')[:10]})")
    return "\n".join(lines)


@mcp.tool()
async def palace_store(
    text: str,
    wing: str = "",
    room: str = "",
    category: str = "general",
    project: str = "",
) -> str:
    """Store a memory in the palace. Auto-routes to the appropriate wing/room if not specified."""
    async with httpx.AsyncClient(timeout=30) as client:
        body = {"text": text, "source": "claude_code", "category": category}
        if wing:
            body["wing"] = wing
        if room:
            body["room"] = room
        if project:
            body["project"] = project
        resp = await client.post(_url("/api/palace/store"), json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("ok"):
        return f"Stored: {data.get('id', 'unknown')}"
    return f"Not stored: {data.get('reason', 'unknown')}"


@mcp.tool()
async def palace_list_wings() -> str:
    """List all wings in the memory palace with their rooms."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_url("/api/palace/wings"), headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    lines = []
    for wing, info in data.items():
        rooms = ", ".join(info.get("rooms", []))
        lines.append(f"- {wing}: {info.get('description', '')} ({info.get('room_count', 0)} rooms: {rooms})")
    return "\n".join(lines) or "No wings configured."


@mcp.tool()
async def palace_list_rooms(wing: str) -> str:
    """List rooms in a specific wing with memory counts."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_url(f"/api/palace/wings/{wing}/rooms"), headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    lines = []
    for room, info in data.items():
        lines.append(f"- {room}: {info.get('description', '')} ({info.get('memory_count', 0)} memories)")
    return "\n".join(lines) or f"No rooms in wing '{wing}'."


@mcp.tool()
async def palace_get_memory(doc_id: str) -> str:
    """Get a specific memory by its ID."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_url(f"/api/palace/memory/{doc_id}"), headers=_headers())
        if resp.status_code == 404:
            return "Memory not found."
        resp.raise_for_status()
        data = resp.json()

    return (
        f"ID: {data['id']}\n"
        f"Wing: {data.get('wing', '')}/{data.get('room', '')}\n"
        f"Text: {data['text']}\n"
        f"Source: {data.get('source', '')}\n"
        f"Created: {data.get('created_at', '')}"
    )


@mcp.tool()
async def palace_mine_sessions(session_path: str = "") -> str:
    """Mine Claude Code session logs for insights and store them in the palace."""
    async with httpx.AsyncClient(timeout=120) as client:
        body = {}
        if session_path:
            body["session_path"] = session_path
        resp = await client.post(_url("/api/palace/mine"), json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("ok"):
        return f"Mining complete: {data.get('extracted', 0)} insights extracted, {data.get('stored', 0)} stored."
    return f"Mining failed: {data.get('error', 'unknown')}"


if __name__ == "__main__":
    mcp.run()
