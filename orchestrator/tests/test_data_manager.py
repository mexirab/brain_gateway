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
