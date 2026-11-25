"""
Tests for focus_manager.py — F-004 Body Doubling & Focus Sessions.

Covers backward compatibility, multi-sprint sessions, audio source routing,
check-ins, sprint transitions, session summaries, and input validation.

External deps (ha_client, pihole, scheduler, _announce_voice, state_store)
are mocked by the shared ``selfcare_focus`` fixture (fixtures_selfcare.py).
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@contextmanager
def _first_choice():
    """Pin focus_manager.random.choice to the first option (deterministic summary text)."""
    with patch("orchestrator.focus_manager.random") as rand:
        rand.choice = lambda x: x[0]
        yield


# ---------------------------------------------------------------------------
# 1. tool_start_focus backward compat
# ---------------------------------------------------------------------------


class TestStartFocusBackwardCompat:
    async def test_legacy_call_no_new_params(self, selfcare_focus):
        """Existing calls with no F-004 params still work, and the reply names task + duration."""
        fm = selfcare_focus["fm"]
        result = await fm.tool_start_focus(task="Write report", duration=25)

        assert "Focus session started" in result
        assert "Write report" in result
        assert "25 minutes" in result
        session = selfcare_focus["session"]
        assert session["active"] is True
        assert session["task"] == "Write report"
        assert session["duration"] == 25
        # Legacy single sprint: sprints_planned should be None
        assert session["sprints_planned"] is None
        assert session["sprint_count"] == 0

    async def test_already_active_returns_warning(self, selfcare_focus):
        """Starting a session while one is active returns a warning."""
        session = selfcare_focus["session"]
        session["active"] = True
        session["task"] = "Existing task"
        session["started"] = datetime.now()
        session["duration"] = 25

        fm = selfcare_focus["fm"]
        result = await fm.tool_start_focus(task="New task")
        assert "already focusing" in result.lower()


# ---------------------------------------------------------------------------
# 2. tool_start_focus with sprints
# ---------------------------------------------------------------------------


class TestStartFocusWithSprints:
    async def test_multi_sprint_sets_planned(self, selfcare_focus):
        """sprints > 1 sets sprints_planned in session and the reply mentions the count."""
        fm = selfcare_focus["fm"]
        result = await fm.tool_start_focus(task="Study", duration=25, sprints=3)

        session = selfcare_focus["session"]
        assert session["sprints_planned"] == 3
        assert session["active"] is True
        assert "3 sprints" in result

    async def test_check_in_job_scheduled(self, selfcare_focus):
        """Check-in interval job is scheduled when check_ins=True."""
        fm = selfcare_focus["fm"]
        await fm.tool_start_focus(task="Code", duration=25, check_ins=True, check_in_interval=10)

        session = selfcare_focus["session"]
        assert session["check_in_interval"] == 10
        assert session["check_in_job_id"] is not None
        # Scheduler should have been called for both break job and check-in job
        assert selfcare_focus["scheduler"].add_job.call_count == 2

    async def test_check_ins_disabled(self, selfcare_focus):
        """check_ins=False means no check-in job."""
        fm = selfcare_focus["fm"]
        await fm.tool_start_focus(task="Code", duration=25, check_ins=False)

        session = selfcare_focus["session"]
        assert session["check_in_interval"] is None
        assert session["check_in_job_id"] is None
        # Only the break job should be scheduled
        assert selfcare_focus["scheduler"].add_job.call_count == 1


# ---------------------------------------------------------------------------
# 3. tool_start_focus with audio sources
# ---------------------------------------------------------------------------


class TestStartFocusAudioSources:
    async def test_lofi_audio(self, selfcare_focus):
        """audio='lofi' starts stream URL."""
        fm = selfcare_focus["fm"]
        with patch.object(fm, "start_focus_stream_audio", new_callable=AsyncMock, return_value=True) as mock_stream:
            result = await fm.tool_start_focus(task="Draw", audio="lofi")
            mock_stream.assert_called_once_with("http://lofi.stream/listen", "media_player.office")
            assert "lo-fi" in result

    async def test_coffee_shop_audio(self, selfcare_focus):
        """audio='coffee_shop' starts coffee stream URL."""
        fm = selfcare_focus["fm"]
        with patch.object(fm, "start_focus_stream_audio", new_callable=AsyncMock, return_value=True) as mock_stream:
            result = await fm.tool_start_focus(task="Write", audio="coffee_shop")
            mock_stream.assert_called_once_with("http://coffee.stream/listen", "media_player.office")
            assert "coffee shop" in result

    async def test_silence_audio(self, selfcare_focus):
        """audio='silence' skips audio start entirely."""
        fm = selfcare_focus["fm"]
        with (
            patch.object(fm, "start_focus_stream_audio", new_callable=AsyncMock) as mock_stream,
            patch.object(fm, "start_focus_audio", new_callable=AsyncMock) as mock_endel,
        ):
            await fm.tool_start_focus(task="Meditate", audio="silence")
            mock_stream.assert_not_called()
            mock_endel.assert_not_called()

        session = selfcare_focus["session"]
        assert session["audio_source"] == "silence"

    async def test_endel_audio_default(self, selfcare_focus):
        """No audio param defaults to endel."""
        fm = selfcare_focus["fm"]
        with patch.object(fm, "start_focus_audio", new_callable=AsyncMock, return_value=True) as mock_endel:
            await fm.tool_start_focus(task="Focus", duration=25)
            mock_endel.assert_called_once()

        session = selfcare_focus["session"]
        assert session["audio_source"] == "endel"


# ---------------------------------------------------------------------------
# 4. deliver_check_in
# ---------------------------------------------------------------------------


class TestDeliverCheckIn:
    async def test_check_in_with_task(self, selfcare_focus):
        """Check-in announces task description via TTS."""
        session = selfcare_focus["session"]
        session["active"] = True
        session["task_description"] = "writing documentation"

        fm = selfcare_focus["fm"]
        await fm.deliver_check_in()

        selfcare_focus["announce"].assert_called_once()
        msg = selfcare_focus["announce"].call_args[0][0]
        assert "writing documentation" in msg

    async def test_check_in_no_task(self, selfcare_focus):
        """Check-in with no task_description uses generic message."""
        session = selfcare_focus["session"]
        session["active"] = True
        session["task_description"] = None
        session["task"] = None

        fm = selfcare_focus["fm"]
        await fm.deliver_check_in()

        selfcare_focus["announce"].assert_called_once()
        msg = selfcare_focus["announce"].call_args[0][0]
        assert "zone" in msg.lower() or "going" in msg.lower()

    async def test_check_in_inactive_noop(self, selfcare_focus):
        """Check-in does nothing when session is inactive."""
        session = selfcare_focus["session"]
        session["active"] = False

        fm = selfcare_focus["fm"]
        await fm.deliver_check_in()

        selfcare_focus["announce"].assert_not_called()


# ---------------------------------------------------------------------------
# 5-7. deliver_focus_break: more sprints left / all done / legacy single sprint
# ---------------------------------------------------------------------------


class TestDeliverFocusBreak:
    async def test_mid_session_break_counts_sprint_and_stays_active(self, selfcare_focus):
        """A sprint ends with more planned: sprint_count +1, minutes accumulate,
        the session stays active through the break, the break job id is cleared."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "duration": 25,
                "sprint_count": 1,
                "sprints_planned": 4,
                "total_focus_minutes": 25,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        await selfcare_focus["fm"].deliver_focus_break("Study", 5)

        assert session["sprint_count"] == 2
        assert session["total_focus_minutes"] == 50
        assert session["active"] is True
        assert session["job_id"] is None

    async def test_all_sprints_complete_builds_summary_and_resets(self, selfcare_focus):
        """All sprints complete: summary announced, session reset."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "task_description": "Study math",
                "duration": 25,
                "sprint_count": 2,  # will become 3 after increment
                "sprints_planned": 3,
                "total_focus_minutes": 50,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        with _first_choice():
            await selfcare_focus["fm"].deliver_focus_break("Study", 5)

        assert session["active"] is False
        selfcare_focus["announce"].assert_called_once()
        msg = selfcare_focus["announce"].call_args[0][0]
        assert "Session complete" in msg
        assert "75" in msg  # 50 + 25 = 75 minutes total

    async def test_legacy_single_sprint_resets_fully(self, selfcare_focus):
        """Legacy single-sprint (sprints_planned=None) resets session fully."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Write report",
                "duration": 25,
                "sprint_count": 0,
                "sprints_planned": None,
                "total_focus_minutes": 0,
                "audio_player": None,
                "block_sites": False,
                "check_in_job_id": None,
            }
        )

        await selfcare_focus["fm"].deliver_focus_break("Write report", 5)

        assert session["active"] is False
        assert session["task"] is None


# ---------------------------------------------------------------------------
# 8. tool_focus_sprint next_sprint
# ---------------------------------------------------------------------------


class TestFocusSprintNextSprint:
    async def test_next_sprint_rearms_timer(self, selfcare_focus):
        """next_sprint re-arms break timer and audio."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "duration": 25,
                "break_duration": 5,
                "sprint_count": 1,
                "sprints_planned": 3,
                "check_in_interval": 10,
                "audio_player": "media_player.office",
                "audio_source": "lofi",
                "block_sites": True,
                "job_id": None,
                "check_in_job_id": None,
            }
        )

        fm = selfcare_focus["fm"]
        with patch.object(fm, "_start_audio_for_source", new_callable=AsyncMock, return_value=True):
            result = await fm.tool_focus_sprint(action="next_sprint")

        assert "Sprint 2" in result
        assert "25 minutes" in result
        # Scheduler should have break job + check-in job
        assert selfcare_focus["scheduler"].add_job.call_count >= 2
        # Session should have new job_id
        assert session["job_id"] is not None

    async def test_next_sprint_no_active_session(self, selfcare_focus):
        """next_sprint with no active session returns error."""
        result = await selfcare_focus["fm"].tool_focus_sprint(action="next_sprint")
        assert "No active" in result


# ---------------------------------------------------------------------------
# 9. tool_focus_sprint extend
# ---------------------------------------------------------------------------


class TestFocusSprintExtend:
    @pytest.mark.parametrize(
        ("minutes", "added"),
        [pytest.param(15, 15, id="15-min"), pytest.param(None, 10, id="default-10")],
    )
    async def test_extend_reschedules(self, selfcare_focus, minutes, added):
        """extend reschedules the break job forward (default +10 minutes)."""
        session = selfcare_focus["session"]
        session.update({"active": True, "task": "Code", "duration": 25, "job_id": "focus_123"})

        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now() + timedelta(minutes=10)
        selfcare_focus["scheduler"].get_job = MagicMock(return_value=mock_job)

        fm = selfcare_focus["fm"]
        if minutes is None:
            result = await fm.tool_focus_sprint(action="extend")
        else:
            result = await fm.tool_focus_sprint(action="extend", duration_minutes=minutes)

        assert f"{added} minutes" in result
        selfcare_focus["scheduler"].reschedule_job.assert_called_once()
        assert session["duration"] == 25 + added

    async def test_extend_no_job_found(self, selfcare_focus):
        """extend when scheduler job is gone returns error."""
        session = selfcare_focus["session"]
        session.update({"active": True, "task": "Code", "duration": 25, "job_id": "focus_123"})
        selfcare_focus["scheduler"].get_job = MagicMock(return_value=None)

        result = await selfcare_focus["fm"].tool_focus_sprint(action="extend")
        assert "not found" in result.lower() or "ended" in result.lower()


# ---------------------------------------------------------------------------
# 10. tool_focus_sprint end_session
# ---------------------------------------------------------------------------


class TestFocusSprintEndSession:
    async def test_end_session_builds_summary(self, selfcare_focus):
        """end_session builds summary and resets."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Code",
                "task_description": "refactoring",
                "duration": 25,
                "sprint_count": 2,
                "sprints_planned": 4,
                "total_focus_minutes": 50,
                "audio_player": "media_player.office",
                "block_sites": True,
                "job_id": "focus_123",
                "check_in_job_id": "checkin_123",
            }
        )

        with _first_choice():
            result = await selfcare_focus["fm"].tool_focus_sprint(action="end_session")

        assert "Session complete" in result
        assert session["active"] is False
        selfcare_focus["announce"].assert_called_once()

    async def test_end_session_unknown_action(self, selfcare_focus):
        """Unknown action returns error."""
        selfcare_focus["session"]["active"] = True
        result = await selfcare_focus["fm"].tool_focus_sprint(action="invalid_action")
        assert "Unknown action" in result


# ---------------------------------------------------------------------------
# 11. tool_stop_focus with sprints
# ---------------------------------------------------------------------------


class TestStopFocusWithSprints:
    async def test_stop_reports_total_across_sprints(self, selfcare_focus):
        """Stopping mid-session reports total across all completed sprints."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Study",
                "started": datetime.now() - timedelta(minutes=10),
                "duration": 25,
                "sprint_count": 2,
                "sprints_planned": 4,
                "total_focus_minutes": 50,
                "audio_player": None,
                "block_sites": False,
                "job_id": "focus_123",
                "check_in_job_id": None,
            }
        )

        result = await selfcare_focus["fm"].tool_stop_focus()

        assert "Study" in result
        assert "2 completed sprints" in result  # sprint_count=2 completed
        # Total should include accumulated + current elapsed
        assert "total" in result.lower()

    async def test_stop_no_active_session(self, selfcare_focus):
        """Stopping with no active session returns message."""
        result = await selfcare_focus["fm"].tool_stop_focus()
        assert "No focus timer" in result

    async def test_stop_legacy_single_sprint(self, selfcare_focus):
        """Legacy single sprint stop doesn't mention sprint count."""
        session = selfcare_focus["session"]
        session.update(
            {
                "active": True,
                "task": "Write",
                "started": datetime.now() - timedelta(minutes=15),
                "duration": 25,
                "sprint_count": 0,
                "sprints_planned": None,
                "total_focus_minutes": 0,
                "audio_player": None,
                "block_sites": False,
                "job_id": "focus_123",
                "check_in_job_id": None,
            }
        )

        result = await selfcare_focus["fm"].tool_stop_focus()

        assert "Write" in result
        assert "sprint" not in result.lower()


# ---------------------------------------------------------------------------
# 12. _build_session_summary
# ---------------------------------------------------------------------------


class TestBuildSessionSummary:
    @pytest.mark.parametrize(
        ("description", "task", "sprints", "minutes", "expected", "absent"),
        [
            pytest.param("writing tests", "writing tests", 1, 25, ["25 minutes", "writing tests"], "sprints", id="one"),
            pytest.param("studying", "studying", 3, 75, ["75 minutes", "3 sprints", "studying"], None, id="multi"),
            pytest.param(None, "fallback task", 1, 30, ["fallback task"], None, id="task-fallback"),
        ],
    )
    def test_summary(self, selfcare_focus, description, task, sprints, minutes, expected, absent):
        """Single sprint omits the sprint count; multi-sprint says it; no
        task_description falls back to the task name."""
        selfcare_focus["session"].update(
            {
                "task_description": description,
                "task": task,
                "sprint_count": sprints,
                "total_focus_minutes": minutes,
            }
        )

        with _first_choice():
            result = selfcare_focus["fm"]._build_session_summary()

        assert "Session complete" in result
        for text in expected:
            assert text in result
        if absent:
            assert absent not in result


# ---------------------------------------------------------------------------
# 13. Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.parametrize(
        ("kwargs", "key", "expected"),
        [
            pytest.param({"sprints": 0}, "sprints_planned", None, id="sprints-min"),  # 1 sprint = legacy single
            pytest.param({"sprints": 20}, "sprints_planned", 10, id="sprints-max"),
            pytest.param({"check_ins": True, "check_in_interval": 2}, "check_in_interval", 5, id="checkin-min"),
            pytest.param({"check_ins": True, "check_in_interval": 200}, "check_in_interval", 120, id="checkin-max"),
        ],
    )
    async def test_clamped(self, selfcare_focus, kwargs, key, expected):
        """sprints is clamped to 1..10, check_in_interval to 5..120."""
        await selfcare_focus["fm"].tool_start_focus(task="Test", **kwargs)
        assert selfcare_focus["session"][key] == expected

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param({"duration": "abc"}, "number", id="duration-text"),
            pytest.param({"duration": None}, "number", id="duration-none"),
            pytest.param({"duration": 0}, "between 1 and 480", id="duration-0"),
            pytest.param({"duration": -1}, "between 1 and 480", id="duration-negative"),
            pytest.param({"duration": 481}, "between 1 and 480", id="duration-481"),
            pytest.param({"duration": 500}, "between 1 and 480", id="duration-500"),
            pytest.param({"break_duration": 0}, "between 1 and 60", id="break-0"),
            pytest.param({"break_duration": -5}, "between 1 and 60", id="break-negative"),
            pytest.param({"break_duration": 61}, "between 1 and 60", id="break-61"),
        ],
    )
    async def test_bad_durations_rejected(self, selfcare_focus, kwargs, expected):
        """Out-of-range or non-numeric focus/break durations start nothing."""
        result = await selfcare_focus["fm"].tool_start_focus(task="Test", **kwargs)
        assert expected in result.lower()
        assert selfcare_focus["session"]["active"] is False
        selfcare_focus["scheduler"].add_job.assert_not_called()

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"duration": 1}, id="duration-1"),
            pytest.param({"duration": 480}, id="duration-480"),
            pytest.param({"break_duration": 1}, id="break-1"),
            pytest.param({"break_duration": 60}, id="break-60"),
        ],
    )
    async def test_boundary_durations_accepted(self, selfcare_focus, kwargs):
        await selfcare_focus["fm"].tool_start_focus(task="Test", audio="silence", **kwargs)
        assert selfcare_focus["session"]["active"] is True
