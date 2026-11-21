"""Who gets the brain's single slot (llama-server runs ``--parallel 1``).

Interactive chat always goes first.

* **Background callers** (auto_learn, the meal planner, the self-investigator)
  keep their own bounded "wait for idle chat" loops. Each one also asks
  :func:`coder_busy`, so a coder-gate request in flight counts as busy for them.
* **The coder gate** (``coder_gate.py``) is the politest caller:
  - it starts a request only once chat has been idle for ``CODER_IDLE_SECONDS``
    (:func:`wait_for_coder_turn`), which stops it from slipping in between two
    turns of the same conversation;
  - it runs one request at a time;
  - it aborts that request the moment chat starts.

  llama.cpp frees the slot ~0.2 s after a client disconnects, and its
  ``--cache-ram`` brings Jess's cached prompt back after a coder prompt in
  ~0.3 s (both measured 2026-11-20, ``docs/internal/CODER_SPIKE.md`` §3).

For the coder gate, "chat busy" (:func:`chat_busy`) means any of:

* ``bgw_active_requests`` > 0: chat turns, and streamed ones until their stream
  ends;
* a ``look_it_up`` lookup is running;
* an :func:`interactive_call` is in flight, such as a vision request for a
  meal, document or pantry photo, which doesn't count as a chat turn.

The background waiters keep their own narrower checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Iterator

from orchestrator.metrics import ACTIVE_REQUESTS

_last_chat_activity: float = 0.0  # time.monotonic() of the last chat start/end; 0 = none yet
_coder_in_flight = 0
_interactive_in_flight = 0


def note_chat_activity() -> None:
    """Record that a chat turn just started or ended (cloud_brain calls this)."""
    global _last_chat_activity
    _last_chat_activity = time.monotonic()


def chat_busy() -> bool:
    """True while a chat turn, a lookup or an interactive call uses the brain."""
    if _interactive_in_flight > 0:
        return True
    try:
        if ACTIVE_REQUESTS._value.get() > 0:
            return True
    except Exception:  # noqa: BLE001 — prometheus internals; treat as idle
        pass
    from orchestrator.research_helper import lookup_active

    return lookup_active()


def coder_busy() -> bool:
    """True while a coder-gate request is being answered by the brain."""
    return _coder_in_flight > 0


def seconds_since_chat() -> float:
    """Seconds since the last chat turn started or ended (inf if none yet)."""
    if not _last_chat_activity:
        return float("inf")
    return time.monotonic() - _last_chat_activity


@contextlib.contextmanager
def interactive_call() -> Iterator[None]:
    """Mark a non-chat brain call someone is waiting on (e.g. a photo)."""
    global _interactive_in_flight
    _interactive_in_flight += 1
    note_chat_activity()
    try:
        yield
    finally:
        _interactive_in_flight -= 1
        note_chat_activity()


@contextlib.contextmanager
def coder_request() -> Iterator[None]:
    """Mark a coder-gate request as in flight for the duration of the block."""
    global _coder_in_flight
    _coder_in_flight += 1
    try:
        yield
    finally:
        _coder_in_flight -= 1


async def wait_for_coder_turn(idle_s: float, max_wait_s: float, poll_s: float = 0.5) -> bool:
    """Wait until chat is idle and has been for ``idle_s`` seconds.

    Returns True when the coder may go, False if ``max_wait_s`` ran out first.
    Busy moments seen while polling count as chat activity too, so a turn that
    started and ended between two polls still resets the idle clock.
    """
    deadline = time.monotonic() + max(0.0, max_wait_s)
    while True:
        if chat_busy():
            note_chat_activity()
        elif seconds_since_chat() >= idle_s:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)
