"""Coder-box gate: the only way a sandboxed Qwen Code box reaches the brain.

Design: ``docs/internal/CODER_PLAN.md``. Measurements behind the numbers here:
``docs/internal/CODER_SPIKE.md``.

Boxes run on ``coder-net`` (docker-compose.yml). That network is internal and
puts no host address on its bridge. Its only other member is the orchestrator,
at ``CODER_GATE_ADDRESS`` inside ``CODER_SUBNET``; boxes get addresses from
``CODER_BOX_RANGE``. A box holds a *coder token* (one per jess-coder session or
coder job), never ``MODEL_API_KEY`` or ``API_TOKEN``.

What the gate guarantees:

* **Reach.** BearerAuthMiddleware treats a request as coming from coder-net when
  it arrived on the gate address *or* came from inside the coder subnet
  (:func:`on_coder_net`). Such a request may reach only :data:`GATE_PATHS`
  (403 otherwise). The gate itself serves only when *both* hold
  (:func:`arrived_on_gate`), and its paths 404 everywhere else.

  The two checks are independent, so drift between the compose address and the
  setting fails closed instead of opening the API to boxes. A coder token is
  useless from the LAN. The guard lives in a BaseHTTPMiddleware, which
  websocket connections skip, so the app must have no websocket routes (a test
  checks that).
* **Tokens.** Random, stored only as sha256, with UTC expiry, revocation, and
  request + token budgets. They are checked at admission and again before every
  attempt to reach the brain.
* **Admission.** At most :data:`MAX_ADMITTED_TOTAL` requests are admitted at a
  time, and one per token. With this middleware stack Starlette can't tell
  that a client hung up (``is_disconnected`` stays False), and Qwen Code sends
  one request at a time per session. So when a token sends a new request, the
  older one is treated as abandoned and is *superseded*: cancelled, and its
  answer becomes 409.
* **Request filter** (:func:`sanitize_request`). The upstream body is rebuilt
  from an allow-list:
  - The model is forced.
  - ``max_tokens`` and ``temperature`` are clamped; non-finite numbers are
    refused.
  - ``repeat_penalty`` is 1.0, or MTP acceptance collapses.
  - Message content is text only. llama-server fetches image URLs, which is
    SSRF from Helios, so non-text parts become a note.
  - Tool schemas are bounded: grammar blow-ups stall the brain Jess shares.
  - A client ``chat_template_kwargs`` is dropped, so llama-server's default
    (``reasoning_effort`` low) applies.

  No client header is forwarded; ``model_auth`` adds the brain key on
  ``shared._http``.
* **Response filter** (:func:`strip_chunk`). ``timings`` and
  ``usage.prompt_tokens_details`` are removed: cached-token counts would let a
  box probe Jess's cached prompt (memories, meds) one token at a time. Total
  latency still says a little about cache hits; that is accepted. Upstream
  error text never reaches the box. Only ``exceed_context_size_error`` passes,
  with a fixed message, so Qwen Code knows to compact.
* **Chat first** (``brain_slot``). A request waits until chat has been idle for
  ``CODER_IDLE_SECONDS``, then runs alone. It streams from llama-server into a
  buffer, and is aborted and re-sent if chat (or a vision call) starts in the
  meantime. The box gets the finished reply in one burst.

**Hold time.** :data:`MAX_HOLD_S` bounds the waiting before the final attempt.
That attempt's own prefill and generation come on top: roughly 25 s to prefill
64K, and up to ~140 s to generate while the TTS shares the GPU. So the first
byte can arrive about MAX_HOLD_S + 165 s after the request. The box's Qwen Code
``streamIdleTimeoutMs`` (default 240 s) must be larger than that.

Logs carry ids, counts, reasons and exception types only, never request or
response content.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import ipaddress
import json
import logging
import math
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from orchestrator import brain_slot, shared, state_store
from orchestrator.config import settings as _settings
from orchestrator.metrics import (
    CODER_ADMITTED,
    CODER_PREEMPTED_TOTAL,
    CODER_UPSTREAM_SECONDS,
    CODER_WAIT_SECONDS,
)

logger = logging.getLogger(__name__)

GATE_CHAT_PATH = "/api/coder/v1/chat/completions"
GATE_MODELS_PATH = "/api/coder/v1/models"
GATE_PATHS = frozenset({GATE_CHAT_PATH, GATE_MODELS_PATH})

MAX_BODY_BYTES = 2 * 1024 * 1024  # a full 262K-token context is ~1 MB of text
MAX_BYTES_PER_MINUTE = 32 * 1024 * 1024  # per token: JSON parsing runs on the event loop
MAX_ADMITTED_TOTAL = 4  # requests waiting or running, all tokens together
MAX_REJECTIONS = 20  # filter refusals before a token is revoked (real Qwen Code never trips it)
MAX_MESSAGES = 4000
MAX_TOOL_CALLS_PER_MESSAGE = 64
MAX_ID_CHARS = 256
MAX_TOOLS = 32
MAX_TOOL_SCHEMA_CHARS = 16_000  # Qwen Code's biggest (run_shell_command) is ~5.5K
MAX_TOOLS_TOTAL_CHARS = 64_000  # its default yolo set is ~20.6K
MAX_SCHEMA_DEPTH = 24
MAX_SCHEMA_BOUND = 1000  # minItems/maxLength/... above this blow up llama.cpp's grammar
MAX_ENUM = 64
MAX_ARGUMENTS_CHARS = 512 * 1024  # one tool call's arguments (write_file carries a whole file)
MAX_ARGUMENTS_DEPTH = 32  # llama.cpp's template engine parses them recursively
MAX_STOP = 4
MAX_STOP_CHARS = 64
MAX_HOLD_S = 300.0  # waits before the final attempt (see "Hold time" above)
MAX_PREEMPTS = 6
PREEMPT_POLL_S = 0.2
ATTEMPT_TIMEOUT_S = 240.0  # one upstream attempt, prefill + generation
MAX_ERROR_BODY_BYTES = 64 * 1024
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
NON_TEXT_NOTE = "[non-text content removed by the coder gate: the brain gets text only]"

TOKEN_PREFIX = {"session": "jcs_", "job": "jcj_"}
ACTIVE_WITHIN_S = 900  # a token used this recently counts as "in use" (Helios sleep refusal)
PRUNE_AFTER_DAYS = 30
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_BOUND_KEYS = frozenset({"minItems", "maxItems", "minLength", "maxLength", "minProperties", "maxProperties"})
# Tool-schema keyword ALLOW-list. llama-server compiles schemas into a grammar
# whose matcher is an unmemoized recursion: $ref/$defs chains, anyOf/oneOf,
# nested-repeat `pattern`s and the like cost exponential time from a few KB and
# wedge the shared server beyond any timeout (security review 2026-11-21).
# Qwen Code 0.25.3's six tools use only type/properties/required/description/
# minimum/maximum.
_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "items", "enum", "description", "title", "default", "minimum", "maximum",
     "additionalProperties"} | _BOUND_KEYS
)  # fmt: skip
_SCHEMA_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})
_PASSED_KEYS = frozenset(
    {
        "messages",
        "tools",
        "tool_choice",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "stream",
    }
)
# Dropped-key metric labels: these names, everything else counts as "other".
KNOWN_DROPPED_KEYS = frozenset(
    {
        "model", "stream_options", "response_format", "grammar", "json_schema", "id_slot", "cache_prompt",
        "n_probs", "logit_bias", "lora", "samplers", "n", "chat_template_kwargs", "logprobs", "top_logprobs",
        "seed", "user", "presence_penalty", "frequency_penalty", "repeat_penalty", "parallel_tool_calls",
        "metadata", "reasoning_effort",
    }
)  # fmt: skip
REJECT_REASONS = (
    "bad_json", "bad_messages", "bad_role", "bad_tools", "too_many_tools", "schema_too_large",
    "schema_bounds", "schema_keyword", "bad_params",
)  # fmt: skip
# llama-server error types worth passing through (fixed text only): Qwen Code
# compacts its history when it is told the context is full.
PASS_THROUGH_ERRORS = {"exceed_context_size_error": "the request exceeds the available context size"}


class GateRejected(Exception):
    """The request body failed the filter; ``reason`` is in REJECT_REASONS."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GateBusy(Exception):
    """Too many requests admitted, or chat kept the brain past the wait budget (429)."""


class Superseded(Exception):
    """A newer request from the same token replaced this one (its client gave up)."""


class TokenRevoked(Exception):
    """The token was revoked, expired or ran out of budget while the request waited."""


class BrainUnavailable(Exception):
    """llama-server could not be reached or is still loading (503)."""


class UpstreamError(Exception):
    """llama-server answered with an error.

    ``status`` is the HTTP status (0 = broken or truncated stream); ``err_type``
    is set only for PASS_THROUGH_ERRORS.
    """

    def __init__(self, status: int, err_type: Optional[str] = None):
        super().__init__(f"upstream {status}")
        self.status = status
        self.err_type = err_type


class _Preempted(Exception):
    pass


# ----------------------------------------------------------------- reach


@functools.lru_cache(maxsize=8)
def _network(cidr: str) -> Optional[ipaddress.IPv4Network]:
    try:
        return ipaddress.IPv4Network(cidr) if cidr else None
    except ValueError:
        return None


def _ipv4(value: Any) -> Optional[ipaddress.IPv4Address]:
    """An IPv4 address, unwrapping IPv4-mapped IPv6 (uvicorn on `::`)."""
    try:
        ip = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.ipv4_mapped
    return ip


def _server_is_gate(scope: Dict[str, Any]) -> bool:
    """The local socket uvicorn accepted on (the client can't set it)."""
    address = _ipv4(_settings.coder_gate_address)
    server = scope.get("server")
    return address is not None and bool(server) and _ipv4(server[0]) == address


def _client_in_subnet(scope: Dict[str, Any]) -> bool:
    """The TCP peer is inside the coder subnet. uvicorn trusts X-Forwarded-For
    only from 127.0.0.1, so a box can't fake this."""
    net = _network(_settings.coder_subnet)
    client = scope.get("client")
    if net is None or not client:
        return False
    ip = _ipv4(client[0])
    return ip is not None and ip in net


def on_coder_net(scope: Dict[str, Any]) -> bool:
    """Guard condition: the request may have come from a box (fails closed)."""
    return _server_is_gate(scope) or _client_in_subnet(scope)


def arrived_on_gate(scope: Dict[str, Any]) -> bool:
    """Serve condition: arrived on the gate address *and* from the coder subnet."""
    return _server_is_gate(scope) and _client_in_subnet(scope)


def gate_enabled() -> bool:
    return bool(getattr(shared, "CODER_ENABLED", False))


# ----------------------------------------------------------------- tokens


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def valid_label(label: str) -> bool:
    return label == "" or bool(_LABEL_RE.match(label))


def valid_token_id(token_id: str) -> bool:
    return bool(_TOKEN_ID_RE.match(token_id or ""))


def mint_token(
    kind: str,
    label: str = "",
    hours: Optional[float] = None,
    max_requests: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a coder token. Returns the token itself only here, never again."""
    if kind not in TOKEN_PREFIX:
        raise ValueError(f"unknown token kind {kind!r}")
    if not valid_label(label):
        raise ValueError("bad label")
    hours = _settings.coder_session_hours if hours is None else hours
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("bad hours")
    max_requests = _settings.coder_session_max_requests if max_requests is None else max_requests
    max_tokens = _settings.coder_session_max_tokens if max_tokens is None else max_tokens
    token = TOKEN_PREFIX[kind] + secrets.token_urlsafe(32)
    token_id = uuid.uuid4().hex
    now = _now()
    expires = now + timedelta(hours=hours)
    cutoff = _stamp(now - timedelta(days=PRUNE_AFTER_DAYS))
    with state_store.get_db() as conn:
        conn.execute(
            "DELETE FROM coder_tokens WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
            (cutoff, cutoff),
        )
        conn.execute(
            "INSERT INTO coder_tokens (id, kind, label, token_sha256, created_at, expires_at, max_requests, max_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token_id, kind, label, _hash(token), _stamp(now), _stamp(expires), int(max_requests), int(max_tokens)),
        )
    return {"id": token_id, "token": token, "kind": kind, "label": label, "expires_at": _stamp(expires)}


def _usable(row: Dict[str, Any], now: datetime) -> bool:
    if row.get("revoked_at"):
        return False
    try:
        return datetime.fromisoformat(row["expires_at"]) > now
    except (TypeError, ValueError):
        return False


def lookup_token(token: str) -> Optional[Dict[str, Any]]:
    """The live token row for ``token`` (not revoked, not expired), else None."""
    if not token or len(token) > 200 or not token.startswith(tuple(TOKEN_PREFIX.values())):
        return None
    with state_store.get_db() as conn:
        row = conn.execute("SELECT * FROM coder_tokens WHERE token_sha256 = ?", (_hash(token),)).fetchone()
    if row is None:
        return None
    row = dict(row)
    return row if _usable(row, _now()) else None


def budget_left(row: Dict[str, Any]) -> bool:
    return (
        row["requests"] < row["max_requests"] and (row["prompt_tokens"] + row["completion_tokens"]) < row["max_tokens"]
    )


def token_still_usable(token_id: str) -> bool:
    """Re-check a token by id before an attempt: still live and within budget.
    This request was already counted at admission, so ``requests`` may equal
    ``max_requests`` (the token's last allowed request)."""
    with state_store.get_db() as conn:
        row = conn.execute("SELECT * FROM coder_tokens WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        return False
    row = dict(row)
    within = (
        row["requests"] <= row["max_requests"] and (row["prompt_tokens"] + row["completion_tokens"]) < row["max_tokens"]
    )
    return _usable(row, _now()) and within


def count_request(token_id: str) -> None:
    """Every admitted request counts toward ``max_requests`` (refused ones too)."""
    with state_store.get_db() as conn:
        conn.execute(
            "UPDATE coder_tokens SET requests = requests + 1, last_used_at = ? WHERE id = ?",
            (_stamp(_now()), token_id),
        )


def record_usage(token_id: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Add a finished request's brain tokens (the request itself was counted at admission)."""
    with state_store.get_db() as conn:
        conn.execute(
            "UPDATE coder_tokens SET prompt_tokens = prompt_tokens + ?,"
            " completion_tokens = completion_tokens + ?, last_used_at = ? WHERE id = ?",
            (max(0, int(prompt_tokens)), max(0, int(completion_tokens)), _stamp(_now()), token_id),
        )


def revoke_token(token_id: str, kind: Optional[str] = None) -> bool:
    """Revoke a live token. False if no such live token (of that kind)."""
    if not valid_token_id(token_id):
        return False
    sql = "UPDATE coder_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
    args: List[Any] = [_stamp(_now()), token_id]
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    with state_store.get_db() as conn:
        return conn.execute(sql, args).rowcount > 0


def list_tokens(kind: str) -> List[Dict[str, Any]]:
    """Live tokens of ``kind``, newest first, without their hashes."""
    with state_store.get_db() as conn:
        rows = conn.execute(
            "SELECT id, kind, label, created_at, expires_at, revoked_at, last_used_at, requests, prompt_tokens,"
            " completion_tokens, max_requests, max_tokens FROM coder_tokens WHERE kind = ? AND revoked_at IS NULL"
            " ORDER BY created_at DESC",
            (kind,),
        ).fetchall()
    now = _now()
    return [dict(r) for r in rows if _usable(dict(r), now)]


def active_use_exists(within_s: float = ACTIVE_WITHIN_S) -> bool:
    """True if a live token was used in the last ``within_s`` seconds."""
    since = _stamp(_now() - timedelta(seconds=within_s))
    with state_store.get_db() as conn:
        rows = conn.execute(
            "SELECT expires_at, revoked_at FROM coder_tokens WHERE revoked_at IS NULL AND last_used_at >= ?",
            (since,),
        ).fetchall()
    now = _now()
    return any(_usable(dict(r), now) for r in rows)


# ----------------------------------------------------------------- per-token abuse limits

_bytes_seen: Dict[str, List[Tuple[float, int]]] = {}
_rejections: Dict[str, int] = {}


def bytes_allowance(token_id: str) -> int:
    """How many body bytes this token may still send in the current minute."""
    cutoff = time.monotonic() - 60.0
    seen = [entry for entry in _bytes_seen.get(token_id, []) if entry[0] >= cutoff]
    _bytes_seen[token_id] = seen
    return max(0, MAX_BYTES_PER_MINUTE - sum(n for _, n in seen))


def note_bytes(token_id: str, count: int) -> None:
    _bytes_seen.setdefault(token_id, []).append((time.monotonic(), count))


def note_rejection(token_id: str) -> bool:
    """Count a filter refusal; True when the token has just been revoked for it."""
    _rejections[token_id] = _rejections.get(token_id, 0) + 1
    if _rejections[token_id] < MAX_REJECTIONS:
        return False
    _rejections.pop(token_id, None)
    revoke_token(token_id)
    return True


# ----------------------------------------------------------------- admission


class Admission:
    """One admitted request: a reserved slot, supersedable by its token's next request."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.superseded = False
        self._task: Optional[asyncio.Task] = None

    def supersede(self) -> None:
        self.superseded = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def run(
        self, up: Dict[str, Any], still_usable: Optional[Callable[[], Awaitable[bool]]] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        if self.superseded:
            raise Superseded()
        self._task = asyncio.create_task(run_chat(up, still_usable))
        try:
            return await self._task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.superseded and not (current is not None and current.cancelling()):
                raise Superseded() from None
            raise

    def release(self) -> None:
        if _admitted.get(self.token_id) is self:
            del _admitted[self.token_id]
            CODER_ADMITTED.set(len(_admitted))


_admitted: Dict[str, Admission] = {}


def admit(token_id: str) -> Admission:
    """Reserve a slot before the body is read. Supersedes the token's older
    request; raises GateBusy when MAX_ADMITTED_TOTAL requests are already in."""
    old = _admitted.get(token_id)
    if old is not None:
        old.supersede()
    elif len(_admitted) >= MAX_ADMITTED_TOTAL:
        raise GateBusy()
    adm = Admission(token_id)
    _admitted[token_id] = adm
    CODER_ADMITTED.set(len(_admitted))
    return adm


def requests_admitted() -> int:
    """Coder requests waiting or running right now (Helios sleep refusal)."""
    return len(_admitted)


# ----------------------------------------------------------------- request filter


def _number(value: Any) -> float:
    """A finite JSON number. json.loads accepts NaN/Infinity and huge integers,
    which slip through min/max clamps (comparisons with NaN are all False)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateRejected("bad_params")
    try:
        number = float(value)
    except OverflowError:
        raise GateRejected("bad_params") from None
    if not math.isfinite(number):
        raise GateRejected("bad_params")
    return number


def _clean_text_parts(parts: List[Any], dropped: List[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            out.append({"type": "text", "text": part["text"]})
        else:
            # Images (and anything else) never reach the brain: llama-server
            # fetches image URLs. Tell the model instead of failing the turn.
            out.append({"type": "text", "text": NON_TEXT_NOTE})
            dropped.append("non_text_part")
    return out


def _json_depth_ok(value: Any, limit: int) -> bool:
    """Iterative depth check (no recursion for the box to exhaust)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return False
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return True


def _clean_arguments(arguments: str, dropped: List[str]) -> str:
    """Tool-call arguments as a re-serialized JSON object. The chat template
    parses them with a recursive parser on llama-server (100 KB of brackets
    segfaults it), so anything that isn't a small, shallow object becomes {}."""
    try:
        if len(arguments) > MAX_ARGUMENTS_CHARS:
            raise ValueError("too long")
        value = json.loads(arguments) if arguments.strip() else {}
        if not isinstance(value, dict) or not _json_depth_ok(value, MAX_ARGUMENTS_DEPTH):
            raise ValueError("not a shallow object")
        return json.dumps(value)
    except (ValueError, RecursionError, OverflowError):
        dropped.append("bad_tool_arguments")
        return "{}"


def _clean_tool_calls(calls: Any, dropped: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS_PER_MESSAGE:
        raise GateRejected("bad_messages")
    out = []
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict):
            raise GateRejected("bad_messages")
        name, arguments, call_id = fn.get("name"), fn.get("arguments", ""), call.get("id", "")
        if (
            not isinstance(name, str)
            or not _TOOL_NAME_RE.match(name)
            or not isinstance(arguments, str)
            or not isinstance(call_id, str)
            or len(call_id) > MAX_ID_CHARS
        ):
            raise GateRejected("bad_messages")
        arguments = _clean_arguments(arguments, dropped)
        out.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
    return out


def _clean_message(message: Any, dropped: List[str]) -> Dict[str, Any]:
    if not isinstance(message, dict):
        raise GateRejected("bad_messages")
    role = message.get("role")
    if role not in _ALLOWED_ROLES:
        raise GateRejected("bad_role")
    out: Dict[str, Any] = {"role": role}
    content = message.get("content")
    if isinstance(content, str):
        out["content"] = content
    elif isinstance(content, list):
        out["content"] = _clean_text_parts(content, dropped)
    elif content is None:
        out["content"] = None if (role == "assistant" and message.get("tool_calls")) else ""
    else:
        raise GateRejected("bad_messages")
    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > MAX_ID_CHARS:
                raise GateRejected("bad_messages")
            out[key] = value
    reasoning = message.get("reasoning_content")
    if reasoning is not None:
        if not isinstance(reasoning, str):
            raise GateRejected("bad_messages")
        out["reasoning_content"] = reasoning
    if message.get("tool_calls"):
        if role != "assistant":
            raise GateRejected("bad_messages")
        out["tool_calls"] = _clean_tool_calls(message["tool_calls"], dropped)
    return out


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_schema(node: Any, depth: int = 0) -> None:
    """Allow-list check of one schema node (see _SCHEMA_KEYWORDS)."""
    if depth > MAX_SCHEMA_DEPTH:
        raise GateRejected("schema_bounds")
    if not isinstance(node, dict) or set(node) - _SCHEMA_KEYWORDS:
        raise GateRejected("schema_keyword")
    types = node.get("type")
    types = types if isinstance(types, list) else [types]
    if not types or not all(isinstance(t, str) and t in _SCHEMA_TYPES for t in types):
        raise GateRejected("schema_keyword")  # untyped schemas compile to "any JSON"
    for key in _BOUND_KEYS & node.keys():
        value = node[key]
        if not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SCHEMA_BOUND):
            raise GateRejected("schema_bounds")
    for key in ("minimum", "maximum"):
        if key in node and not _is_number(node[key]):
            raise GateRejected("schema_bounds")
    for key in ("description", "title"):
        if key in node and not isinstance(node[key], str):
            raise GateRejected("schema_keyword")
    scalar = (str, int, float, bool, type(None))
    if "default" in node and not isinstance(node["default"], scalar):
        raise GateRejected("schema_keyword")
    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not 0 < len(enum) <= MAX_ENUM or not all(isinstance(e, scalar) for e in enum):
            raise GateRejected("schema_bounds")
    if "additionalProperties" in node and not isinstance(node["additionalProperties"], bool):
        raise GateRejected("schema_keyword")
    if "required" in node and not (
        isinstance(node["required"], list) and all(isinstance(r, str) for r in node["required"])
    ):
        raise GateRejected("schema_keyword")
    properties = node.get("properties", {})
    if not isinstance(properties, dict):
        raise GateRejected("schema_keyword")
    for name, child in properties.items():
        if not isinstance(name, str) or not 0 < len(name) <= 64:
            raise GateRejected("schema_keyword")
        _check_schema(child, depth + 1)
    if "items" in node:
        _check_schema(node["items"], depth + 1)  # a list (tuple form) fails the dict check


def _clean_tools(tools: Any) -> List[Dict[str, Any]]:
    if not isinstance(tools, list):
        raise GateRejected("bad_tools")
    if len(tools) > MAX_TOOLS:
        raise GateRejected("too_many_tools")
    out, total = [], 0
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) and tool.get("type") == "function" else None
        if not isinstance(fn, dict):
            raise GateRejected("bad_tools")
        name = fn.get("name")
        description = fn.get("description", "")
        parameters = fn.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
            raise GateRejected("bad_tools")
        if not isinstance(description, str) or not isinstance(parameters, dict):
            raise GateRejected("bad_tools")
        if parameters.get("type") != "object":
            raise GateRejected("schema_keyword")
        _check_schema(parameters)
        item = {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}
        size = len(json.dumps(item))
        if size > MAX_TOOL_SCHEMA_CHARS:
            raise GateRejected("schema_too_large")
        total += size
        out.append(item)
    if total > MAX_TOOLS_TOTAL_CHARS:
        raise GateRejected("schema_too_large")
    return out


def _clean_tool_choice(choice: Any) -> Any:
    if choice in ("auto", "none", "required"):
        return choice
    fn = choice.get("function") if isinstance(choice, dict) and choice.get("type") == "function" else None
    if isinstance(fn, dict) and isinstance(fn.get("name"), str) and _TOOL_NAME_RE.match(fn["name"]):
        return {"type": "function", "function": {"name": fn["name"]}}
    raise GateRejected("bad_params")


def _clean_stop(stop: Any) -> List[str]:
    items = [stop] if isinstance(stop, str) else stop
    if not isinstance(items, list) or len(items) > MAX_STOP:
        raise GateRejected("bad_params")
    if not all(isinstance(s, str) and 0 < len(s) <= MAX_STOP_CHARS for s in items):
        raise GateRejected("bad_params")
    return list(items)


def sanitize_request(body: Any, max_tokens_cap: int) -> Tuple[Dict[str, Any], List[str]]:
    """Build the upstream body from an allow-list. Returns (body, dropped key names).

    Raises :class:`GateRejected` for anything malformed or over a bound.
    """
    if not isinstance(body, dict):
        raise GateRejected("bad_json")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise GateRejected("bad_messages")
    dropped: List[str] = [key for key in body if key not in _PASSED_KEYS]
    up: Dict[str, Any] = {
        "model": shared.MODEL_NAME or "default",
        "messages": [_clean_message(m, dropped) for m in messages],
        "stream": True,
        "stream_options": {"include_usage": True},
        "repeat_penalty": 1.0,
    }
    if body.get("tools") is not None:
        up["tools"] = _clean_tools(body["tools"])
        if body.get("tool_choice") is not None:
            up["tool_choice"] = _clean_tool_choice(body["tool_choice"])
    requested = body.get("max_tokens", body.get("max_completion_tokens"))
    up["max_tokens"] = max_tokens_cap if requested is None else int(min(max(_number(requested), 1), max_tokens_cap))
    if body.get("temperature") is not None:
        up["temperature"] = min(max(_number(body["temperature"]), 0.0), 1.0)
    if body.get("top_p") is not None:
        up["top_p"] = min(max(_number(body["top_p"]), 0.01), 1.0)
    if body.get("stop") is not None:
        up["stop"] = _clean_stop(body["stop"])
    return up, dropped


def dropped_key_label(key: str) -> str:
    return key if key in KNOWN_DROPPED_KEYS or key in ("non_text_part", "bad_tool_arguments") else "other"


# ----------------------------------------------------------------- response filter


_CHUNK_KEYS = frozenset({"id", "object", "created", "model", "system_fingerprint", "choices", "usage"})
_CHOICE_KEYS = frozenset({"index", "delta", "message", "finish_reason"})
_DELTA_KEYS = frozenset({"role", "content", "reasoning_content", "tool_calls"})
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _clean_delta(delta: Any) -> Dict[str, Any]:
    if not isinstance(delta, dict):
        return {}
    out = {k: v for k, v in delta.items() if k in _DELTA_KEYS}
    if "tool_calls" in out:
        calls = []
        for tc in out["tool_calls"] if isinstance(out["tool_calls"], list) else []:
            if not isinstance(tc, dict):
                continue
            clean = {k: v for k, v in tc.items() if k in ("index", "id", "type")}
            fn = tc.get("function")
            if isinstance(fn, dict):
                clean["function"] = {k: v for k, v in fn.items() if k in ("name", "arguments")}
            calls.append(clean)
        out["tool_calls"] = calls
    return out


def strip_chunk(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only known fields (an allow-list, so a future llama-server field
    can't leak): no timings, cached-token counts or debug fields."""
    out = {k: v for k, v in obj.items() if k in _CHUNK_KEYS}
    if isinstance(out.get("usage"), dict):
        out["usage"] = {k: out["usage"][k] for k in _USAGE_KEYS if k in out["usage"]}
    elif "usage" in out:
        out.pop("usage")
    choices = []
    for choice in out.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        clean = {k: v for k, v in choice.items() if k in _CHOICE_KEYS}
        for key in ("delta", "message"):
            if key in clean:
                clean[key] = _clean_delta(clean[key])
        choices.append(clean)
    if "choices" in out:
        out["choices"] = choices
    return out


def sse_body(chunks: List[Dict[str, Any]]) -> str:
    """The buffered reply as one SSE body (it's all here already; no generator)."""
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def assemble(chunks: List[Dict[str, Any]], usage: Dict[str, int]) -> Dict[str, Any]:
    """One chat.completion from the buffered stream (for non-stream clients)."""
    message: Dict[str, Any] = {"role": "assistant", "content": ""}
    calls: Dict[int, Dict[str, Any]] = {}
    finish = None
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            message["content"] += delta.get("content") or ""
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(
                    tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                slot["id"] = slot["id"] or tc.get("id") or ""
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {
        "id": "coder-" + uuid.uuid4().hex[:12],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": shared.MODEL_NAME or "default",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {**usage, "total_tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)},
    }


# ----------------------------------------------------------------- upstream


def _error_obj(payload: Any) -> Dict[str, Any]:
    """llama-server errors come wrapped ({"error": {...}}) or, in its SSE
    `error:` events, possibly bare ({"code", "message", "type"})."""
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    return error if isinstance(error, dict) else payload


def _error_type(payload: Any) -> Optional[str]:
    err_type = _error_obj(payload).get("type")
    return err_type if err_type in PASS_THROUGH_ERRORS else None


def _error_status(payload: Any) -> int:
    code = _error_obj(payload).get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else 0


async def _read_error_body(resp: httpx.Response) -> Any:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_ERROR_BODY_BYTES:
            return None
    try:
        return json.loads(bytes(buf))
    except ValueError:
        return None


async def _stream_once(up: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """One streamed request to llama-server, buffered. No client header goes along.

    Succeeds only on a cleanly finished stream ([DONE] or a finish_reason);
    an error event or a stream that just stops is an UpstreamError.
    """
    client = shared._http
    if client is None:
        raise BrainUnavailable()
    url = f"{shared.MODEL_URL.rstrip('/')}/chat/completions"
    chunks: List[Dict[str, Any]] = []
    usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
    finished = False
    try:
        async with client.stream("POST", url, json=up, timeout=UPSTREAM_TIMEOUT) as resp:
            if resp.status_code == 503:  # llama-server still loading the model (Helios waking)
                raise BrainUnavailable()
            if resp.status_code != 200:
                payload = await _read_error_body(resp)
                raise UpstreamError(resp.status_code, _error_type(payload))
            async for line in resp.aiter_lines():
                if line.startswith("error:"):  # llama-server's mid-stream error event
                    payload = json.loads(line[6:].strip() or "null")
                    raise UpstreamError(_error_status(payload), _error_type(payload))
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    finished = True
                    break
                if not data:
                    continue
                obj = json.loads(data)
                if not isinstance(obj, dict):
                    continue
                if "error" in obj:
                    raise UpstreamError(_error_status(obj), _error_type(obj))
                raw_usage = obj.get("usage")
                if isinstance(raw_usage, dict):
                    usage = {
                        "prompt_tokens": max(0, int(raw_usage.get("prompt_tokens") or 0)),
                        "completion_tokens": max(0, int(raw_usage.get("completion_tokens") or 0)),
                    }
                choices = obj.get("choices") or []
                if not isinstance(choices, list) or not all(isinstance(c, dict) for c in choices):
                    raise UpstreamError(0)  # malformed chunk
                if any(c.get("finish_reason") for c in choices):
                    finished = True
                chunks.append(strip_chunk(obj))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise BrainUnavailable() from exc
    except (httpx.HTTPError, ValueError, TypeError) as exc:  # read/timeout errors, bad JSON or numbers
        raise UpstreamError(0) from exc
    if not finished:
        raise UpstreamError(0)  # the stream stopped without finishing
    return chunks, usage


async def _run_preemptable(up: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Run one upstream attempt; abort it (closing the connection) if chat starts."""
    task = asyncio.create_task(_stream_once(up))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=PREEMPT_POLL_S)
            if done:
                return task.result()
            if brain_slot.chat_busy():
                raise _Preempted()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Our own cancel of the attempt is expected; a cancel aimed at
                # *this* task (route gone, request superseded) must go on.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:  # noqa: BLE001 — the attempt is abandoned either way
                pass


_locks: Dict[int, asyncio.Lock] = {}


def _gate_lock() -> asyncio.Lock:
    """One coder request at a time (per event loop; tests run several loops)."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(id(loop))
    if lock is None:
        _locks.clear()
        lock = _locks[id(loop)] = asyncio.Lock()
    return lock


async def run_chat(
    up: Dict[str, Any], still_usable: Optional[Callable[[], Awaitable[bool]]] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Wait for the coder's turn, run the request alone, re-send after preemption.

    ``still_usable`` is checked before every attempt, so a token revoked (or
    out of budget) while the request waited never reaches the brain.

    Raises GateBusy, TokenRevoked, BrainUnavailable or UpstreamError.
    """
    start = time.monotonic()

    def remaining() -> float:
        return MAX_HOLD_S - (time.monotonic() - start)

    def busy() -> GateBusy:
        CODER_WAIT_SECONDS.observe(time.monotonic() - start)
        return GateBusy()

    lock = _gate_lock()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=max(0.0, min(_settings.coder_max_wait_seconds, remaining())))
    except TimeoutError:
        raise busy() from None
    try:
        preempts = 0
        waited_logged = False
        while True:
            budget = min(_settings.coder_max_wait_seconds, remaining())
            if budget <= 0 or not await brain_slot.wait_for_coder_turn(_settings.coder_idle_seconds, budget):
                raise busy()
            if still_usable is not None and not await still_usable():
                raise TokenRevoked()
            if not waited_logged:
                CODER_WAIT_SECONDS.observe(time.monotonic() - start)
                waited_logged = True
            attempt_start = time.monotonic()
            with brain_slot.coder_request():
                try:
                    result = await asyncio.wait_for(_run_preemptable(up), ATTEMPT_TIMEOUT_S)
                except TimeoutError:
                    raise UpstreamError(504) from None
                except _Preempted:
                    preempts += 1
                    CODER_PREEMPTED_TOTAL.inc()
                    logger.info("[CODER] Request preempted by chat (%d so far)", preempts)
                    if preempts > MAX_PREEMPTS:
                        raise GateBusy() from None
                    continue
            CODER_UPSTREAM_SECONDS.observe(time.monotonic() - attempt_start)
            return result
    finally:
        lock.release()


# ----------------------------------------------------------------- startup


def _address_is_local(address: str) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((address, 0))
        return True
    except OSError:
        return False


def startup_check() -> None:
    """Log the gate state; turn the gate off if its address isn't ours.

    Binding a socket to CODER_GATE_ADDRESS proves the container has it. If
    compose and .env are out of step, boxes would only ever get 403s from the
    guard and nothing would say why.
    """
    address = _settings.coder_gate_address
    local = bool(address) and _address_is_local(address)
    if not gate_enabled():
        if local:
            logger.info("[CODER] Gate disabled; coder-net guard active on %s", address)
        else:
            logger.warning(
                "[CODER] Gate disabled, and %s is not an address of this container (no coder-net?);"
                " the guard still refuses requests from %s",
                address,
                _settings.coder_subnet,
            )
        return
    if not local:
        logger.error(
            "[CODER] CODER_GATE_ADDRESS %s is not an address of this container; coder gate OFF."
            " Check the coder-net block in docker-compose.yml.",
            address,
        )
        shared.CODER_ENABLED = False
        return
    logger.info("[CODER] Gate enabled on %s (subnet %s)", address, _settings.coder_subnet)
