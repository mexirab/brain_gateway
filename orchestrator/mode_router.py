"""
Mode Router — Intent-based response mode classification.

Classifies user input into response modes (explainer, mirror, counterbalance,
challenge, baseline) with emotional intensity levels. Used to adapt Jess's
system prompt so she responds analytically to curiosity, supportively during
emotional moments, and firmly when asked for accountability.

Deterministic v1: keyword-based, no LLM classification.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RoutingResult:
    mode: str                       # explainer | mirror | counterbalance | challenge | baseline
    intensity: str                  # low | medium | high
    tags: list[str] = field(default_factory=list)  # detected signal tags for logging


# ── Mode-specific system prompt blocks ──────────────────────────────────────

MODE_PROMPTS = {
    "explainer": """MODE: EXPLAINER (analytical)
- Be analytical, structured, mechanism-focused
- No coaching language or emotional scaffolding
- Explain how/why things work clearly
- Only shift to support if distress appears mid-conversation""",

    "mirror": """MODE: MIRROR (reflective)
- Identify behavioral patterns, distortions, and strengths
- Reference what you know about Nadim from personal context
- End with ONE precise question to deepen self-awareness
- Do not lecture — reflect back what you observe""",

    "counterbalance": """MODE: COUNTERBALANCE (grounded support)
- Name urgency distortions and all-or-nothing thinking
- Reduce the emotional temperature without dismissing feelings
- Suggest small, repeatable actions (not grand plans)
- Avoid shame language — normalize the struggle""",

    "challenge": """MODE: CHALLENGE (accountability)
- Be firm but not shaming
- Provide ONE specific next action
- Include a time-bound suggestion ("by end of day", "in the next hour")
- Minimal validation — Nadim asked to be pushed""",

    "baseline": """MODE: BASELINE (low cognitive load)
- Reduce cognitive load — keep it simple
- Offer 2-3 concrete options, not open-ended questions
- Calm, steady tone
- Grounding techniques ARE appropriate here (this is high intensity)""",
}

TONE_CONSTRAINT = """TONE CONSTRAINT:
- Do NOT default to grounding techniques (breathing exercises, "take a deep breath", body scans)
- Only suggest grounding if intensity is high OR Nadim explicitly asks for calming help
- Match your energy to Nadim's — if he's analytical, be analytical"""


class ModeRouter:
    """Deterministic intent classifier for user messages."""

    # Step 1: Explicit intent overrides (phrase → mode)
    EXPLICIT_MIRROR_PHRASES = [
        "analyze me", "analyze my", "reflect on", "behavioral tendencies",
        "behavioral patterns", "my patterns", "what do you notice about me",
    ]
    EXPLICIT_CHALLENGE_PHRASES = [
        "hold me accountable", "don't let me off", "push me",
        "be tough", "be honest with me", "call me out",
        "don't sugarcoat", "no excuses",
    ]

    # Step 2: Emotional intensity keywords
    HIGH_INTENSITY = {
        "panic", "panicking", "hopeless", "killing me", "can't stop crying",
        "uncontrollable", "self-harm", "hurt myself", "can't breathe",
        "breaking down", "falling apart", "can't take it",
    }
    MEDIUM_INTENSITY = {
        "lonely", "shame", "ashamed", "rejected", "anxious", "anxiety",
        "depressed", "depression", "emptiness", "empty", "spiral",
        "spiraling", "crash", "crashing", "overwhelmed", "worthless",
        "numb", "stuck", "lost", "exhausted", "drained", "burned out",
    }

    # Step 3: Curiosity signals (mechanism language)
    CURIOSITY_PHRASES = [
        "how does", "how do", "why does", "why do", "what is",
        "what are", "what's the", "symbolism", "history of",
        "difference between", "explain", "tell me about",
        "how come", "what causes", "mechanism",
    ]

    def route(self, user_text: str) -> RoutingResult:
        """Classify user input into mode + intensity."""
        text_lower = user_text.lower().strip()
        tags: list[str] = []

        # ── Step 1: Explicit intent overrides ──
        for phrase in self.EXPLICIT_MIRROR_PHRASES:
            if phrase in text_lower:
                tags.append(f"explicit:{phrase}")
                intensity = self._classify_intensity(text_lower, tags)
                return RoutingResult(mode="mirror", intensity=intensity, tags=tags)

        for phrase in self.EXPLICIT_CHALLENGE_PHRASES:
            if phrase in text_lower:
                tags.append(f"explicit:{phrase}")
                intensity = self._classify_intensity(text_lower, tags)
                return RoutingResult(mode="challenge", intensity=intensity, tags=tags)

        # ── Step 2: Emotional intensity ──
        intensity = self._classify_intensity(text_lower, tags)

        # ── Step 3: Curiosity detection ──
        if intensity == "low":
            for phrase in self.CURIOSITY_PHRASES:
                if phrase in text_lower:
                    tags.append(f"curiosity:{phrase}")
                    return RoutingResult(mode="explainer", intensity="low", tags=tags)

        # ── Step 4: Default routing by intensity ──
        if intensity == "high":
            return RoutingResult(mode="baseline", intensity="high", tags=tags)
        elif intensity == "medium":
            return RoutingResult(mode="counterbalance", intensity="medium", tags=tags)
        else:
            return RoutingResult(mode="explainer", intensity="low", tags=tags)

    def _classify_intensity(self, text_lower: str, tags: list[str]) -> str:
        """Classify emotional intensity from text."""
        # Check high intensity (phrase matching for multi-word triggers)
        for trigger in self.HIGH_INTENSITY:
            if trigger in text_lower:
                tags.append(f"intensity_high:{trigger}")
                return "high"

        # Check medium intensity (word-level matching)
        words = set(re.findall(r'\b\w+\b', text_lower))
        for trigger in self.MEDIUM_INTENSITY:
            if trigger in words or trigger in text_lower:
                tags.append(f"intensity_medium:{trigger}")
                return "medium"

        return "low"


# ── Singleton ───────────────────────────────────────────────────────────────

_router: Optional[ModeRouter] = None


def get_mode_router() -> ModeRouter:
    """Get or create the singleton ModeRouter instance."""
    global _router
    if _router is None:
        _router = ModeRouter()
        logger.info("[MODE_ROUTER] Initialized deterministic v1 router")
    return _router
