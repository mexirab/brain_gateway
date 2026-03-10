"""
Data Manager for Brain Gateway
Handles structured YAML data (medications, projects) with auto-markdown generation.
"""

import os
import yaml
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Paths - configurable via environment
RAG_BASE = os.environ.get("RAG_BASE", "/home/labadmin/rag/nadim_rag")
MEDICATIONS_YAML = os.path.join(RAG_BASE, "10_profile", "medications.yaml")
MEDICATIONS_MD = os.path.join(RAG_BASE, "10_profile", "medications.md")
PROJECTS_YAML = os.path.join(RAG_BASE, "30_projects", "projects.yaml")
PROJECTS_MD = os.path.join(RAG_BASE, "30_projects", "current.md")


# =============================================================================
# MEDICATIONS
# =============================================================================

def get_medications() -> Dict[str, Any]:
    """Load medications from YAML."""
    try:
        with open(MEDICATIONS_YAML, 'r') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"Medications file not found: {MEDICATIONS_YAML}")
        return {"daily": {"morning": [], "evening": []}, "weekly": [], "as_needed": []}
    except Exception as e:
        logger.error(f"Error loading medications: {e}")
        return {}


def save_medications(data: Dict[str, Any]) -> bool:
    """Save medications to YAML and regenerate markdown."""
    try:
        with open(MEDICATIONS_YAML, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _generate_medications_md(data)
        return True
    except Exception as e:
        logger.error(f"Error saving medications: {e}")
        return False


def add_medication(name: str, dose: str = "", schedule: str = "morning",
                   purpose: str = "", notes: str = "") -> str:
    """Add a medication to the specified schedule."""
    data = get_medications()

    new_med = {
        "name": name,
        "dose": dose,
        "purpose": purpose,
        "notes": notes,
    }

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
            data["daily"][schedule] = [
                m for m in data["daily"][schedule]
                if m.get("name", "").lower() != name.lower()
            ]
            if len(data["daily"][schedule]) < original_len:
                found = True

    # Search weekly
    if "weekly" in data:
        original_len = len(data["weekly"])
        data["weekly"] = [
            m for m in data["weekly"]
            if m.get("name", "").lower() != name.lower()
        ]
        if len(data["weekly"]) < original_len:
            found = True

    # Search as_needed
    if "as_needed" in data:
        original_len = len(data["as_needed"])
        data["as_needed"] = [
            m for m in data["as_needed"]
            if m.get("name", "").lower() != name.lower()
        ]
        if len(data["as_needed"]) < original_len:
            found = True

    if found:
        if save_medications(data):
            return f"Removed {name} from medications."
        return f"Found {name} but failed to save changes."
    return f"Medication '{name}' not found."


def update_medication(name: str, dose: str = None, purpose: str = None,
                      notes: str = None, schedule: str = None) -> str:
    """Update an existing medication's properties."""
    data = get_medications()
    found = False
    updated_fields = []

    def update_med(med_list):
        nonlocal found, updated_fields
        for med in med_list:
            if med.get("name", "").lower() == name.lower():
                found = True
                if dose is not None:
                    med["dose"] = dose
                    updated_fields.append(f"dose={dose}")
                if purpose is not None:
                    med["purpose"] = purpose
                    updated_fields.append(f"purpose={purpose}")
                if notes is not None:
                    med["notes"] = notes
                    updated_fields.append(f"notes={notes}")
                return True
        return False

    # Search all schedules
    if "daily" in data:
        for sched in ["morning", "evening"]:
            if sched in data["daily"]:
                update_med(data["daily"][sched])

    if "weekly" in data:
        update_med(data["weekly"])

    if "as_needed" in data:
        update_med(data["as_needed"])

    if found:
        if save_medications(data):
            return f"Updated {name}: {', '.join(updated_fields)}."
        return f"Found {name} but failed to save changes."
    return f"Medication '{name}' not found."


def _generate_medications_md(data: Dict[str, Any]) -> None:
    """Regenerate medications.md from YAML data."""
    lines = ["# Nadim's Medications", ""]

    # Daily medications
    lines.append("## Daily Medications")
    lines.append("")

    # Morning
    lines.append("### Morning (with breakfast)")
    lines.append("| Medication | Dose | Purpose | Notes |")
    lines.append("|------------|------|---------|-------|")
    for med in data.get("daily", {}).get("morning", []):
        lines.append(f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('purpose', '')} | {med.get('notes', '')} |")
    lines.append("")

    # Evening
    lines.append("### Evening (before bed)")
    lines.append("| Medication | Dose | Purpose | Notes |")
    lines.append("|------------|------|---------|-------|")
    for med in data.get("daily", {}).get("evening", []):
        lines.append(f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('purpose', '')} | {med.get('notes', '')} |")
    lines.append("")

    # Weekly
    lines.append("## Weekly Medications")
    lines.append("| Medication | Dose | When to Take | Notes |")
    lines.append("|------------|------|--------------|-------|")
    for med in data.get("weekly", []):
        lines.append(f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('when', '')} | {med.get('notes', '')} |")
    lines.append("")

    # As-needed
    lines.append("## As-needed Medications")
    lines.append("| Medication | Dose | When to Take | Max per Day |")
    lines.append("|------------|------|--------------|-------------|")
    for med in data.get("as_needed", []):
        lines.append(f"| {med.get('name', '')} | {med.get('dose', '')} | {med.get('when', '')} | {med.get('max_per_day', '')} |")
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
        with open(MEDICATIONS_MD, 'w') as f:
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
        with open(PROJECTS_YAML, 'r') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"Projects file not found: {PROJECTS_YAML}")
        return {"active": [], "on_hold": [], "someday_maybe": [], "completed": [], "parking_lot": []}
    except Exception as e:
        logger.error(f"Error loading projects: {e}")
        return {}


def save_projects(data: Dict[str, Any]) -> bool:
    """Save projects to YAML and regenerate markdown."""
    try:
        with open(PROJECTS_YAML, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        _generate_projects_md(data)
        return True
    except Exception as e:
        logger.error(f"Error saving projects: {e}")
        return False


def add_project(name: str, goal: str = "", priority: str = "medium",
                category: str = "active") -> str:
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
            p for p in items
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
                return f"Failed to save project status change."
        return f"Project '{name}' not found in active projects."

    # Otherwise update status in active
    for project in data.get("active", []):
        if project.get("name", "").lower() == name.lower():
            project["status"] = status
            if save_projects(data):
                return f"Updated '{name}' status to {status}."
            return f"Failed to save project status change."

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
            return f"Failed to save step."

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
                return f"Failed to save step completion."

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
        with open(PROJECTS_MD, 'w') as f:
            f.write("\n".join(lines))
        logger.info(f"Regenerated {PROJECTS_MD}")
    except Exception as e:
        logger.error(f"Error writing projects markdown: {e}")


# =============================================================================
# UNIFIED TOOL HANDLER
# =============================================================================

def handle_update_data(action: str, name: str, **kwargs) -> str:
    """
    Unified handler for the update_data tool.
    Routes to the appropriate function based on action.
    """
    action_handlers = {
        "add_medication": lambda: add_medication(
            name=name,
            dose=kwargs.get("dose", ""),
            schedule=kwargs.get("schedule", "morning"),
            purpose=kwargs.get("purpose", ""),
            notes=kwargs.get("notes", ""),
        ),
        "remove_medication": lambda: remove_medication(name),
        "update_medication": lambda: update_medication(
            name=name,
            dose=kwargs.get("dose"),
            purpose=kwargs.get("purpose"),
            notes=kwargs.get("notes"),
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
