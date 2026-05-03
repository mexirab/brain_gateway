"""
System prompt builders and helper functions for the Brain Gateway orchestrator.

Builds mode-aware unified system prompt, handles RAG context retrieval,
and provides text utilities.
"""

import logging
import time
from typing import Any, Dict, List

from orchestrator import shared
from orchestrator.metrics import RAG_QUERY_COUNT, RAG_QUERY_LATENCY, RAG_RESULTS_RETURNED
from orchestrator.mode_router import MODE_PROMPTS, get_tone_constraint
from orchestrator.shared import (
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


def rag_context(query: str, wing: str = "", room: str = "") -> str:
    """Query ChromaDB for relevant personal context, optionally filtered by wing/room."""
    original_query = query
    RAG_QUERY_COUNT.inc()
    _rag_t0 = time.time()

    query = query.strip()
    query = query.strip("\"'`\u201c\u201d\u2018\u2019?!.,;:()[]{}")
    query = query.lower()

    if not query:
        logger.warning(f"[RAG] Empty query after normalization (original: '{original_query}')")
        return ""

    # Soft-fail: unknown wing → drop the filter rather than returning zero
    # results. Uses the palace config as the source of truth; falls back
    # to accepting the value if palace isn't available.
    if wing:
        try:
            from orchestrator.shared import get_palace

            if not get_palace().is_known_wing(wing):
                logger.warning("[RAG] Ignoring unknown wing filter: %r", wing)
                wing = ""
        except Exception:
            pass  # palace unavailable — fall through and pass to ChromaDB

    filter_desc = f", wing={wing}, room={room}" if wing or room else ""
    logger.info(
        f"[RAG] Searching for: '{query}' (original: '{original_query}'{filter_desc})", extra={"component": "rag"}
    )

    try:
        query_embedding = embedding_model.encode(query, normalize_embeddings=True).tolist()

        query_kwargs = {
            "query_embeddings": [query_embedding],
            "n_results": TOP_K,
            "include": ["documents", "metadatas", "distances"],
        }

        # Build optional wing/room filter
        conditions = []
        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})
        if len(conditions) == 1:
            query_kwargs["where"] = conditions[0]
        elif len(conditions) > 1:
            query_kwargs["where"] = {"$and": conditions}

        res = collection.query(**query_kwargs)
    except Exception as e:
        logger.error(f"[RAG] Query error: {e}", extra={"component": "rag", "error_type": type(e).__name__})
        RAG_QUERY_LATENCY.observe(time.time() - _rag_t0)
        return ""

    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]

    logger.info(f"[RAG] Retrieved {len(docs)} candidates from ChromaDB")

    # Log cosine similarity using the same formula as the filter (1 - dist/2
    # for ChromaDB's default L2² on normalized vectors). The prior formula
    # (1 - dist) was a legacy holdover that made debugging misleading —
    # logged "scores" looked negative while actual cos values were > 0.2.
    all_scores = [1.0 - float(d) / 2.0 for d in dists]
    logger.info(f"[RAG] Candidate cos: {[f'{s:.2f}' for s in all_scores]}")

    MIN_CHUNK_LEN = 100

    chunks = []
    for _i, (doc, meta, dist) in enumerate(zip(docs, metas, dists, strict=False)):
        if doc is None or len(doc.strip()) < MIN_CHUNK_LEN:
            logger.debug(f"[RAG] Skipping short chunk ({len(doc)} chars)")
            continue

        try:
            # ChromaDB returns squared L2 distance by default. For the
            # normalized vectors we use, L2² = 2(1 - cos_sim), so the true
            # cosine similarity is 1 - dist/2 (NOT 1 - dist as older code
            # assumed, which silently halved the computed similarity and
            # made MIN_COS filtering nearly no-op).
            cos = 1.0 - float(dist) / 2.0
        except (ValueError, TypeError):
            cos = None

        # Hard MIN_COS floor — previously soft-bounded by a MIN_RESULTS=TOP_K
        # minimum, which kept negative-similarity chunks in the prompt every
        # turn and bloated prefill latency by ~700 tokens on voice queries.
        if cos is not None and cos < MIN_COS:
            continue

        src = ""
        location = ""
        if isinstance(meta, dict):
            src = meta.get("file_path") or meta.get("source") or ""
            # Use distinct names so we don't shadow the function's wing/room filter params
            doc_wing = meta.get("wing", "")
            doc_room = meta.get("room", "")
            if doc_wing:
                location = f"{doc_wing}/{doc_room}" if doc_room else doc_wing

        # Decrypt Fernet-encrypted chunks (auto_learn facts). Detection is
        # either metadata-driven (encrypted="true") or format-driven (every
        # Fernet token starts with "gAAAAAB" for v0 tokens). We try both
        # so chunks written by older code paths still render correctly.
        display_doc = doc
        is_encrypted = (
            isinstance(meta, dict) and str(meta.get("encrypted", "")).lower() == "true"
        ) or display_doc.startswith("gAAAAAB")
        if is_encrypted:
            try:
                from orchestrator.auto_learn import decrypt_text

                display_doc = decrypt_text(display_doc)
            except Exception as e:
                logger.debug("[RAG] Decryption failed for chunk: %s", e)
                # fall through with ciphertext — better than losing the result

        entry = f"- {display_doc[:800]}"
        if location:
            entry += f"\n  (palace: {location})"
        elif src:
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


def _resolve_tone(user_name: str, prof) -> str:
    """Return the tone block for the system prompt.

    - If the user disabled ADHD mode, swap in a neutral tone instruction
      so the prompt stops asserting an ADHD-coaching frame.
    - If `tone_preference` is set ("warm" | "balanced" | "direct"), use a
      preset block keyed off that choice.
    - Otherwise fall back to the legacy `get_tone_constraint(user)` block.
    """
    adhd_on = bool(getattr(prof, "adhd_mode", True))
    tone_pref = (getattr(prof, "tone_preference", "") or "").strip().lower()

    if not adhd_on:
        return (
            "TONE:\n"
            f"- Be helpful and concise. Match {user_name}'s energy.\n"
            "- Skip therapeutic framing unless explicitly asked."
        )

    presets = {
        "warm": (
            "TONE (warm):\n"
            f"- Lead with empathy. Validate before redirecting.\n"
            f"- Match {user_name}'s energy; never lecture."
        ),
        "balanced": (
            "TONE (balanced):\n"
            "- Mix warmth with directness. Acknowledge feelings briefly, then move to action.\n"
            f"- Match {user_name}'s energy."
        ),
        "direct": (
            "TONE (direct):\n"
            "- Skip the warm-up. Lead with the answer or the next step.\n"
            "- Don't soften or pad. Plain speech only."
        ),
    }
    if tone_pref in presets:
        return presets[tone_pref]

    return get_tone_constraint(user_name)


def get_unified_system_prompt(
    personal_context: str = "",
    mode: str = "explainer",
    intensity: str = "low",
    is_voice: bool = False,
) -> str:
    """Unified system prompt for a single model handling both conversation and tool execution.

    Merges the conversational personality from the Helios prompt with the
    tool execution instructions from the orchestrator prompt. Used by the v7
    unified architecture.

    When ``is_voice=True`` the AVAILABLE TOOLS and WHEN TO USE TOOLS sections
    are dropped — they duplicate the JSON tool schemas the model already sees
    in the ``tools`` parameter and were costing ~2.3k prefill tokens per turn.
    The DECISION HELPER and IMPORTANT RULES sections are kept because they
    carry behavior the schemas don't encode (selfcare mandatory logging etc.).
    """
    user = profile.user_name
    assistant = profile.assistant_name
    tone = _resolve_tone(user, profile)
    mode_block = MODE_PROMPTS.get(mode, MODE_PROMPTS["explainer"])

    from orchestrator.task_decomposition import get_active_tasks_context

    context_section = ""
    if personal_context:
        context_section = f"""
PERSONAL CONTEXT (from {user}'s notes):
{personal_context}
"""

    active_tasks = get_active_tasks_context()
    if active_tasks:
        context_section += f"\n{active_tasks}\n"

    from orchestrator.routine_manager import get_active_routine_context

    routine_context = get_active_routine_context()
    if routine_context:
        context_section += f"\n{routine_context}\n"

    from orchestrator.context_tracker import get_active_context_summary

    interrupt_context = get_active_context_summary()
    if interrupt_context:
        context_section += f"\n{interrupt_context}\n"

    try:
        from orchestrator.presence_tracker import get_presence_prompt_context

        presence_ctx = get_presence_prompt_context()
        if presence_ctx:
            context_section += f"\nLOCATION: {presence_ctx}\n"
    except Exception:
        pass

    # MemPalace wakeup context
    if shared.PALACE_ENABLED and shared.PALACE_WAKEUP_ENABLED:
        try:
            from orchestrator.shared import get_palace

            palace = get_palace()
            wakeup = palace.generate_wakeup_context()
            if wakeup:
                context_section += f"\nIDENTITY CONTEXT (from memory palace):\n{wakeup}\n"
        except Exception:
            pass

    from datetime import datetime

    now = datetime.now()
    date_str = now.strftime("%A, %B %-d, %Y at %-I:%M %p")

    # ask_expert guidance is conditional: only present when the expert tool is
    # both ENABLED and NOT being hidden from the voice path. Injecting the
    # guidance when the tool isn't in the schema would lead the model to
    # reference a tool that doesn't exist for that turn. Voice also strips
    # the whole AVAILABLE TOOLS...DECISION HELPER block below, so this is
    # belt-and-suspenders.
    if shared.EXPERT_ENABLED and not is_voice:
        expert_section = (
            "- ask_expert: Delegate a HARD reasoning task to the expert model "
            "(Qwen3-32B Thinking on Saturn 3090). Use for multi-step math, "
            "complex planning, debug analyses, research syntheses, or when the "
            f'user explicitly says "ask the expert", "think harder", '
            '"reason through this", or similar. DO NOT use for simple questions, '
            "home_assistant tasks, reminders, calendar, email, or anything involving "
            "live system state — those are YOUR job. The expert has no tools and no "
            "memory of this conversation, so bake any needed context into the "
            f"`question` argument. Latency is 30-150 seconds — ALWAYS warn {user} "
            'first ("let me think carefully about this, it\'ll take a minute") so '
            "they know nothing is hung. Don't call it twice in one turn."
        )
    else:
        expert_section = ""

    prompt = f"""You are {assistant}, {user}'s personal AI assistant and ADHD coach.

CURRENT DATE/TIME: {date_str}

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
2. search_memory - Search {user}'s memory palace for context. Organized into wings (personal, brain_gateway, conjure, infrastructure, jess) with rooms (health, routines, architecture, etc.). Use wing/room to narrow searches.
3. update_data - Update {user}'s medications or projects (persists to YAML, source-of-truth for meds/projects — ALWAYS use this for med schedule changes instead of update_memory)
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
20. start_routine - Start a morning or evening routine with step-by-step TTS guidance
21. routine_action - Advance the active routine: done, skip, pause, resume, stop, or status
22. routine_status - Check current routine progress
23. decide_for_me - Help user decide what to do when stuck or overwhelmed (gathers context, you synthesize)
24. selfcare_log - Log a self-care action (meal, medication, water, movement)
25. bookmark_context - Save current work context before stepping away (interruption recovery)
26. recall_context - Recall recent work context when returning from an interruption
27. update_memory - Correct or update a fact in RAG memory (NOT for medications/projects — use update_data for those). For general factual corrections only.
28. check_claude_activity - See what Claude Code (the CLI coding assistant) has been working on. Use for self-troubleshooting when recent code changes might be relevant.
29. query_budget - Query historical budget/spending data imported from CSV/Excel (separate from live YNAB). Use for past-spending questions: totals, category breakdowns, monthly trends, outliers.

WHEN TO USE TOOLS:
- home_assistant: When user asks to control devices (turn on/off, lights, fan, temperature)
- search_memory: For PERSONAL info (projects, routines, preferences, medications, schedules). Use wing param to narrow by domain (personal, brain_gateway, infrastructure, jess, conjure) and room for specific topics (health, routines, architecture, debugging). NOT for external/real-world info — events, restaurants, businesses, weather, news, sports, "things to do" — use web_search for those.
- update_data: **PREFERRED for structured data** — ALWAYS use this when the user wants to change medications (add/remove/update/change dose/change schedule) or projects (status, steps, goals). This updates the source-of-truth YAML file. Use even when the user says phrases like "update your memory about my meds", "remember that I moved my Vyvanse to evening", "I take Naltrexone in the morning now". Medication schedule changes ALWAYS go through update_data, never update_memory.
- update_memory: For OTHER factual corrections that aren't medications or projects — e.g., "actually I live in Austin now", "my sister's name is Sara not Sarah", "I prefer Python not JavaScript". Use this ONLY when no dedicated tool (update_data, selfcare_log, etc.) handles the domain. If the correction is about meds, projects, or anything with a dedicated tool, use that tool instead.
- set_reminder: When user says "remind me to..." or asks for a reminder
- brain_dump: When user says "brain dump", "remember that", "capture", "note to self", or lists multiple things to remember/do at once
- start_focus: When user wants to start a focus timer, pomodoro, body doubling session, or work session. Use sprints > 1 for body doubling.
- stop_focus: When user wants to stop/cancel/end the current focus timer
- focus_status: When user asks how much time is left or checks focus timer status
- focus_sprint: When user says "next sprint", "extend", "add more time", or "end session" / "I'm done" during a multi-sprint focus session
- web_search: For real-world questions — events, activities, attractions, things to do, news, weather, restaurants, sports scores, businesses, local info ("in Houston", "near me"). You HAVE live web access via this tool — NEVER say "I can't browse the web" or "I don't have internet access". If search_memory returns nothing for an external/world-facing question, call web_search; don't give up after one failed tool call.
- check_calendar: When user asks about their schedule, calendar, or upcoming events
- create_calendar_event: When user wants to add, schedule, or create a calendar event
- check_email: When user asks about their email or inbox
- search_email: When user searches for specific emails
- finance_status: When user asks about budget, spending, or financial game progress IN THE CURRENT PERIOD (live YNAB). For historical / imported CSV budgets, use query_budget instead.
- query_budget: When user asks about PAST spending from imported CSV/Excel budgets. Call list_datasets ONCE if you don't know the dataset. For synthesis questions ("find patterns", "biggest X in period Y", "what stood out", "why did I overspend", "analyze", "compare years"): use question_type='analyze' with analysis_question=<user's actual question>. That ONE call gathers the data AND delegates to the expert reasoning model; its expert_synthesis field IS the answer — don't follow up with more query_budget calls. Only use by_category/by_payee/by_month/total/outliers for narrow per-dimension facts, never for synthesis.
- check_system: When user asks about system behavior, errors, or status
- decompose_task: When user says "break this down", "what are the steps", mentions a big/vague task, or feels overwhelmed by a task
- task_step: When user says "done", "next step", "skip", "what was I working on", or wants to abandon a decomposed task
- start_routine: When user says "start morning routine", "let's do the routine", "I'm up", or the morning/evening routine is auto-triggered
- routine_action: When user says "done", "next", "finished", "skip", "pause routine", "resume routine", "stop routine" during an active routine
- routine_status: When user asks "where am I in the routine" or "what's the current step"
- decide_for_me: When user says "what should I do", "what should I work on", "I'm overwhelmed", "I can't decide", "what should I eat", or seems stuck with choice paralysis
- sleep_mode: When user says "mute", "be quiet", "shut up", "people are over", "guests coming", "mute for X hours", "goodnight", "bedtime" (action=on). Use duration_hours when they specify a time ("mute for 3 hours"). When user says "unmute", "good morning", "you can talk again" (action=off).
- selfcare_log: ALWAYS call this when user mentions eating, meals, meds, water, or exercise. Examples: "I ate", "had lunch", "I took my meds", "yes I took it", "just had a sandwich", "drank water", "went for a walk", "grabbed a snack", "just ate". This MUST be logged even if you also respond conversationally. Use action="check" when user asks "did I take my meds?", "have I eaten?", "what have I logged today?" — returns current status without logging anything.
- ANNOUNCEMENT ACKNOWLEDGMENTS: When you see a prior "[Jess announced - ...]" message in the conversation and the user replies with a short ack like "okay", "done", "I just did", "yep", "already did", "took it" — infer what they're confirming from the announcement context and call the appropriate tool (selfcare_log for meds/meals/water/movement, or respond that the reminder is noted). Don't ask them to clarify if the context is obvious.
- document_vault: Use 'search' when user asks about a stored document ("where's my car title?", "what's my VIN?"). Use 'update' when user provides details about a document ("my VIN is XXXXX", "add this to my car title"). First search to find the doc and get its ID, then update with the notes. The notes field is indexed in RAG so the info becomes searchable.
- bookmark_context: When user says "I need to take a call", "stepping away", "be right back", "brb", "I need to handle something"
- recall_context: When user says "what was I doing?", "where was I?", "what was I working on?", "I'm back", "just got back"
- check_claude_activity: When {user} asks you to troubleshoot yourself, mentions something that "just broke" or "stopped working", or when a code-related question might be explained by recent Claude Code edits. Action="recent" gives you a compact summary of the last ~2 hours of activity. Action="files_touched" tells you which files changed. Use this BEFORE code_agent when the issue is potentially recent.
- code_agent: When user asks about how something works in your code, asks you to troubleshoot a code issue, investigate a bug, look at a specific file, search the codebase, run tests, or implement a change. Examples: "how do meal nudges work?", "look at selfcare_manager.py", "why is the calendar polling failing?", "search for where reminders are sent", "run the tests". Use apply_changes=true ONLY when user explicitly asks you to make changes.
{expert_section}
DECISION HELPER (decide_for_me):
- When using decide_for_me: return ONE concrete recommendation for work/overwhelm, or TWO options max for food/general
- Never present more than 2 options — user wants you to make the call
- Be directive, not wishy-washy: "Do X" not "You could try X or Y or Z"
- For overwhelm: single most important thing, dismiss everything else
- Triage priority: meds not taken > imminent deadline > smallest quick win > "you're fine, take a break"

IMPORTANT RULES:
- MANDATORY LOGGING: When {user} mentions eating, meals, meds, water, or exercise, you MUST call selfcare_log BEFORE responding. Never confirm a meal/med/water log without actually calling the tool — if the tool isn't called, the system won't know and will keep nagging.
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

    # Voice mode: strip the AVAILABLE TOOLS + WHEN TO USE TOOLS sections.
    # They duplicate the JSON tool schemas sent in the ``tools`` parameter and
    # were costing ~2.3k prefill tokens per voice turn. DECISION HELPER and
    # IMPORTANT RULES stay — they carry behavior the schemas don't encode.
    if is_voice:
        import re as _re

        prompt = _re.sub(
            r"\nAVAILABLE TOOLS:.*?(?=\nDECISION HELPER)",
            "\n",
            prompt,
            count=1,
            flags=_re.DOTALL,
        )

    return prompt
