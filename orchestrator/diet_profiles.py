"""
Diet profiles + meal-prep preferences (F-015 phase 2).

Built-in profiles are rule lists handed to the weekly planner prompt
(meal_planner). They are eating *preferences*, not medical prescriptions —
the UI says so. The user's choice and schedule live in a runtime YAML at
DIET_PROFILE_PATH, written atomically and audited like the other /settings
panels (config_writer).
"""

from __future__ import annotations

import copy
import logging
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional

import yaml

from orchestrator import shared
from orchestrator.config_writer import atomic_write_yaml, log_config_change

logger = logging.getLogger(__name__)

PROFILES: Dict[str, Dict[str, Any]] = {
    "mediterranean": {
        "label": "Mediterranean",
        "description": "Vegetables, legumes, whole grains, fish and olive oil; little red meat.",
        "rules": [
            "Vegetables, legumes and whole grains every day",
            "Fish or seafood at least twice a week",
            "Olive oil as the main cooking fat; nuts and seeds welcome",
            "Limit red and processed meat, butter, cream and refined sugar",
            "Prefer high-fibre, low saturated-fat choices",
        ],
    },
    "dash": {
        "label": "DASH",
        "description": "Heart-healthy: lots of produce and low-fat dairy, low sodium.",
        "rules": [
            "Plenty of vegetables, fruit and whole grains",
            "Low-fat dairy, poultry, fish, beans and nuts for protein",
            "Keep sodium low: no heavily salted, cured or processed foods; season with herbs, citrus and spices",
            "Limit red meat, sweets and sugary drinks",
        ],
    },
    "tlc": {
        "label": "TLC (cholesterol-lowering)",
        "description": "Very low saturated fat, high soluble fibre (oats, beans, barley).",
        "rules": [
            "Keep saturated fat very low: lean proteins, no butter, cream, fatty or processed meat",
            "Include soluble fibre daily: oats, barley, beans, lentils, apples, citrus",
            "Fish twice a week; skinless poultry and legumes otherwise",
            "Use olive or canola oil, in moderate amounts",
        ],
    },
    "pescatarian": {
        "label": "Pescatarian",
        "description": "Vegetarian plus fish and seafood.",
        "rules": [
            "No meat or poultry; fish, seafood, eggs and dairy are fine",
            "Legumes and whole grains most days",
        ],
    },
    "vegetarian": {
        "label": "Vegetarian",
        "description": "No meat or fish; eggs and dairy are fine.",
        "rules": [
            "No meat, poultry, fish or seafood (or stocks made from them)",
            "Get protein from legumes, tofu, eggs, dairy, nuts",
        ],
    },
    "vegan": {
        "label": "Vegan",
        "description": "Fully plant-based.",
        "rules": [
            "No animal products at all: no meat, fish, eggs, dairy or honey",
            "Get protein from legumes, tofu, tempeh, seitan, nuts and seeds",
        ],
    },
    "low_carb": {
        "label": "Lower-carb",
        "description": "Fewer starches and sugars; protein and vegetables first.",
        "rules": [
            "Keep starches small: little bread, pasta, rice or potatoes",
            "Build meals around protein and non-starchy vegetables",
            "No sugary sauces or desserts",
        ],
    },
    "balanced": {
        "label": "No particular diet",
        "description": "Just balanced, varied home cooking.",
        "rules": ["Balanced, varied home cooking with a vegetable at most meals"],
    },
}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULTS: Dict[str, Any] = {
    "profile": "mediterranean",
    "servings": 1,
    "batch_day": "sun",
    "batch_lunch_days": 4,  # lunches covered by the batch prep (0 = no batch prep)
    "fresh_dinners_per_week": 4,
    "max_dinner_minutes": 30,
    "avoid": [],
    "extra_rules": [],
    "reminders": {"prep": True, "dinners": True, "prep_time": "15:00", "dinner_time": "17:30"},
    # Weekly "photograph the fridge" nudge (F-015 phase 4) — fixes drift.
    "fridge_check": {"enabled": True, "day": "sat", "time": "18:00"},
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_AVOID = 20
MAX_EXTRA_RULES = 10


def _path() -> str:
    return getattr(shared, "DIET_PROFILE_PATH", "/app/data/diet_profile.yaml")


def _clean_text(raw: Any, limit: int) -> str:
    """Free-text preference → safe one-line string (it lands in an LLM prompt)."""
    text = "".join(ch for ch in str(raw or "") if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[<>{}\[\]`|\\]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``data`` over the defaults; raise ValueError on bad values."""
    if not isinstance(data, dict):
        raise ValueError("diet settings must be an object")
    out = copy.deepcopy(DEFAULTS)
    if "profile" in data:
        if not isinstance(data["profile"], str) or data["profile"] not in PROFILES:
            raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
        out["profile"] = data["profile"]
    for key, lo, hi in (
        ("servings", 1, 8),
        ("batch_lunch_days", 0, 6),
        ("fresh_dinners_per_week", 0, 7),
        ("max_dinner_minutes", 10, 180),
    ):
        if key in data:
            v = data[key]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{key} must be a whole number {lo}-{hi}")
            out[key] = v
    if "batch_day" in data:
        if not isinstance(data["batch_day"], str) or data["batch_day"] not in WEEKDAYS:
            raise ValueError("batch_day must be mon..sun")
        out["batch_day"] = data["batch_day"]
    for key, cap, limit in (("avoid", MAX_AVOID, 40), ("extra_rules", MAX_EXTRA_RULES, 200)):
        if key in data:
            if not isinstance(data[key], list):
                raise ValueError(f"{key} must be a list")
            vals = [_clean_text(x, limit) for x in data[key]]
            vals = [v for v in vals if v]
            if len(vals) > cap:
                raise ValueError(f"{key}: at most {cap} entries")
            out[key] = vals
    if "reminders" in data:
        r = data["reminders"]
        if not isinstance(r, dict):
            raise ValueError("reminders must be an object")
        for flag in ("prep", "dinners"):
            if flag in r:
                if not isinstance(r[flag], bool):
                    raise ValueError(f"reminders.{flag} must be true/false")
                out["reminders"][flag] = r[flag]
        for t in ("prep_time", "dinner_time"):
            if t in r:
                if not isinstance(r[t], str) or not _TIME_RE.match(r[t]):
                    raise ValueError(f"reminders.{t} must be HH:MM")
                out["reminders"][t] = r[t]
    if "fridge_check" in data:
        fc = data["fridge_check"]
        if not isinstance(fc, dict):
            raise ValueError("fridge_check must be an object")
        if "enabled" in fc:
            if not isinstance(fc["enabled"], bool):
                raise ValueError("fridge_check.enabled must be true/false")
            out["fridge_check"]["enabled"] = fc["enabled"]
        if "day" in fc:
            if not isinstance(fc["day"], str) or fc["day"] not in WEEKDAYS:
                raise ValueError("fridge_check.day must be mon..sun")
            out["fridge_check"]["day"] = fc["day"]
        if "time" in fc:
            if not isinstance(fc["time"], str) or not _TIME_RE.match(fc["time"]):
                raise ValueError("fridge_check.time must be HH:MM")
            out["fridge_check"]["time"] = fc["time"]
    return out


def load() -> Dict[str, Any]:
    """Current settings (defaults if the file is missing or unreadable)."""
    try:
        with open(_path()) as f:
            raw = yaml.safe_load(f) or {}
        return validate(raw)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except (OSError, ValueError, yaml.YAMLError) as e:
        logger.error("[DIET] Could not read %s (%s) — using defaults", _path(), e)
        return copy.deepcopy(DEFAULTS)


def save(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a partial update, write atomically, audit. Raises ValueError."""
    if not isinstance(changes, dict):
        raise ValueError("diet settings must be an object")
    before = load()
    merged = {**before, **changes}
    if isinstance(changes.get("reminders"), dict):
        merged["reminders"] = {**before["reminders"], **changes["reminders"]}
    if isinstance(changes.get("fridge_check"), dict):
        merged["fridge_check"] = {**before["fridge_check"], **changes["fridge_check"]}
    after = validate(merged)
    os.makedirs(os.path.dirname(_path()) or ".", exist_ok=True)
    atomic_write_yaml(_path(), after)
    log_config_change("diet", before, after)
    return after


def public_profiles() -> List[Dict[str, str]]:
    return [{"key": k, "label": v["label"], "description": v["description"]} for k, v in PROFILES.items()]


def rules_for(settings: Dict[str, Any]) -> List[str]:
    rules = list(PROFILES.get(settings.get("profile"), PROFILES["balanced"])["rules"])
    rules.extend(settings.get("extra_rules") or [])
    return rules


def label_for(profile: Optional[str]) -> str:
    return PROFILES.get(profile or "", {}).get("label", profile or "")
