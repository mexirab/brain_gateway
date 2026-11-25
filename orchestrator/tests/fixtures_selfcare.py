"""Shared pytest fixtures for the selfcare test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

Orchestrator modules are imported inside the fixtures, never at module level:
conftest imports this file before any test runs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

__all__: list[str] = ["selfcare_focus", "selfcare_fresh_state"]


def router_client(router):
    """A TestClient over one APIRouter on a bare FastAPI app (no middleware, so no bearer)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def selfcare_fresh_state():
    """Fresh selfcare_manager module state (self-care timers + phone-movement
    baselines) before and after the test, so tests can't see each other's logs.
    Returns the selfcare_manager module."""
    from orchestrator import selfcare_manager as sm

    sm._state = sm.SelfCareState()
    sm._health = sm._HealthMovementState()
    yield sm
    sm._state = sm.SelfCareState()
    sm._health = sm._HealthMovementState()


@pytest.fixture
def selfcare_focus():
    """focus_manager with every external dependency mocked and a fresh, inactive
    session dict (the FocusSession defaults).

    Yields a dict: ``fm`` (the module), ``session``, ``pihole`` (enable/disable
    return a plain success — not a confirmed block), ``ha``, ``scheduler``,
    ``state_store``, ``announce`` and ``toggles`` (the PIHOLE_BLOCKING_TOGGLES mock).
    """
    from orchestrator import focus_manager, shared
    from orchestrator.focus_state import FocusSession
    from orchestrator.pihole_client import PiHoleResult

    session = FocusSession().to_dict()
    original_session = shared.current_focus_session.to_dict()

    pihole = AsyncMock()
    pihole.enable_focus_blocking = AsyncMock(return_value=PiHoleResult(True, "ok"))
    pihole.disable_focus_blocking = AsyncMock(return_value=PiHoleResult(True, "ok"))
    ha = AsyncMock()
    ha.call_service = AsyncMock(return_value=MagicMock(success=True, message="ok"))
    scheduler = MagicMock()
    scheduler.get_job.return_value = None
    store = MagicMock()
    announce = AsyncMock()
    toggles = MagicMock()

    with (
        patch.object(shared, "current_focus_session", session),
        patch.object(shared, "ha_client", ha),
        patch.object(shared, "scheduler", scheduler),
        patch.multiple(
            focus_manager,
            ha_client=ha,
            scheduler=scheduler,
            get_pihole_client=MagicMock(return_value=pihole),
            _announce_voice=announce,
            state_store=store,
            current_focus_session=session,
            FOCUS_AUDIO_PLAYER="media_player.office",
            FOCUS_AUDIO_LOFI_URL="http://lofi.stream/listen",
            FOCUS_AUDIO_COFFEE_URL="http://coffee.stream/listen",
            ENDEL_ENABLED=True,
            FOCUS_SESSIONS_STARTED=MagicMock(),
            FOCUS_SESSIONS_COMPLETED=MagicMock(),
            FOCUS_SESSIONS_STOPPED_EARLY=MagicMock(),
            FOCUS_SESSION_DURATION=MagicMock(),
            FOCUS_ACTIVE=MagicMock(),
            PIHOLE_BLOCKING_TOGGLES=toggles,
        ),
    ):
        yield {
            "fm": focus_manager,
            "session": session,
            "pihole": pihole,
            "ha": ha,
            "scheduler": scheduler,
            "state_store": store,
            "announce": announce,
            "toggles": toggles,
        }

    shared.current_focus_session.update(original_session)
