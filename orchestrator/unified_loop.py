"""
Unified agentic tool loop: native function calling with XML fallback.

Replaces the two-model Helios→Nemotron flow with a single model that handles
both conversation and tool execution via OpenAI-compatible tool_calls.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List

from orchestrator.metrics import (
    CHAT_STREAM_OUTCOME,
    LLM_CALL_COUNT,
    LLM_CALL_ERRORS,
    LLM_CALL_LATENCY,
    TOOL_CALL_SOURCE,
    TOOL_ROUNDS,
)
from orchestrator.shared import MAX_TOOL_ROUNDS

logger = logging.getLogger(__name__)

# Hard cap on a single tool result inserted into the conversation. Without
# this cap, a tool like code_agent or a long check_system dump can dominate
# the model's 32K context budget after just a few invocations — every
# subsequent turn replays the entire bloated message list until llama-server
# rejects the request with HTTP 400. 8000 chars ≈ 2000 tokens, which is
# enough for a useful tool result but bounded enough that even several calls
# per session stay well under the context budget. Tools that need to return
# more than this should aggregate or summarize internally before returning.
MAX_TOOL_RESULT_CHARS = 8000


def _cap_tool_result(result: Any, tool_name: str) -> str:
    """Stringify and hard-cap a tool result so it can't poison the context."""
    s = str(result) if result is not None else ""
    if len(s) <= MAX_TOOL_RESULT_CHARS:
        return s
    truncated = s[:MAX_TOOL_RESULT_CHARS]
    overflow = len(s) - MAX_TOOL_RESULT_CHARS
    logger.warning(
        "[UNIFIED] Truncated %s result: %d chars → %d chars (+%d dropped)",
        tool_name,
        len(s),
        MAX_TOOL_RESULT_CHARS,
        overflow,
    )
    # Footer is model-facing, not user-facing. Explicit "do not call this
    # tool again" closes the ambiguity of earlier wording that a reasoning
    # model could interpret as "re-call me with narrower args" — the whole
    # point of this cap is to prevent that loop.
    return (
        f"{truncated}\n\n"
        f"[... {overflow} chars truncated to fit context budget. "
        f"Work with the information above; do not call this tool again to retrieve the rest.]"
    )


# Tools that mutate state — return result directly, don't loop
TERMINAL_TOOLS = {
    "start_focus",
    "stop_focus",
    "focus_sprint",
    "set_reminder",
    "cancel_reminder",
    "home_assistant",
    "update_data",
    "create_calendar_event",
    "brain_dump",
    "decompose_task",
    "task_step",
    "start_routine",
    "routine_action",
    "bookmark_context",
    "selfcare_log",
    "code_agent",
}


def clean_response(text: str) -> str:
    """Remove <think> and <tool_call> blocks (with any attributes) from responses.

    The open tag is matched with an attribute-tolerant pattern (`<think ...>`,
    `<tool_call id=1>`): a literal `<think>`-only match let an attributed tag
    like `<think reason=1>` leak its block through to the user. `\\b` keeps it
    from matching `<thinker>`; the closer allows trailing whitespace.

    Case-sensitive by design: the model/vLLM reasoning parser only ever emits
    lowercase sentinels, and matching uppercase would risk false-stripping
    legitimate prose like "<THINK ABOUT IT>". Prose that literally contains a
    lowercase `<think ...>…</think>` pair IS treated as a sentinel and removed —
    the safe default for a reasoning-hiding guard (can't structurally tell the
    model's reasoning tag from identical-looking prose).

    Note the lazy `.*?` is O(n²) on pathological input (a wall of unterminated
    `<think>`); acceptable here because input is model-generated and bounded by
    max_tokens — a coerced flood is a bounded, single-request self-inflicted cost.
    """
    text = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call\b[^>]*>.*?</tool_call\s*>", "", text, flags=re.DOTALL)
    return text.strip()


class StreamGate:
    """Stateful filter deciding which streamed content is safe to emit live.

    Mirrors clean_response for a token stream: suppresses <think>…</think>
    and <tool_call>…</tool_call> blocks, and holds back any trailing text
    that could be the start of one of those tags until enough characters
    arrive to disambiguate (vLLM's reasoning parser usually routes thinking
    to delta.reasoning_content, so the gate is a defensive second layer —
    XML-fallback tool calls DO arrive in delta.content).
    """

    _OPEN_NAMES = ("think", "tool_call")
    # Complete open tag WITH optional attributes: <think>, <think reason=1>,
    # <tool_call id="x">. `\b` stops <thinker>/<tool_calls> from matching.
    # Case-sensitive to match clean_response (model emits lowercase sentinels).
    # The attribute span is BOUNDED (`{0,256}`) to agree with _MAX_OPEN_TAG_LEN
    # below — so the streamed and whole-input paths make the identical
    # match/no-match decision (chunk-invariance): a tag long enough to trip the
    # stream's hold-back cap is also one the regex refuses to match.
    _MAX_ATTR_LEN = 256
    _OPEN_RE = re.compile(r"<(think|tool_call)\b[^>]{0,256}>")
    _CLOSERS = {"think": "</think>", "tool_call": "</tool_call>"}
    # Hold-back cap for an in-progress tag with no '>' yet. Chosen strictly above
    # the longest matchable open tag (len("<tool_call") + _MAX_ATTR_LEN + ">"
    # = 267) so a real bounded tag NEVER trips the cap mid-stream — only genuine
    # noise (an unterminated `<think aaaa…` flood) does. Keeps whole vs chunked
    # feeds in agreement while bounding the buffer.
    _MAX_OPEN_TAG_LEN = 320

    def __init__(self):
        self._pending = ""
        self._suppress_until: str | None = None

    @classmethod
    def _could_be_open_prefix(cls, rest: str) -> bool:
        """True if `rest` (starts with '<', no complete open tag yet) could still
        grow into an open sentinel tag — either a prefix of `<name`, or `<name`
        followed by an in-progress attribute list (no '>' yet). This is what
        holds back `<think re…` mid-stream; it must NOT hold `<thinker>`/`<div>`."""
        for name in cls._OPEN_NAMES:
            full = "<" + name
            if full.startswith(rest):
                return True  # still typing the tag name, e.g. "<thi"
            if rest.startswith(full):
                nxt = rest[len(full) : len(full) + 1]
                # name complete + next char is a boundary (space/newline/end) →
                # attributes may be arriving. A word char means a different tag
                # (<thinker>, <tool_calls>), so don't hold.
                if nxt == "" or not (nxt.isalnum() or nxt == "_"):
                    return True
        return False

    def feed(self, text: str) -> str:
        """Absorb a content delta, return the part that is safe to emit."""
        self._pending += text
        out: list = []
        while self._pending:
            if self._suppress_until:
                idx = self._pending.find(self._suppress_until)
                if idx == -1:
                    # Keep only a tail that could still be a partial closer.
                    keep = len(self._suppress_until) - 1
                    self._pending = self._pending[-keep:] if len(self._pending) > keep else self._pending
                    break
                self._pending = self._pending[idx + len(self._suppress_until) :]
                self._suppress_until = None
                continue

            lt = self._pending.find("<")
            if lt == -1:
                out.append(self._pending)
                self._pending = ""
                break
            out.append(self._pending[:lt])
            rest = self._pending[lt:]

            m = self._OPEN_RE.match(rest)
            if m:
                self._suppress_until = self._CLOSERS[m.group(1)]
                self._pending = rest[m.end() :]
                continue
            # Hold ONLY while the tag is genuinely still forming: no '>' yet
            # (a '>' present with no _OPEN_RE match above means it's a complete
            # non-sentinel like <div> or an over-long tag — emit it). This
            # `'>' not in rest` guard is what keeps whole and chunked feeds in
            # lockstep at the cap boundary.
            if ">" not in rest and self._could_be_open_prefix(rest):
                # Bound the hold: a real open tag is short, so a run this long
                # with still no '>' is noise, not a sentinel — emit the '<' and
                # re-scan rather than buffer attacker-controlled bytes forever.
                if len(rest) > self._MAX_OPEN_TAG_LEN:
                    out.append("<")
                    self._pending = rest[1:]
                    continue
                self._pending = rest
                break
            # A '<' that is provably not a sentinel: emit it and move on.
            out.append("<")
            self._pending = rest[1:]
        return "".join(out)

    def flush(self) -> str:
        """End of stream: release held-back text (unless mid-suppression)."""
        if self._suppress_until:
            self._pending = ""
            return ""
        out, self._pending = self._pending, ""
        return out


async def _stream_model_round(
    backend,
    messages: List[Dict],
    system_prompt: str,
    tools,
    tool_choice: str,
    timeout: int,
    extra_body,
    emit,
    label: str,
) -> Dict[str, Any]:
    """One streamed model round: relay gate-safe content via `emit` while
    assembling the full content + tool_calls.

    Never raises after the first emitted character — a mid-stream error is
    downgraded to "the stream ended here" so the caller can't double-send
    text by retrying. Returns:
        {"content", "tool_calls", "finish_reason", "emitted", "reasoning_len",
         "reasoning_has_tool_marker", "error": Exception | None}
    """
    gate = StreamGate()
    parts: List[str] = []
    tool_parts: Dict[int, Dict[str, Any]] = {}
    finish_reason = None
    emitted = 0
    reasoning_len = 0
    # Reasoning text is accumulated only to answer one yes/no question at the
    # end: did a <tool_call> tag land in the reasoning channel? That is the
    # fingerprint of the vLLM<0.20.0 qwen3 parser bug (see TOOL_CALL_SOURCE in
    # metrics.py). We keep the joined string out of the return value on purpose
    # — reasoning is model-internal content and this module's logs deliberately
    # carry no content — so only the boolean escapes.
    #
    # It must be accumulated rather than tested per-delta: the tag routinely
    # splits across SSE chunks ("<tool" / "_call>"), so a per-chunk membership
    # test would miss most real occurrences — the exact silent-zero failure this
    # instrumentation exists to avoid.
    reasoning_parts: List[str] = []
    error: Exception | None = None

    try:
        async for sse in backend.stream_chat_completion(
            messages,
            system=system_prompt,
            timeout=timeout,
            tools=tools,
            tool_choice=tool_choice,
            extra_body=extra_body,
        ):
            line = sse.strip()
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                reasoning_len += len(reasoning)
                reasoning_parts.append(reasoning)

            content = delta.get("content")
            if content:
                parts.append(content)
                safe = gate.feed(content)
                if safe:
                    emitted += len(safe)
                    await emit(safe)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                part = tool_parts.setdefault(
                    idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    part["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    part["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    part["function"]["arguments"] += fn["arguments"]
    except Exception as e:  # noqa: BLE001 — downgraded by design, see docstring
        error = e
        if emitted or tool_parts:
            logger.warning("[%s] Stream ended early after %d emitted chars: %s", label, emitted, e)

    tail = gate.flush()
    if tail and error is None:
        emitted += len(tail)
        await emit(tail)

    tool_calls = []
    if error is None:
        # A truncated stream can leave a tool call with half-baked JSON args —
        # executing that would be worse than dropping it, so tool_calls are
        # only trusted from a cleanly-finished stream.
        for idx in sorted(tool_parts):
            part = tool_parts[idx]
            if not part["id"]:
                part["id"] = f"call_s{idx}"
            tool_calls.append(part)
    elif tool_parts:
        logger.warning("[%s] Dropping %d tool call(s) from a broken stream", label, len(tool_parts))

    return {
        "content": "".join(parts),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "emitted": emitted,
        "reasoning_len": reasoning_len,
        "reasoning_has_tool_marker": bool(_TOOL_CALL_MARKER_RE.search("".join(reasoning_parts))),
        "error": error,
    }


# Detects an opening <tool_call> tag for the TOOL_CALL_SOURCE classification.
# `\b` is load-bearing and matches the convention at _OPEN_RE above: it admits
# `<tool_call>` and `<tool_call id="x">` while EXCLUDING `<tool_calls>` (plural),
# which tests/test_real_streaming.py::test_clean_keeps_tool_calls_lookalike
# pins as legitimate user-visible prose. Without it, the owner asking this
# assistant about its own tool-call handling would log an ERROR and inflate the
# "dropped" counter — and this codebase is a routine topic of conversation.
_TOOL_CALL_MARKER_RE = re.compile(r"<tool_call\b")


def parse_xml_tool_calls(content: str) -> List[Dict[str, Any]]:
    """Parse <tool_call> XML tags from model content (fallback for older models).

    Returns tool_calls in OpenAI-compatible format.
    """
    tool_calls = []
    # Greedy match between tags to handle nested JSON objects like {"arguments": {"key": "val"}}
    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    matches = re.findall(pattern, content, re.DOTALL)

    for i, match in enumerate(matches):
        try:
            parsed = json.loads(match)
            tool_name = parsed.get("name", "")
            arguments = parsed.get("arguments", {})

            tool_calls.append(
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments,
                    },
                }
            )
        except json.JSONDecodeError as e:
            logger.warning("[UNIFIED] Failed to parse XML tool call: %s", e)
            continue

    return tool_calls


# ---------------------------------------------------------------------------
# Selfcare safety net — auto-log if model skips the tool call
# ---------------------------------------------------------------------------

_MEAL_PATTERNS = re.compile(
    r"\b(i\s+ate|i\s+had|i\s+just\s+ate|i\s+just\s+had|had\s+breakfast|had\s+lunch|had\s+dinner|"
    r"had\s+a\s+snack|grabbed\s+a\s+snack|had\s+some\s+food|ate\s+breakfast|ate\s+lunch|ate\s+dinner|"
    r"i\s+ate\s+\w+|just\s+had\s+a\s+sandwich|had\s+a\s+meal|i\s+already\s+ate|already\s+had\s+breakfast|"
    r"already\s+had\s+lunch|already\s+had\s+dinner)\b",
    re.IGNORECASE,
)
_MED_PATTERNS = re.compile(
    r"\b(i\s+took\s+my\s+meds|took\s+my\s+medication|i\s+took\s+it|yes\s+i\s+took\s+it|"
    r"already\s+took\s+it|took\s+my\s+vyvanse|took\s+my\s+adderall|took\s+my\s+wellbutrin|"
    r"i\s+took\s+the\s+meds|meds\s+taken|took\s+meds)\b",
    re.IGNORECASE,
)
_WATER_PATTERNS = re.compile(
    r"\b(drank\s+water|had\s+water|i\s+drank|staying\s+hydrated|just\s+had\s+water|"
    r"had\s+some\s+water|drinking\s+water)\b",
    re.IGNORECASE,
)
_MOVEMENT_PATTERNS = re.compile(
    r"\b(went\s+for\s+a\s+walk|took\s+a\s+walk|i\s+walked|just\s+walked|"
    r"did\s+some\s+exercise|went\s+for\s+a\s+run|i\s+stretched|did\s+yoga)\b",
    re.IGNORECASE,
)


def _last_user_text(messages: list) -> str:
    """Extract the last user message text."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            text = msg["content"]
            # Skip Open WebUI auto-generated prompts (tags, follow-ups, titles)
            if text.startswith("### Task:"):
                continue
            return text
    return ""


def _detect_selfcare_action(text: str) -> dict | None:
    """Detect selfcare actions from user text. Returns tool args or None."""
    if not text:
        return None
    if _MEAL_PATTERNS.search(text):
        return {"action": "meal", "detail": "meal (auto-detected)"}
    if _MED_PATTERNS.search(text):
        return {"action": "medication", "detail": "medication (auto-detected)"}
    if _WATER_PATTERNS.search(text):
        return {"action": "water"}
    if _MOVEMENT_PATTERNS.search(text):
        return {"action": "movement"}
    return None


async def run_unified_tool_loop(
    messages: List[Dict],
    system_prompt: str,
    tools: List[Dict],
    model_url: str,
    model_name: str,
    http_client,
    max_rounds: int = MAX_TOOL_ROUNDS,
    label: str = "UNIFIED",
    is_voice: bool = False,
    on_delta=None,
) -> str:
    """
    Unified agentic tool loop with native function calling.

    Uses tool_choice="auto" for native OpenAI-compatible tool calling.
    Falls back to XML <tool_call> parsing if the model doesn't return
    structured tool_calls (backward compat for older models).

    Args:
        messages: Conversation messages.
        system_prompt: System prompt (unified: personality + tool instructions).
        tools: Tool definitions (all tools, not just ask_orchestrator).
        model_url: LLM endpoint URL.
        model_name: Model name/identifier.
        http_client: Not used directly (call_model resolves backend).
        max_rounds: Maximum tool execution rounds.
        label: Log label.
        on_delta: Optional async callback awaited with gate-safe text as the
            model generates it (real streaming). Rounds fall back to buffered
            call_model when the backend can't stream or a stream dies before
            anything was emitted. The return value stays the canonical final
            text either way.

    Returns:
        Final text response from the model.
    """
    # Import here to avoid circular import (tool_handlers imports shared)
    from orchestrator.orchestrator import call_model
    from orchestrator.tool_handlers import execute_tool

    stream_backend = None
    if on_delta is not None:
        try:
            from orchestrator.orchestrator import get_stream_capable_backend

            stream_backend = get_stream_capable_backend(model_url, model_name)
        except Exception as e:
            logger.warning("[%s] Stream backend resolution failed: %s", label, e)
        if stream_backend is None:
            logger.info("[%s] Backend not stream-capable — buffered rounds", label)
            CHAT_STREAM_OUTCOME.labels(outcome="not_stream_capable").inc()

    # Total gate-safe chars relayed to the client so far. Later rounds insert
    # a paragraph break before their first emission so multi-round answers
    # ("let me check… <tools run> …here's the answer") read as one message.
    emitted_total = 0

    def _make_emitter():
        state = {"sep": emitted_total > 0}

        async def _emit(text: str):
            nonlocal emitted_total
            if state["sep"]:
                state["sep"] = False
                emitted_total += 2
                await on_delta("\n\n")
            emitted_total += len(text)
            await on_delta(text)

        return _emit

    async def _finalize(text: str, *, already_streamed: bool) -> str:
        """Return the final text, emitting it first if it never went through
        the stream (buffered-fallback rounds, error apologies, max-rounds)."""
        if on_delta is not None and not already_streamed and text:
            try:
                await _make_emitter()(text)
            except Exception as e:  # noqa: BLE001 — emission must not eat the answer
                logger.warning("[%s] Final emit failed: %s", label, e)
        return text

    # Voice mode: reduce max_tokens for shorter responses (faster TTS) and
    # disable Qwen3 thinking. With vLLM's reasoning parser, thinking gets
    # extracted into a separate `reasoning` field — and on voice prompts
    # Qwen3.6 emits ~700-2000 tokens of reasoning before producing any
    # `content`, so a 1024 cap was being entirely consumed by reasoning,
    # leaving raw_len=0 (empty replies). Disabling thinking via chat-template
    # kwargs returns a direct answer; tool calls still fire (verified
    # 2026-04-26 against Qwen3.6-27B).
    voice_extra = None
    if is_voice:
        voice_extra = {
            "max_tokens": 1024,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        logger.info("[%s] Voice mode: max_tokens=1024, thinking=off", label)

    # Build allowlist of valid tool names from the tools passed in
    valid_tool_names = {t["function"]["name"] for t in tools if "function" in t}

    executed_calls = set()

    for round_num in range(max_rounds):
        logger.info("[%s] Round %d/%d", label, round_num + 1, max_rounds)

        _llm_t0 = time.time()
        llm_resp = None
        if stream_backend is not None:
            sr = await _stream_model_round(
                stream_backend,
                messages,
                system_prompt,
                tools,
                "auto",
                120,
                voice_extra,
                _make_emitter(),
                label,
            )
            LLM_CALL_COUNT.labels(model=model_name, purpose="unified_loop").inc()
            LLM_CALL_LATENCY.labels(model=model_name, purpose="unified_loop").observe(time.time() - _llm_t0)
            if sr["error"] is not None and sr["emitted"] == 0:
                # Clean failure before anything reached the client — a
                # buffered retry below is safe and can't double-send text.
                LLM_CALL_ERRORS.labels(model=model_name, error_type=type(sr["error"]).__name__).inc()
                CHAT_STREAM_OUTCOME.labels(outcome="pre_emission_retry").inc()
                logger.warning("[%s] Streamed round failed pre-emission (%s) — buffered retry", label, sr["error"])
            elif sr["error"] is not None:
                # Partial answer is already on the client's screen; retrying
                # would duplicate it. End the turn with what got through.
                LLM_CALL_ERRORS.labels(model=model_name, error_type=type(sr["error"]).__name__).inc()
                CHAT_STREAM_OUTCOME.labels(outcome="died_mid_emission").inc()
                TOOL_ROUNDS.observe(round_num + 1)
                logger.error("[%s] Stream died mid-answer — returning partial (%d chars)", label, sr["emitted"])
                return clean_response(sr["content"])
            else:
                # finish_reason is carried through so the TOOL_CALL_SOURCE
                # classification below can log it — a dropped call shows up as
                # finish_reason="stop" with an empty tool_calls array, and that
                # pairing is the fingerprint of the vLLM<0.20.0 parser bug.
                llm_resp = {
                    "choices": [
                        {
                            "message": {
                                "content": sr["content"],
                                "tool_calls": sr["tool_calls"] or None,
                                # The streaming path never carries reasoning TEXT
                                # (it is accumulated and discarded in
                                # _stream_model_round). Without this boolean the
                                # "dropped" classification below could never fire
                                # on the streaming path — i.e. on the default,
                                # primary interactive path — and the counter would
                                # read a reassuring zero exactly where the bug is
                                # most likely to be occurring.
                                "_reasoning_has_tool_marker": sr["reasoning_has_tool_marker"],
                            },
                            "finish_reason": sr["finish_reason"],
                        }
                    ]
                }
                logger.info(
                    "[%s] LLM probe (stream): raw_len=%d reasoning_len=%d emitted=%d tool_calls=%d elapsed=%.2fs",
                    label,
                    len(sr["content"]),
                    sr["reasoning_len"],
                    sr["emitted"],
                    len(sr["tool_calls"]),
                    time.time() - _llm_t0,
                )

        streamed_this_round = llm_resp is not None

        if llm_resp is None:
            try:
                llm_resp = await call_model(
                    model_url,
                    model_name,
                    messages,
                    system=system_prompt,
                    tools=tools,
                    tool_choice="auto",
                    timeout=120,
                    extra_body=voice_extra,
                )
                LLM_CALL_COUNT.labels(model=model_name, purpose="unified_loop").inc()
                LLM_CALL_LATENCY.labels(model=model_name, purpose="unified_loop").observe(time.time() - _llm_t0)
            except Exception as e:
                LLM_CALL_ERRORS.labels(model=model_name, error_type=type(e).__name__).inc()
                logger.error("[%s] Call failed: %s", label, e)
                return await _finalize(
                    "Sorry, I couldn't complete that action. Please try again.", already_streamed=False
                )

            choice = llm_resp.get("choices", [{}])[0]
            _message_probe = choice.get("message", {})
            _content_probe = _message_probe.get("content") or ""

            # Per-call LLM telemetry: prompt/completion tokens, think-tag
            # presence, wall-clock latency. Kept as an INFO log (not just
            # metrics) so you can correlate a specific turn's token load with
            # its latency in Loki.
            _usage = llm_resp.get("usage") or {}
            _has_think = "<think>" in _content_probe
            logger.info(
                "[%s] LLM probe: prompt_toks=%s completion_toks=%s raw_len=%d has_think=%s elapsed=%.2fs",
                label,
                _usage.get("prompt_tokens"),
                _usage.get("completion_tokens"),
                len(_content_probe),
                _has_think,
                time.time() - _llm_t0,
            )

        choice = llm_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        _had_native = bool(tool_calls)

        # Fallback: parse XML <tool_call> tags if no native tool_calls
        if not tool_calls and content:
            tool_calls = parse_xml_tool_calls(content)

        # Classify how the call arrived — see TOOL_CALL_SOURCE in metrics.py for
        # the vLLM 0.19.1 / PR #35687 defect this measures. Wrapped defensively:
        # this is diagnostics, and it must never be able to break a chat turn.
        try:
            if _had_native:
                TOOL_CALL_SOURCE.labels(source="native").inc()
            elif tool_calls:
                # Recovered by the XML fallback. NOTE: this is NOT the signal for
                # the vLLM reasoning-parser bug — that failure empties `content`
                # (so the fallback never runs) and emits qwen3_coder's non-JSON
                # tag format (which parse_xml_tool_calls cannot decode). See the
                # TOOL_CALL_SOURCE comment in metrics.py. Watch `dropped` instead.
                # INFO rather than WARNING: jobs_self_audit.py scrapes 24h of
                # warn/error from Loki into a nightly Pushover digest, and a
                # recoverable, already-counted event does not belong in it.
                TOOL_CALL_SOURCE.labels(source="xml_fallback").inc()
                logger.info(
                    "[%s] Tool call recovered via XML fallback (native tool_calls was empty, "
                    "finish_reason=%s) — unexpected for qwen3_coder; worth investigating.",
                    label,
                    choice.get("finish_reason"),
                )
            else:
                # No calls at all. If a marker is still present the model tried
                # and nothing caught it — the turn ends early and the user just
                # sees the assistant give up mid-task.
                # Buffered path exposes reasoning text; the streaming path can
                # only hand us the precomputed boolean (see the synthetic dict).
                _marker = bool(
                    _TOOL_CALL_MARKER_RE.search(content)
                    or message.get("_reasoning_has_tool_marker")
                    or _TOOL_CALL_MARKER_RE.search(message.get("reasoning_content") or "")
                )
                if _marker:
                    TOOL_CALL_SOURCE.labels(source="dropped").inc()
                    logger.error(
                        "[%s] Tool call SILENTLY DROPPED: <tool_call> marker present but "
                        "no call parsed (finish_reason=%s). The loop will end this turn "
                        "early. This is the vLLM<0.20.0 qwen3 reasoning-parser bug.",
                        label,
                        choice.get("finish_reason"),
                    )
                else:
                    TOOL_CALL_SOURCE.labels(source="none").inc()
        except Exception:  # pragma: no cover - diagnostics must never break chat
            # WARNING, not DEBUG: LOG_LEVEL defaults to INFO, so a DEBUG line here
            # would be invisible and a broken counter would read as a flat zero —
            # which is indistinguishable from "the bug never happens" and would be
            # used to justify skipping the vLLM upgrade.
            logger.warning("[%s] TOOL_CALL_SOURCE instrumentation failed", label, exc_info=True)

        # No tool calls — return the text response
        if not tool_calls:
            TOOL_ROUNDS.observe(round_num + 1)
            result = clean_response(content)
            # Length only, never the text. This used to log result[:100], which
            # shipped the first 100 characters of every assistant reply to Loki —
            # and medication names, dosages, medical detail and personal context
            # all flow through these turns. Loki retains 30 days and is scraped by
            # the self-audit job, so that was a standing copy of the user's
            # conversation in a second system. The length still gives the
            # empty-response signal that made this line useful for debugging.
            logger.info("[%s] Final response: %d chars", label, len(result))

            # Safety net: if user mentioned meals/meds/water/movement but model
            # skipped selfcare_log, call it automatically so nudges stop.
            if "selfcare_log" in valid_tool_names and round_num == 0:
                user_text = _last_user_text(messages)
                auto_action = _detect_selfcare_action(user_text)
                if auto_action:
                    logger.info("[%s] Auto-calling selfcare_log (model skipped it): %s", label, auto_action)
                    try:
                        await execute_tool("selfcare_log", auto_action)
                    except Exception as e:
                        logger.warning("[%s] Auto selfcare_log failed: %s", label, e)

            return await _finalize(result, already_streamed=streamed_this_round)

        # Filter out duplicate calls (already executed in a previous round)
        new_tool_calls = []
        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                logger.warning("[%s] Malformed arguments for %s: %s", label, tool_name, args_str[:100])
                arguments = {}
            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            if call_key in executed_calls:
                logger.info("[%s] Skipping repeat tool call: %s", label, tool_name)
            else:
                new_tool_calls.append(tool_call)

        # All calls are repeats — force a final response
        if not new_tool_calls:
            logger.info("[%s] All tool calls are repeats, forcing final response", label)
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": "You already have the results above. Do NOT call any tools again. Summarize the information in a brief, natural response.",
                }
            )
            _final_t0 = time.time()
            try:
                final_resp = await call_model(
                    model_url,
                    model_name,
                    messages,
                    system=system_prompt,
                    timeout=60,
                )
                LLM_CALL_COUNT.labels(model=model_name, purpose="unified_final").inc()
                LLM_CALL_LATENCY.labels(model=model_name, purpose="unified_final").observe(time.time() - _final_t0)
                final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                return await _finalize(clean_response(final_content), already_streamed=False)
            except Exception as e:
                LLM_CALL_ERRORS.labels(model=model_name, error_type=type(e).__name__).inc()
                logger.error("[%s] Final response failed: %s", label, e)
                return await _finalize(
                    "I found some results but couldn't summarize them. Please try again.", already_streamed=False
                )

        # Execute new tool calls
        logger.info("[%s] Processing %d tool call(s)", label, len(new_tool_calls))

        # Build assistant message with tool_calls for the conversation
        assistant_msg = {"role": "assistant", "content": content or None}
        # Only include tool_calls that will actually be executed (not skipped duplicates)
        # to avoid dangling tool_calls with no matching tool result messages
        if message.get("tool_calls"):
            executed_ids = {tc.get("id") for tc in new_tool_calls}
            assistant_msg["tool_calls"] = [tc for tc in message["tool_calls"] if tc.get("id") in executed_ids]
        messages.append(assistant_msg)

        # Parse and validate all calls first, then execute. A batch made up
        # entirely of read-only tools (e.g. check_calendar + check_email)
        # runs concurrently; any batch containing a state-mutating terminal
        # tool keeps the original strict sequential order.
        has_terminal = False
        parsed_calls = []  # (call_id, tool_name, arguments) — arguments is None when rejected
        for tool_call in new_tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                arguments = {}

            call_id = tool_call.get("id", f"call_{round_num}")

            # Validate tool name against allowlist (prevents hallucinated/injected tools)
            if tool_name not in valid_tool_names:
                logger.warning("[%s] Rejected unknown tool call: %s", label, tool_name[:50])
                parsed_calls.append((call_id, tool_name, None))
                continue

            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            executed_calls.add(call_key)
            parsed_calls.append((call_id, tool_name, arguments))

            if tool_name in TERMINAL_TOOLS:
                has_terminal = True

        async def _run_one(call_id: str, tool_name: str, arguments) -> tuple:
            if arguments is None:
                return (call_id, tool_name, f"Unknown tool: {tool_name}")
            # Metrics are recorded inside execute_tool() — no double-counting here
            try:
                result = await execute_tool(tool_name, arguments)
            except Exception as e:
                logger.error("[%s] Tool %s failed: %s", label, tool_name, e, exc_info=True)
                result = f"The {tool_name} tool encountered an error. Please try again."
            return (call_id, tool_name, _cap_tool_result(result, tool_name))

        if len(parsed_calls) > 1 and not has_terminal:
            tool_results = list(await asyncio.gather(*[_run_one(*pc) for pc in parsed_calls]))
        else:
            tool_results = [await _run_one(*pc) for pc in parsed_calls]

        # Add tool results to conversation
        # Use proper tool role messages if the model sent native tool_calls
        if message.get("tool_calls"):
            for call_id, _tool_name, result in tool_results:
                # result is already a str (capped by _cap_tool_result above)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    }
                )
        else:
            # XML fallback: add results as user message (like nemotron_loop)
            results_text = "\n".join(f"[{name}] {result}" for _, name, result in tool_results)
            messages.append(
                {
                    "role": "user",
                    "content": f"<tool_response>\n{results_text}\n</tool_response>\n\nThe action is complete. Summarize the result in a brief, natural response. Do NOT call any more tools.",
                }
            )

        # For terminal tools, allow one final round for natural language response
        # but prevent further tool calls
        if has_terminal:
            logger.info("[%s] Terminal tool executed, allowing final response round", label)
            # Add instruction to respond naturally without more tool calls
            if message.get("tool_calls"):
                messages.append(
                    {
                        "role": "user",
                        "content": "The action(s) above are complete. Now respond naturally to the user's full request. Do NOT call any more tools.",
                    }
                )
            # Cap remaining rounds to 1 (just generate the response)
            max_rounds = round_num + 2  # current round + 1 more

    # Hit max rounds
    TOOL_ROUNDS.observe(max_rounds)
    logger.warning("[%s] Hit max tool rounds (%d)", label, max_rounds)
    return await _finalize(
        "I tried to complete that but ran into some complexity. Please try a simpler request.",
        already_streamed=False,
    )
