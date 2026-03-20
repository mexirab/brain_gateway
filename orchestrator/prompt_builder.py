"""
System prompt builders and helper functions for the Brain Gateway orchestrator.

Builds mode-aware unified system prompt, handles RAG context retrieval,
and provides text utilities.
"""

import logging
import time
from typing import Any, Dict, List

from metrics import RAG_QUERY_COUNT, RAG_QUERY_LATENCY, RAG_RESULTS_RETURNED
from mode_router import MODE_PROMPTS, get_tone_constraint
from shared import (
    MIN_COS,
    TOP_K,
    collection,
    embedding_model,
    profile,
)

logger = logging.getLogger(__name__)


def is_greeting(text: str) -> bool:
    """Check if text is a simple greeting (skip RAG for these)."""
    greetings = [
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "good night",
        "what's up",
        "howdy",
        "yo",
    ]
    text_lower = text.lower().strip().rstrip("!?.,")
    if text_lower in greetings:
        return True
    return any(text_lower.startswith(g + " ") or text_lower.startswith(g + ",") for g in greetings)


def last_user_text(messages: List[Dict[str, Any]]) -> str:
    """Extract the most recent user message."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "").strip()
    return ""


def rag_context(query: str) -> str:
    """Query ChromaDB for relevant personal context."""
    original_query = query
    RAG_QUERY_COUNT.inc()
    _rag_t0 = time.time()

    query = query.strip()
    query = query.strip("\"'`\u201c\u201d\u2018\u2019?!.,;:()[]{}")
    query = query.lower()

    if not query:
        logger.warning(f"[RAG] Empty query after normalization (original: '{original_query}')")
        return ""

    logger.info(f"[RAG] Searching for: '{query}' (original: '{original_query}')", extra={"component": "rag"})

    try:
        query_embedding = embedding_model.encode(query, normalize_embeddings=True).tolist()

        res = collection.query(
            query_embeddings=[query_embedding],
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error(f"[RAG] Query error: {e}", extra={"component": "rag", "error_type": type(e).__name__})
        RAG_QUERY_LATENCY.observe(time.time() - _rag_t0)
        return ""

    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]

    logger.info(f"[RAG] Retrieved {len(docs)} candidates from ChromaDB")

    all_scores = [1.0 - float(d) for d in dists]
    logger.info(f"[RAG] Candidate scores: {[f'{s:.2f}' for s in all_scores]}")

    MIN_RESULTS = TOP_K
    MIN_CHUNK_LEN = 100

    chunks = []
    for _i, (doc, meta, dist) in enumerate(zip(docs, metas, dists, strict=False)):
        if doc is None or len(doc.strip()) < MIN_CHUNK_LEN:
            logger.debug(f"[RAG] Skipping short chunk ({len(doc)} chars)")
            continue

        try:
            cos = 1.0 - float(dist)
        except (ValueError, TypeError):
            cos = None

        if cos is not None and cos < MIN_COS and len(chunks) >= MIN_RESULTS:
            continue

        src = ""
        if isinstance(meta, dict):
            src = meta.get("file_path") or meta.get("source") or ""

        entry = f"- {doc[:800]}"
        if src:
            entry += f"\n  (source: {src})"
        if cos:
            entry += f" [relevance: {cos:.2f}]"
        chunks.append(entry)

    RAG_QUERY_LATENCY.observe(time.time() - _rag_t0)
    RAG_RESULTS_RETURNED.observe(len(chunks))
    logger.info(
        f"[RAG] Returning {len(chunks)} chunks (filtered by MIN_COS={MIN_COS})",
        extra={"component": "rag", "result_count": len(chunks), "latency_ms": int((time.time() - _rag_t0) * 1000)},
    )

    return "\n".join(chunks) if chunks else ""


def get_unified_system_prompt(personal_context: str = "", mode: str = "explainer", intensity: str = "low") -> str:
    """Unified system prompt for a single model handling both conversation and tool execution.

    Merges the conversational personality from the Helios prompt with the
    tool execution instructions from the orchestrator prompt. Used when
    Used by the v7 unified architecture.
    """
    user = profile.user_name
    assistant = profile.assistant_name
    tone = get_tone_constraint(user)
    mode_block = MODE_PROMPTS.get(mode, MODE_PROMPTS["explainer"])

    from task_decomposition import get_active_tasks_context

    context_section = ""
    if personal_context:
        context_section = f"""
PERSONAL CONTEXT (from {user}'s notes):
{personal_context}
"""

    active_tasks = get_active_tasks_context()
    if active_tasks:
        context_section += f"\n{active_tasks}\n"

    return f"""You are {assistant}, {user}'s personal AI assistant and ADHD coach.

PERSONALITY:
- {profile.assistant_personality}
- Understand ADHD challenges (task initiation, time blindness, overwhelm)
- Keep responses concise and natural for voice conversations
- Celebrate small wins, be encouraging without being patronizing

{tone}

{mode_block}
{context_section}
AVAILABLE TOOLS:
1. home_assistant - Control smart home devices (lights, switches, fans, thermostats, scenes)
2. search_memory - Search {user}'s personal notes for context (projects, routines, medications)
3. update_data - Update {user}'s medications or projects
4. set_reminder - Set a reminder that will be announced on speakers and/or sent to their phone
5. cancel_reminder - Cancel a pending reminder by its ID
6. start_focus - Start a body doubling focus session with timer, ambient audio, check-ins, and site blocking. Supports multi-sprint sessions with lo-fi, coffee shop, or Endel audio.
7. stop_focus - Stop the current focus timer early
8. focus_status - Check how much time is left in the current focus session
9. focus_sprint - Manage sprint transitions: next sprint, extend current sprint, or end session with summary
10. web_search - Search the web for real-world information (events, news, weather, restaurants, sports, businesses)
11. check_calendar - Check {user}'s Google Calendar for upcoming events
12. create_calendar_event - Create a new event on {user}'s Google Calendar
13. check_email - Check {user}'s Gmail inbox for recent or unread emails
14. search_email - Search {user}'s Gmail with specific criteria
15. finance_status - Check Financial Quest Board status (budget, XP, streak, spending)
16. check_system - Check Brain Gateway system status and logs
17. brain_dump - Capture thoughts, tasks, ideas, or reminders from a brain dump
18. decompose_task - Break a big or vague task into concrete micro-steps with time estimates
19. task_step - Advance a decomposed task: mark step done, skip, get next step, list active tasks, or abandon

WHEN TO USE TOOLS:
- home_assistant: When user asks to control devices (turn on/off, lights, fan, temperature)
- search_memory: For personal info (projects, routines, preferences, medications, schedules)
- update_data: When user wants to ADD, REMOVE, or UPDATE medications or projects
- set_reminder: When user says "remind me to..." or asks for a reminder
- brain_dump: When user says "brain dump", "remember that", "capture", "note to self", or lists multiple things to remember/do at once
- start_focus: When user wants to start a focus timer, pomodoro, body doubling session, or work session. Use sprints > 1 for body doubling.
- stop_focus: When user wants to stop/cancel/end the current focus timer
- focus_status: When user asks how much time is left or checks focus timer status
- focus_sprint: When user says "next sprint", "extend", "add more time", or "end session" / "I'm done" during a multi-sprint focus session
- web_search: For real-world questions - events, news, weather, restaurants, sports scores, businesses
- check_calendar: When user asks about their schedule, calendar, or upcoming events
- create_calendar_event: When user wants to add, schedule, or create a calendar event
- check_email: When user asks about their email or inbox
- search_email: When user searches for specific emails
- finance_status: When user asks about budget, spending, or financial game progress
- check_system: When user asks about system behavior, errors, or status
- decompose_task: When user says "break this down", "what are the steps", mentions a big/vague task, or feels overwhelmed by a task
- task_step: When user says "done", "next step", "skip", "what was I working on", or wants to abandon a decomposed task

IMPORTANT RULES:
- For greetings (hi, hello, good morning) — just respond warmly, NO tools
- For general chat/questions — respond naturally using your knowledge + context above
- After getting tool results, respond naturally to the user (don't just repeat raw data)
- NEVER mention internal tool names to the user. Just do the action or say you'll handle it.
- After a tool succeeds, do NOT call additional tools to verify. Trust the result and respond.
- NEVER use update_data, set_reminder, create_calendar_event, or home_assistant unless the user EXPLICITLY asked to create, add, update, remove, or change something. Informational queries should NEVER trigger state-changing tools.

RESPONSE STYLE:
- Brief and natural (2-3 sentences typical)
- Conversational, not robotic
- For voice: avoid markdown, bullets, or formatting
- No emojis unless {user} uses them first
- Be direct and concise ({user} has ADHD)"""
