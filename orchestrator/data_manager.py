"""
Data Manager for Brain Gateway
Handles structured YAML data (medications, projects) with auto-markdown generation.
"""

import copy
import logging
import os
from datetime import datetime
from typing import Any, Dict, Tuple

import yaml

from orchestrator.config_writer import atomic_write_yaml, log_config_change

logger = logging.getLogger(__name__)


def _rag_docs_enabled() -> bool:
    """Whether to (re)generate the medications.md / current.md RAG shadow docs.

    Default OFF: the model now reads the YAML directly (get_data + prompt
    inject), so the derived markdown no longer needs to be a RAG source — and
    keeping it created a stale, auto_learn-pollutable second copy of the meds.
    Flip GENERATE_RAG_STRUCTURED_DOCS=true only to restore the old behavior.
    """
    return os.environ.get("GENERATE_RAG_STRUCTURED_DOCS", "false").lower() in ("1", "true", "yes")


# Paths - configurable via environment
RAG_BASE = os.environ.get("RAG_BASE", "/app/data/rag")
MEDICATIONS_YAML = os.path.join(RAG_BASE, "10_profile", "medications.yaml")
MEDICATIONS_MD = os.path.join(RAG_BASE, "10_profile", "medications.md")
PROJECTS_YAML = os.path.join(RAG_BASE, "30_projects", "projects.yaml")
PROJECTS_MD = os.path.join(RAG_BASE, "30_projects", "current.md")

# mtime-keyed YAML cache — every mutation previously re-read and re-parsed
# the full file from disk. path -> (mtime, parsed data)
_yaml_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _load_yaml_cached(path: str) -> Dict[str, Any]:
    """Return parsed YAML, re-reading from disk only when the mtime changes.

    Raises FileNotFoundError like open() so callers keep their defaults.
    Returns a deep copy so callers can mutate freely without corrupting
    the cache (mutation-then-save is the normal flow here).
    """
    mtime = os.stat(path).st_mtime
    cached = _yaml_cache.get(path)
    if cached is None or cached[0] != mtime:
        with open(path) as f:
            _yaml_cache[path] = (mtime, yaml.safe_load(f) or {})
    return copy.deepcopy(_yaml_cache[path][1])


def _store_yaml_cache(path: str, data: Dict[str, Any]) -> None:
    """Refresh the cache after a successful write."""
    try:
        _yaml_cache[path] = (os.stat(path).st_mtime, copy.deepcopy(data))
    except OSError:
        _yaml_cache.pop(path, None)


# =============================================================================
# MEDICATIONS
# =============================================================================


def get_medications() -> Dict[str, Any]:
    """Load medications from YAML."""
    try:
        return _load_yaml_cached(MEDICATIONS_YAML)
    except FileNotFoundError:
        logger.warning(f"Medications file not found: {MEDICATIONS_YAML}")
        return {"daily": {"morning": [], "evening": []}, "weekly": [], "as_needed": []}
    except Exception as e:
        logger.error(f"Error loading medications: {e}")
        return {}


def save_medications(data: Dict[str, Any]) -> bool:
    """Save medications to YAML and regenerate markdown.

    Uses atomic_write_yaml (tmpfile + os.replace): a crash mid-write must not
    corrupt medications.yaml — get_medications() would then return {} and all
    med nudges would silently die.
    """
    try:
        # Snapshot the pre-write state for the audit trail BEFORE atomic_write
        # (the cache still holds the old value here).
        before = get_medications()
        # Atomic write (#33 crash-safety) + refresh the parse cache (perf).
        atomic_write_yaml(MEDICATIONS_YAML, data)
        _store_yaml_cache(MEDICATIONS_YAML, data)
        # Derived RAG shadow doc is OFF by default now (see _rag_docs_enabled).
        if _rag_docs_enabled():
            _generate_medications_md(data)
        # Unify meds writes with the settings-page audit trail they used to
        # bypass (config_writer docstring). Never let audit failure sink a save.
        try:
            log_config_change("medications", before, data)
        except Exception as audit_err:  # noqa: BLE001
            logger.warning(f"[DATA] meds audit log failed: {audit_err}")
        return True
    except Exception as e:
        logger.error(f"Error saving medications: {e}")
        return False


_CANONICAL_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def normalize_days(days: Any = None, skip_weekends: Any = None) -> list | None:
    """Collapse the model-facing `days` / `skip_weekends` inputs to a canonical
    weekday list, or None when neither was supplied (= "don't touch / every day").

    - Explicit `days` wins over `skip_weekends` if both are given.
    - Entries are normalized (lowercase, first 3 chars); unknown tokens dropped.
    - Output preserves canonical Mon→Sun order and de-dups.
    - `skip_weekends` truthy → [mon,tue,wed,thu,fri].
    """
    if days:
        seen = {str(d).strip().lower()[:3] for d in days if str(d).strip()}
        canon = [d for d in _CANONICAL_DAYS if d in seen]
        return canon or None
    if skip_weekends:
        return ["mon", "tue", "wed", "thu", "fri"]
    return None


def add_medication(
    name: str,
    dose: str = "",
    schedule: str = "morning",
    purpose: str = "",
    notes: str = "",
    days: list = None,
) -> str:
    """Add a medication to the specified schedule."""
    data = get_medications()

    new_med = {
        "name": name,
        "dose": dose,
        "purpose": purpose,
        "notes": notes,
    }
    # Only carry `days` when set — absence means "every day" (backward compatible).
    if days:
        new_med["days"] = days

    # Determine where to add based on schedule
    if schedule in ["morning", "evening"]:
        if "daily" not in data:
            data["daily"] = {"morning": [], "evening": []}
        if schedule not in data["daily"]:
            data["daily"][schedule] = []
        data["daily"][schedule].append(new_med)
    elif schedule == "weekly":
        if "weekly" not in data:
            data["weekly"] = []
        new_med["when"] = notes  # For weekly, notes often contains timing
        data["weekly"].append(new_med)
    elif schedule == "as_needed":
        if "as_needed" not in data:
            data["as_needed"] = []
        new_med["when"] = notes
        data["as_needed"].append(new_med)
    else:
        return f"Unknown schedule: {schedule}. Use morning, evening, weekly, or as_needed."

    if save_medications(data):
        return f"Added {name} ({dose}) to {schedule} medications."
    return f"Failed to save medication {name}."


def remove_medication(name: str) -> str:
    """Remove a medication by name (searches all schedules)."""
    data = get_medications()
    found = False

    # Search daily medications
    for schedule in ["morning", "evening"]:
        if "daily" in data and schedule in data["daily"]:
            original_len = len(data["daily"][schedule])
            data["daily"][schedule] = [m for m in data["daily"][schedule] if m.get("name", "").lower() != name.lower()]
            if len(data["daily"][schedule]) < original_len:
                found = True

    # Search weekly
    if "weekly" in data:
        original_len = len(data["weekly"])
        data["weekly"] = [m for m in data["weekly"] if m.get("name", "").lower() != name.lower()]
        if len(data["weekly"]) < original_len:
            found = True

    # Search as_needed
    if "as_needed" in data:
        original_len = len(data["as_needed"])
        data["as_needed"] = [m for m in data["as_needed"] if m.get("name", "").lower() != name.lower()]
        if len(data["as_needed"]) < original_len:
            found = True

    if found:
        if save_medications(data):
            return f"Removed {name} from medications."
        return f"Found {name} but failed to save changes."
    return f"Medication '{name}' not found."


_SCHEDULE_BUCKETS = ("morning", "evening", "weekly", "as_needed")


def _find_medication(data: Dict[str, Any], name: str):
    """Locate a med by name across every schedule bucket.

    Returns (containing_list, index, schedule_str) or None. `schedule_str` is
    the caller-facing bucket name ("morning"/"evening"/"weekly"/"as_needed"),
    so update_medication can tell whether a requested schedule is a real move.
    """
    name_l = name.lower()
    daily = data.get("daily") or {}
    for sched in ("morning", "evening"):
        lst = daily.get(sched) or []
        for i, med in enumerate(lst):
            if isinstance(med, dict) and med.get("name", "").lower() == name_l:
                return lst, i, sched
    for bucket in ("weekly", "as_needed"):
        lst = data.get(bucket) or []
        for i, med in enumerate(lst):
            if isinstance(med, dict) and med.get("name", "").lower() == name_l:
                return lst, i, bucket
    return None


def _append_to_schedule(data: Dict[str, Any], med: Dict[str, Any], schedule: str) -> None:
    """Append an existing med dict into the target schedule bucket."""
    if schedule in ("morning", "evening"):
        daily = data.setdefault("daily", {"morning": [], "evening": []})
        daily.setdefault(schedule, []).append(med)
    else:  # weekly | as_needed (validated by caller)
        data.setdefault(schedule, []).append(med)


def update_medication(
    name: str,
    dose: str = None,
    purpose: str = None,
    notes: str = None,
    schedule: str = None,
    days: list = None,
) -> str:
    """Update an existing medication's properties.

    Honesty contract (was the source of a silent false-success): a found med
    that ends up with NO changed field returns an explicit "nothing to update"
    and does NOT write — the previous code wrote an unchanged dict, returned
    True, and reported "Updated X: ." which the model relayed as done.

    `schedule` now actually relocates the med between buckets (morning/evening/
    weekly/as_needed); it used to be an accepted-but-ignored dead parameter.
    """
    if schedule is not None and schedule not in _SCHEDULE_BUCKETS:
        return f"Unknown schedule: {schedule}. Use morning, evening, weekly, or as_needed."

    data = get_medications()
    located = _find_medication(data, name)
    if located is None:
        return f"Medication '{name}' not found."

    lst, idx, current_schedule = located
    med = lst[idx]
    updated_fields = []

    if dose is not None:
        med["dose"] = dose
        updated_fields.append(f"dose={dose}")
    if purpose is not None:
        med["purpose"] = purpose
        updated_fields.append(f"purpose={purpose}")
    if notes is not None:
        med["notes"] = notes
        updated_fields.append(f"notes={notes}")
    if days is not None:
        med["days"] = days
        updated_fields.append(f"days={','.join(days)}")

    # Relocate only when the requested schedule differs from the current one.
    if schedule is not None and schedule != current_schedule:
        lst.pop(idx)
        _append_to_schedule(data, med, schedule)
        updated_fields.append(f"schedule={schedule}")

    # Found but nothing actually changed: report honestly and skip the write
    # (no pointless save + audit entry). NEVER return "Updated X: .".
    if not updated_fields:
        return f"No changes to {name} — nothing to update."

    if save_medications(data):
        return f"Updated {name}: {', '.join(updated_fields)}."
    return f"Found {name} but failed to save changes."


def _generate_medications_md(data: Dict[str, Any]) -> None:
    """Regenerate medications.md from YAML data."""
    from orchestrator.user_profile import get_profile

    lines = [f"# {get_profile().user_name}'s Medications", ""]

    # Daily medications
    lines.append("## Daily Medications")
    lines.append("")

    # Morning
    lines.append("### Morning (with breakfast)")
    lines.append("| Medication | Dose | Purpose | Notes | Days |")
    lines.append("|------------|------|---------|-------|------|")
    for med in data.get("daily", {}).get("morning", []):
        lines.append(
            f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('purpose', '')} | "
            f"{med.get('notes', '')} | {_fmt_days(med.get('days')) or 'every day'} |"
        )
    lines.append("")

    # Evening
    lines.append("### Evening (before bed)")
    lines.append("| Medication | Dose | Purpose | Notes | Days |")
    lines.append("|------------|------|---------|-------|------|")
    for med in data.get("daily", {}).get("evening", []):
        lines.append(
            f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('purpose', '')} | "
            f"{med.get('notes', '')} | {_fmt_days(med.get('days')) or 'every day'} |"
        )
    lines.append("")

    # Weekly
    lines.append("## Weekly Medications")
    lines.append("| Medication | Dose | When to Take | Notes |")
    lines.append("|------------|------|--------------|-------|")
    for med in data.get("weekly", []):
        lines.append(
            f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('when', '')} | {med.get('notes', '')} |"
        )
    lines.append("")

    # As-needed
    lines.append("## As-needed Medications")
    lines.append("| Medication | Dose | When to Take | Max per Day |")
    lines.append("|------------|------|--------------|-------------|")
    for med in data.get("as_needed", []):
        lines.append(
            f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('when', '')} | {med.get('max_per_day', '')} |"
        )
    lines.append("")

    # Pharmacy & Refills
    lines.append("## Pharmacy & Refills")
    for pharmacy in data.get("pharmacy", []):
        lines.append(f"- **Pharmacy:** {pharmacy.get('name', '')}")
        lines.append(f"- **Refill day:** {pharmacy.get('refill_day', '')}")
        lines.append(f"- **Doctor:** {pharmacy.get('doctor', '')}")
        meds = ", ".join(pharmacy.get("meds", []))
        lines.append(f"- **Meds:** {meds}")
        lines.append("")

    # Interactions
    lines.append("## Important Interactions")
    for interaction in data.get("interactions", []):
        lines.append(f"- {interaction}")
    lines.append("")

    # Reminders
    lines.append("## Reminders")
    reminders = data.get("reminders", {})
    if reminders.get("morning"):
        lines.append(f"- Morning meds: {reminders['morning']}")
    if reminders.get("evening"):
        lines.append(f"- Evening meds: {reminders['evening']}")
    if reminders.get("refill"):
        lines.append(f"- Refill reminder: {reminders['refill']}")
    lines.append("")

    try:
        with open(MEDICATIONS_MD, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"Regenerated {MEDICATIONS_MD}")
    except Exception as e:
        logger.error(f"Error writing medications markdown: {e}")


# =============================================================================
# PROJECTS
# =============================================================================


def get_projects() -> Dict[str, Any]:
    """Load projects from YAML."""
    try:
        return _load_yaml_cached(PROJECTS_YAML)
    except FileNotFoundError:
        logger.warning(f"Projects file not found: {PROJECTS_YAML}")
        return {"active": [], "on_hold": [], "someday_maybe": [], "completed": [], "parking_lot": []}
    except Exception as e:
        logger.error(f"Error loading projects: {e}")
        return {}


def save_projects(data: Dict[str, Any]) -> bool:
    """Save projects to YAML and regenerate markdown.

    Atomic for the same reason as save_medications: a torn write would make
    get_projects() return {} on the next load.
    """
    try:
        before = get_projects()
        # Atomic write (#33 crash-safety) + refresh the parse cache (perf).
        atomic_write_yaml(PROJECTS_YAML, data)
        _store_yaml_cache(PROJECTS_YAML, data)
        if _rag_docs_enabled():
            _generate_projects_md(data)
        try:
            log_config_change("projects", before, data)
        except Exception as audit_err:  # noqa: BLE001
            logger.warning(f"[DATA] projects audit log failed: {audit_err}")
        return True
    except Exception as e:
        logger.error(f"Error saving projects: {e}")
        return False


def add_project(name: str, goal: str = "", priority: str = "medium", category: str = "active") -> str:
    """Add a new project."""
    data = get_projects()

    if category == "active":
        if "active" not in data:
            data["active"] = []
        new_project = {
            "name": name,
            "status": "not_started",
            "priority": priority,
            "goal": goal,
            "completed": [],
            "next_steps": [],
            "blockers": [],
        }
        data["active"].append(new_project)
    elif category == "someday_maybe":
        if "someday_maybe" not in data:
            data["someday_maybe"] = []
        data["someday_maybe"].append(name)
    elif category == "parking_lot":
        if "parking_lot" not in data:
            data["parking_lot"] = []
        data["parking_lot"].append(name)
    else:
        return f"Unknown category: {category}. Use active, someday_maybe, or parking_lot."

    if save_projects(data):
        return f"Added project '{name}' to {category}."
    return f"Failed to save project '{name}'."


def remove_project(name: str) -> str:
    """Remove a project entirely from any category (active, on_hold, someday_maybe, parking_lot)."""
    data = get_projects()
    found = False

    for category in ["active", "on_hold", "someday_maybe", "parking_lot", "completed"]:
        items = data.get(category, [])
        original_len = len(items)
        data[category] = [
            p
            for p in items
            if not (
                (isinstance(p, dict) and p.get("name", "").lower() == name.lower())
                or (isinstance(p, str) and p.lower() == name.lower())
            )
        ]
        if len(data[category]) < original_len:
            found = True

    if found:
        if save_projects(data):
            return f"Removed project '{name}'."
        return f"Found '{name}' but failed to save changes."
    return f"Project '{name}' not found."


def update_project_status(name: str, status: str) -> str:
    """Update a project's status (not_started, in_progress, blocked, done)."""
    data = get_projects()

    valid_statuses = ["not_started", "in_progress", "blocked", "done"]
    if status not in valid_statuses:
        return f"Invalid status: {status}. Use: {', '.join(valid_statuses)}"

    # If marking as done, move to completed
    if status == "done":
        for i, project in enumerate(data.get("active", [])):
            if project.get("name", "").lower() == name.lower():
                completed_project = {
                    "name": project["name"],
                    "completed_date": datetime.now().strftime("%B %Y"),
                }
                if "completed" not in data:
                    data["completed"] = []
                data["completed"].insert(0, completed_project)
                data["active"].pop(i)
                if save_projects(data):
                    return f"Marked '{name}' as complete and moved to completed list."
                return "Failed to save project status change."
        return f"Project '{name}' not found in active projects."

    # Otherwise update status in active
    for project in data.get("active", []):
        if project.get("name", "").lower() == name.lower():
            project["status"] = status
            if save_projects(data):
                return f"Updated '{name}' status to {status}."
            return "Failed to save project status change."

    return f"Project '{name}' not found in active projects."


def add_project_step(project_name: str, step: str, completed: bool = False) -> str:
    """Add a step to a project's next_steps or completed list."""
    data = get_projects()

    for project in data.get("active", []):
        if project.get("name", "").lower() == project_name.lower():
            if completed:
                if "completed" not in project:
                    project["completed"] = []
                project["completed"].append(step)
                action = "completed items"
            else:
                if "next_steps" not in project:
                    project["next_steps"] = []
                project["next_steps"].append(step)
                action = "next steps"

            if save_projects(data):
                return f"Added '{step}' to {project['name']}'s {action}."
            return "Failed to save step."

    return f"Project '{project_name}' not found in active projects."


def complete_step(project_name: str, step: str) -> str:
    """Move a step from next_steps to completed."""
    data = get_projects()

    for project in data.get("active", []):
        if project.get("name", "").lower() == project_name.lower():
            # Find the step in next_steps (fuzzy match)
            next_steps = project.get("next_steps", [])
            found_idx = None
            found_step = None

            for i, s in enumerate(next_steps):
                if step.lower() in s.lower() or s.lower() in step.lower():
                    found_idx = i
                    found_step = s
                    break

            if found_idx is not None:
                # Move from next_steps to completed
                project["next_steps"].pop(found_idx)
                if "completed" not in project:
                    project["completed"] = []
                project["completed"].append(found_step)

                if save_projects(data):
                    return f"Marked '{found_step}' as complete on {project['name']}."
                return "Failed to save step completion."

            return f"Step '{step}' not found in {project['name']}'s next steps."

    return f"Project '{project_name}' not found in active projects."


def _generate_projects_md(data: Dict[str, Any]) -> None:
    """Regenerate current.md from YAML data."""
    lines = ["# Current Projects", ""]

    # Active projects
    lines.append("## Active (Working on Now)")
    lines.append("")

    for project in data.get("active", []):
        lines.append(f"### {project.get('name', 'Unnamed Project')}")
        lines.append(f"**Status:** {project.get('status', 'Unknown').replace('_', ' ').title()}")
        lines.append(f"**Priority:** {project.get('priority', 'Medium').title()}")
        lines.append(f"**Goal:** {project.get('goal', '')}")
        lines.append("")

        # Completed items
        completed = project.get("completed", [])
        if completed:
            lines.append("**Completed:**")
            for item in completed:
                lines.append(f"- [x] {item}")
            lines.append("")

        # Next steps
        next_steps = project.get("next_steps", [])
        if next_steps:
            lines.append("**Next steps:**")
            for item in next_steps:
                lines.append(f"- [ ] {item}")
            lines.append("")

        # Blockers
        blockers = project.get("blockers", [])
        if blockers:
            lines.append(f"**Blockers:** {', '.join(blockers)}")
        else:
            lines.append("**Blockers:** None currently")
        lines.append("")
        lines.append("---")
        lines.append("")

    # On hold
    lines.append("## On Hold (Will Return To)")
    lines.append("")
    for project in data.get("on_hold", []):
        if isinstance(project, dict):
            lines.append(f"### {project.get('name', '')}")
            lines.append(f"**Why on hold:** {project.get('why_on_hold', '')}")
            lines.append(f"**Resume when:** {project.get('resume_when', '')}")
            lines.append("")
            lines.append("---")
            lines.append("")
        else:
            lines.append(f"- {project}")
    lines.append("")

    # Someday/Maybe
    lines.append("## Someday / Maybe")
    for item in data.get("someday_maybe", []):
        if isinstance(item, str):
            lines.append(f"- {item}")
        elif isinstance(item, dict):
            lines.append(f"- {item.get('name', item)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Recently Completed
    lines.append("## Recently Completed")
    for project in data.get("completed", []):
        if isinstance(project, dict):
            lines.append(f"- {project.get('name', '')} - Completed {project.get('completed_date', '')}")
        else:
            lines.append(f"- {project}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Parking lot
    lines.append("## Project Parking Lot")
    lines.append("*Quick capture for ideas that pop up - review weekly*")
    lines.append("")
    for item in data.get("parking_lot", []):
        if isinstance(item, str):
            lines.append(f"- {item}")
        elif isinstance(item, dict):
            lines.append(f"- {item.get('name', item)}")
    if not data.get("parking_lot"):
        lines.append("- ")
    lines.append("")

    try:
        with open(PROJECTS_MD, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"Regenerated {PROJECTS_MD}")
    except Exception as e:
        logger.error(f"Error writing projects markdown: {e}")


# =============================================================================
# UNIFIED TOOL HANDLER
# =============================================================================


# =============================================================================
# READ-ONLY ACCESS (single source of truth for the model + the prompt)
# =============================================================================
# The model has no way to READ these YAMLs except through here — update_data is
# write-only. Before this, "what are my meds?" fell through to search_memory
# (the RAG palace), which lags the YAML and can be poisoned by auto_learn. Both
# the `get_data` tool AND the system-prompt inject render from the SAME helpers
# below, so a tool answer and the injected block can never disagree.


_DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def _fmt_days(days: Any) -> str:
    """Compact human label for a med's `days` list: 'Mon–Fri', 'weekends', else
    a slash list like 'Mon/Wed/Fri'. '' when absent/empty/malformed so callers
    can `if hint:`. Mirrors the frontend badge so text and page never disagree."""
    if not days or not isinstance(days, (list, tuple)):
        return ""
    seen = {str(d).strip().lower()[:3] for d in days if str(d).strip()}
    canon = [d for d in _CANONICAL_DAYS if d in seen]
    if not canon:
        return ""
    if canon == ["mon", "tue", "wed", "thu", "fri"]:
        return "Mon–Fri"
    if canon == ["sat", "sun"]:
        return "weekends"
    return "/".join(_DAY_LABELS[d] for d in canon)


def _fmt_med(med: Dict[str, Any]) -> str:
    """`Name Dose` (dose omitted when blank), plus a `(Mon–Fri)` schedule hint
    when the med carries a `days` restriction — so the authoritative text the
    model sees (get_data + prompt inject) reflects weekends-off, not plain daily."""
    name = str(med.get("name", "")).strip()
    dose = str(med.get("dose", "")).strip()
    base = f"{name} {dose}".strip()
    hint = _fmt_days(med.get("days"))
    return f"{base} ({hint})" if hint else base


def render_medications_compact(data: Dict[str, Any] | None = None) -> str:
    """Terse names+doses+schedule rendering of the meds YAML (no notes/purpose).

    This is the authoritative text the model sees. Kept small on purpose — it
    rides in every prompt (1b) and is returned by get_data (1a).
    """
    data = get_medications() if data is None else data
    daily = data.get("daily", {}) or {}

    def _dicts(items) -> list:
        # Guard non-dict entries (the YAML permits bare strings in some lists);
        # a stray string must not raise and blank the meds block.
        return [m for m in (items or []) if isinstance(m, dict) and m.get("name")]

    def _line(meds: list) -> str:
        parts = [_fmt_med(m) for m in meds]
        return "; ".join(parts) if parts else "(none)"

    def _with_when(m: Dict[str, Any]) -> str:
        when = str(m.get("when", "")).strip()
        return f"{_fmt_med(m)} ({when})" if when else _fmt_med(m)

    lines = [
        f"Morning: {_line(_dicts(daily.get('morning')))}",
        f"Evening: {_line(_dicts(daily.get('evening')))}",
    ]
    weekly = _dicts(data.get("weekly"))
    if weekly:
        lines.append("Weekly: " + "; ".join(_with_when(m) for m in weekly))
    as_needed = _dicts(data.get("as_needed"))
    if as_needed:
        lines.append("As-needed: " + "; ".join(m.get("name", "") for m in as_needed))
    return "\n".join(lines)


_PRIORITY_ORDER = {"high": 0, "medium": 1, "normal": 1, "low": 2}


def render_projects_compact(data: Dict[str, Any] | None = None, top_n: int = 5) -> str:
    """Top-N active project names, highest priority first."""
    data = get_projects() if data is None else data
    # Guard non-dict entries — the schema allows bare strings under some project
    # buckets, and remove_project treats a string under `active` as reachable.
    active = [p for p in (data.get("active") or []) if isinstance(p, dict) and p.get("name")]
    ordered = sorted(active, key=lambda p: _PRIORITY_ORDER.get(str(p.get("priority", "medium")).lower(), 1))
    names = [str(p.get("name", "")).strip() for p in ordered[:top_n]]
    return "; ".join(names) if names else "(none)"


def render_profile_compact() -> str:
    """Minimal identity line (name + timezone)."""
    from orchestrator.user_profile import get_profile

    p = get_profile()
    return f"Name: {p.user_name}; timezone: {p.timezone}"


def get_structured_facts_block() -> str:
    """Compact block injected into every system prompt (1b). Robust: never
    raises. Meds and projects render under SEPARATE guards so a bad projects
    entry can't blank the medications block (the safety-critical half)."""
    parts = []
    try:
        parts.append(
            "MEDICATIONS (authoritative — from medications.yaml, NOT memory):\n" + render_medications_compact()
        )
    except Exception as e:  # noqa: BLE001 — meds must ride the prompt even if projects fail
        logger.warning(f"[DATA] meds block render failed: {e}")
    try:
        parts.append(f"ACTIVE PROJECTS (top 3): {render_projects_compact(top_n=3)}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[DATA] projects block render failed: {e}")
    return "\n".join(parts)


def handle_get_data(kind: str) -> str:
    """Read handler for the `get_data` tool (non-terminal). Returns authoritative
    YAML-derived text so the model answers from the source, not RAG."""
    # str() guard: the schema constrains kind to an enum but isn't runtime-
    # enforced, so a non-string kind from the model must not AttributeError.
    k = str(kind or "").lower().strip()
    if k in ("medications", "medication", "meds"):
        return "MEDICATIONS (from medications.yaml — source of truth):\n" + render_medications_compact()
    if k in ("projects", "project"):
        return "ACTIVE PROJECTS (from projects.yaml):\n" + render_projects_compact()
    if k in ("profile", "identity"):
        return render_profile_compact()
    return f"Unknown kind: {kind!r}. Valid kinds: medications, projects, profile."


def handle_update_data(action: str, name: str, **kwargs) -> str:
    """
    Unified handler for the update_data tool.
    Routes to the appropriate function based on action.
    """
    # Normalize the two model-facing weekday inputs down to a single canonical
    # `days` list once, so add/update never see skip_weekends or raw junk.
    days = normalize_days(kwargs.get("days"), kwargs.get("skip_weekends"))

    action_handlers = {
        "add_medication": lambda: add_medication(
            name=name,
            dose=kwargs.get("dose", ""),
            schedule=kwargs.get("schedule", "morning"),
            purpose=kwargs.get("purpose", ""),
            notes=kwargs.get("notes", ""),
            days=days,
        ),
        "remove_medication": lambda: remove_medication(name),
        "update_medication": lambda: update_medication(
            name=name,
            dose=kwargs.get("dose"),
            purpose=kwargs.get("purpose"),
            notes=kwargs.get("notes"),
            schedule=kwargs.get("schedule"),
            days=days,
        ),
        "remove_project": lambda: remove_project(name),
        "add_project": lambda: add_project(
            name=name,
            goal=kwargs.get("goal", ""),
            priority=kwargs.get("priority", "medium"),
            category=kwargs.get("category", "active"),
        ),
        "update_project_status": lambda: update_project_status(
            name=name,
            status=kwargs.get("status", "in_progress"),
        ),
        "add_project_step": lambda: add_project_step(
            project_name=name,
            step=kwargs.get("step", ""),
            completed=kwargs.get("completed", False),
        ),
        "complete_step": lambda: complete_step(
            project_name=name,
            step=kwargs.get("step", ""),
        ),
    }

    if action not in action_handlers:
        valid_actions = ", ".join(action_handlers.keys())
        return f"Unknown action: {action}. Valid actions: {valid_actions}"

    try:
        return action_handlers[action]()
    except Exception as e:
        logger.error(f"Error in handle_update_data({action}): {e}")
        return f"Error executing {action}: {str(e)}"
