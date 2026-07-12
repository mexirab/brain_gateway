"""
Tests for orchestrator/data_manager.py — YAML save paths.

Covers the atomic-write regression: save_medications / save_projects used to
open(w) + yaml.dump directly, so a crash mid-write corrupted the YAML and
get_medications() then returned {} (silently killing all med nudges). Both
now write through config_writer.atomic_write_yaml.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml


def _meds_payload() -> dict:
    return {
        "daily": {
            "morning": [{"name": "Vyvanse", "dose": "30mg", "purpose": "ADHD", "notes": ""}],
            "evening": [],
        },
        "weekly": [],
        "as_needed": [],
    }


def _point_paths_at_tmp(monkeypatch, tmp_path: Path) -> dict:
    from orchestrator import data_manager

    paths = {
        "meds_yaml": tmp_path / "medications.yaml",
        "meds_md": tmp_path / "medications.md",
        "projects_yaml": tmp_path / "projects.yaml",
        "projects_md": tmp_path / "current.md",
    }
    monkeypatch.setattr(data_manager, "MEDICATIONS_YAML", str(paths["meds_yaml"]))
    monkeypatch.setattr(data_manager, "MEDICATIONS_MD", str(paths["meds_md"]))
    monkeypatch.setattr(data_manager, "PROJECTS_YAML", str(paths["projects_yaml"]))
    monkeypatch.setattr(data_manager, "PROJECTS_MD", str(paths["projects_md"]))
    return paths


def test_save_medications_round_trip(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    paths = _point_paths_at_tmp(monkeypatch, tmp_path)
    payload = _meds_payload()

    assert data_manager.save_medications(payload) is True
    with open(paths["meds_yaml"]) as f:
        assert yaml.safe_load(f) == payload
    assert data_manager.get_medications() == payload
    # RAG shadow markdown is OFF by default now — the model reads the YAML
    # directly (get_data + prompt inject), so the derived .md must NOT be
    # written into the RAG source tree where auto_learn/reindex could shadow it.
    assert not paths["meds_md"].exists()


def test_save_medications_generates_md_when_flag_enabled(monkeypatch, tmp_path: Path):
    """Back-compat escape hatch: GENERATE_RAG_STRUCTURED_DOCS=true restores the
    derived markdown."""
    from orchestrator import data_manager

    paths = _point_paths_at_tmp(monkeypatch, tmp_path)
    monkeypatch.setenv("GENERATE_RAG_STRUCTURED_DOCS", "true")
    assert data_manager.save_medications(_meds_payload()) is True
    assert paths["meds_md"].exists()


def test_save_medications_failure_leaves_previous_file_intact(monkeypatch, tmp_path: Path):
    """Simulated crash mid-write: the old open('w') truncated the file first,
    so a dump failure left a corrupt/empty medications.yaml behind. With the
    atomic writer the original content must survive untouched."""
    from orchestrator import data_manager

    paths = _point_paths_at_tmp(monkeypatch, tmp_path)
    good = _meds_payload()
    assert data_manager.save_medications(good) is True

    # os.replace blowing up mid-save simulates the crash window.
    with patch("orchestrator.config_writer.os.replace", side_effect=OSError("crash")):
        assert data_manager.save_medications({"daily": {"morning": [], "evening": []}}) is False

    # Original file is intact and still parses to the previous payload.
    with open(paths["meds_yaml"]) as f:
        assert yaml.safe_load(f) == good
    assert data_manager.get_medications() == good
    # No stray tmpfiles left behind
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_save_projects_round_trip_and_atomicity(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    paths = _point_paths_at_tmp(monkeypatch, tmp_path)
    payload = {"active": [{"name": "Garage", "status": "in_progress", "priority": "high", "goal": "clean"}]}

    assert data_manager.save_projects(payload) is True
    with open(paths["projects_yaml"]) as f:
        assert yaml.safe_load(f) == payload

    with patch("orchestrator.config_writer.os.replace", side_effect=OSError("crash")):
        assert data_manager.save_projects({"active": []}) is False
    assert data_manager.get_projects() == payload


# ---------------------------------------------------------------------------
# Read path (get_data tool + prompt-inject block) — the single source of truth
# ---------------------------------------------------------------------------


def test_handle_get_data_medications_renders(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    out = data_manager.handle_get_data("medications")
    assert "source of truth" in out.lower()
    assert "Morning: Vyvanse 30mg" in out
    assert "Evening: (none)" in out


def test_handle_get_data_unknown_kind(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert "Unknown kind" in data_manager.handle_get_data("bogus")


def test_structured_facts_block_renders_and_never_raises(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    # No files on disk → get_medications/get_projects return defaults; the block
    # must still render (it rides every prompt and must never sink it).
    _point_paths_at_tmp(monkeypatch, tmp_path)
    block = data_manager.get_structured_facts_block()
    assert "MEDICATIONS" in block
    assert "ACTIVE PROJECTS" in block

    # And with real data it reflects the YAML.
    assert data_manager.save_medications(_meds_payload()) is True
    block = data_manager.get_structured_facts_block()
    assert "Vyvanse 30mg" in block


def test_bad_projects_entry_does_not_blank_meds(monkeypatch, tmp_path: Path):
    """A malformed (bare-string) active project must not raise and blank the
    medications half of the injected block — meds are safety-critical and render
    under a separate guard."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    assert data_manager.save_projects({"active": ["oops-a-bare-string", {"name": "RealProj", "priority": "high"}]})

    block = data_manager.get_structured_facts_block()
    assert "Vyvanse 30mg" in block  # meds still present despite the bad project entry
    assert "RealProj" in block  # the valid dict project still renders


# ---------------------------------------------------------------------------
# update_medication — honesty (Defect B) + real schedule relocation
# ---------------------------------------------------------------------------


def test_update_medication_noop_is_honest_and_does_not_write(monkeypatch, tmp_path: Path):
    """A found med with no changed field must report 'nothing to update' and
    must NOT write — the old code wrote an unchanged dict and returned the
    hollow 'Updated Vyvanse: .' which the model relayed as done."""
    from orchestrator import data_manager

    paths = _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    mtime_before = paths["meds_yaml"].stat().st_mtime_ns

    with patch.object(data_manager, "save_medications", wraps=data_manager.save_medications) as spy:
        msg = data_manager.update_medication("Vyvanse")  # no fields changed
    assert "nothing to update" in msg.lower()
    assert msg != "Updated Vyvanse: ."
    spy.assert_not_called()  # no pointless write + audit entry
    assert paths["meds_yaml"].stat().st_mtime_ns == mtime_before


def test_update_medication_real_change_reports_field(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    msg = data_manager.update_medication("Vyvanse", dose="40mg")
    assert msg == "Updated Vyvanse: dose=40mg."
    assert data_manager.get_medications()["daily"]["morning"][0]["dose"] == "40mg"


def test_update_medication_relocates_between_buckets(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    msg = data_manager.update_medication("Vyvanse", schedule="evening")
    assert "schedule=evening" in msg
    data = data_manager.get_medications()
    assert [m["name"] for m in data["daily"]["morning"]] == []
    assert [m["name"] for m in data["daily"]["evening"]] == ["Vyvanse"]


def test_update_medication_same_schedule_is_noop(monkeypatch, tmp_path: Path):
    """Requesting the schedule the med already sits in is not a change."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    msg = data_manager.update_medication("Vyvanse", schedule="morning")
    assert "nothing to update" in msg.lower()


def test_update_medication_not_found(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    assert "not found" in data_manager.update_medication("Nonexistent", dose="1mg").lower()


def test_update_medication_bad_schedule_rejected(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    assert "Unknown schedule" in data_manager.update_medication("Vyvanse", schedule="lunchtime")


# ---------------------------------------------------------------------------
# days / skip_weekends — model-facing weekday scheduling (Defect A, write side)
# ---------------------------------------------------------------------------


def test_normalize_days_variants():
    from orchestrator.data_manager import normalize_days

    assert normalize_days(None, None) is None
    assert normalize_days(None, True) == ["mon", "tue", "wed", "thu", "fri"]
    assert normalize_days(None, False) is None
    # explicit days win over skip_weekends, canonical order + de-dup + normalize
    assert normalize_days(["sat", "SUN", "sun"], True) == ["sat", "sun"]
    assert normalize_days(["Friday", "monday"], None) == ["mon", "fri"]
    # unknown tokens dropped; all-unknown → None (not an empty list)
    assert normalize_days(["bogus", "xyz"], None) is None


def test_handle_update_data_skip_weekends_writes_days(monkeypatch, tmp_path: Path):
    """End-to-end write path: 'stop reminding me on weekends' → the Vyvanse
    entry gains days=[mon..fri]."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    msg = data_manager.handle_update_data("update_medication", "Vyvanse", skip_weekends=True)
    assert "days=mon,tue,wed,thu,fri" in msg
    assert data_manager.get_medications()["daily"]["morning"][0]["days"] == ["mon", "tue", "wed", "thu", "fri"]


def test_normalize_days_clear_sentinel():
    """Explicit empty list = CLEAR_DAYS sentinel; absent = None; all-unknown =
    None (a typo must not be mistaken for a clear)."""
    from orchestrator.data_manager import CLEAR_DAYS, normalize_days

    assert normalize_days([], None) is CLEAR_DAYS
    assert normalize_days(None, None) is None
    assert normalize_days(["bogus"], None) is None  # typo, not a clear


def test_handle_update_data_clear_days_restores_every_day(monkeypatch, tmp_path: Path):
    """'Take Vyvanse every day again' (days=[]) must actually drop the restriction
    — not silently keep the med suppressed on weekends (inverse silent-failure)."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    payload = {
        "daily": {"morning": [{"name": "Vyvanse", "dose": "30mg", "days": ["mon", "tue", "wed", "thu", "fri"]}], "evening": []},
        "weekly": [],
        "as_needed": [],
    }
    assert data_manager.save_medications(payload) is True
    msg = data_manager.handle_update_data("update_medication", "Vyvanse", days=[])
    assert "every day" in msg.lower()
    assert "days" not in data_manager.get_medications()["daily"]["morning"][0]


def test_clear_days_on_unrestricted_med_is_honest_noop(monkeypatch, tmp_path: Path):
    """Clearing a med that has no restriction is nothing to do — honest no-op."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True  # Vyvanse has no days
    msg = data_manager.handle_update_data("update_medication", "Vyvanse", days=[])
    assert "nothing to update" in msg.lower()


def test_handle_add_medication_with_explicit_days(monkeypatch, tmp_path: Path):
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    data_manager.handle_update_data("add_medication", "Ritalin", dose="10mg", days=["mon", "wed", "fri"])
    added = data_manager.get_medications()["daily"]["morning"][-1]
    assert added["name"] == "Ritalin"
    assert added["days"] == ["mon", "wed", "fri"]


def test_add_medication_without_days_has_no_days_key(monkeypatch, tmp_path: Path):
    """Backward compat: a plain add must not sprout an empty days field."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    assert data_manager.save_medications(_meds_payload()) is True
    data_manager.handle_update_data("add_medication", "Zoloft", dose="50mg")
    added = data_manager.get_medications()["daily"]["morning"][-1]
    assert "days" not in added


# ---------------------------------------------------------------------------
# Slice 4 — schedule hint surfaces in the authoritative text (get_data + prompt)
# ---------------------------------------------------------------------------


def test_fmt_days_labels():
    from orchestrator.data_manager import _fmt_days

    assert _fmt_days(["mon", "tue", "wed", "thu", "fri"]) == "Mon–Fri"
    assert _fmt_days(["sat", "sun"]) == "weekends"
    assert _fmt_days(["mon", "wed", "fri"]) == "Mon/Wed/Fri"
    assert _fmt_days(["SUN", "sat"]) == "weekends"  # order-independent + case
    assert _fmt_days([]) == ""
    assert _fmt_days(None) == ""
    assert _fmt_days("friday") == ""  # non-list → no hint (fail safe)


def test_get_data_reflects_weekends_off(monkeypatch, tmp_path: Path):
    """The whole point: a weekends-off med must read as (Mon–Fri), not plain
    daily, in the text the model answers 'when do I take X?' from."""
    from orchestrator import data_manager

    _point_paths_at_tmp(monkeypatch, tmp_path)
    payload = {
        "daily": {
            "morning": [{"name": "Vyvanse", "dose": "30mg", "days": ["mon", "tue", "wed", "thu", "fri"]}],
            "evening": [],
        },
        "weekly": [],
        "as_needed": [],
    }
    assert data_manager.save_medications(payload) is True
    out = data_manager.handle_get_data("medications")
    assert "Vyvanse 30mg (Mon–Fri)" in out
    # and the same hint rides the injected prompt block
    assert "Vyvanse 30mg (Mon–Fri)" in data_manager.get_structured_facts_block()
