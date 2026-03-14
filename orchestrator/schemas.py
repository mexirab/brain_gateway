"""
Pydantic models for API request/response validation.

All API endpoints should use these models instead of raw dicts.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Standard response envelope
# ---------------------------------------------------------------------------


class APIResponse(BaseModel):
    """Standard wrapper for all API responses."""

    ok: bool = True
    error: Optional[str] = None

    class Config:
        extra = "allow"  # allow additional fields per-endpoint


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    stream: bool = False
    tools: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Focus timer
# ---------------------------------------------------------------------------


class FocusStartRequest(BaseModel):
    task: str = "focus session"
    duration: int = Field(default=25, ge=1, le=480, description="Minutes")
    break_duration: int = Field(default=5, ge=1, le=60, description="Minutes")
    speaker: Optional[str] = None
    soundscape: str = "focus"


class FocusStatus(APIResponse):
    active: bool
    task: Optional[str] = None
    elapsed_minutes: Optional[float] = None
    remaining_minutes: Optional[float] = None
    duration: Optional[int] = None
    break_duration: Optional[int] = None
    started: Optional[str] = None


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


class ReminderTriggerRequest(BaseModel):
    reminder_id: str


# ---------------------------------------------------------------------------
# Memory / RAG
# ---------------------------------------------------------------------------


class MemoryAddRequest(BaseModel):
    text: str = Field(min_length=1)
    category: str = "general"
    source: str = "manual"
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# HA commands
# ---------------------------------------------------------------------------


class HACommandRequest(BaseModel):
    command: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


class AnnounceRequest(BaseModel):
    text: str = Field(min_length=1)
    speaker: Optional[str] = None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str
    end: str
    location: Optional[str] = None
    description: Optional[str] = None
    all_day: bool = False
    calendar: str = ""
    source: str = ""


class CalendarTodayResponse(APIResponse):
    events: list[CalendarEvent] = Field(default_factory=list)
    source: str = "none"
    count: int = 0
