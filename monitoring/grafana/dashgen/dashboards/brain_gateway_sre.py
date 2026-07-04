"""
brain-gateway-sre — full application telemetry.

Replaces the old "Brain Gateway Deep Dive" which was a duplicate of Overview.
This dashboard covers every metric defined in orchestrator/metrics.py, grouped
by subsystem into 12 collapsible rows. Use when Jess Glance shows a symptom
but you need to drill into which subsystem is misbehaving.

Template variables:
  $tool        — filter tool-call panels to one tool
  $mode        — filter request panels to one routing mode
  $speaker     — filter TTS panels to one target speaker
  $wing        — filter palace panels to one memory wing
  $request_id  — paste a request_id to trace a single request in logs

Layout: rows are expanded by default. Grafana users can collapse whichever
row they don't need. Each row is 24 columns wide with panels split evenly.
"""

from __future__ import annotations

from ..dashboard import dashboard, var_query, var_text
from ..layout import assign_ids, grid_row, row_divider
from ..panels import (
    EXPERT_CIRCUIT_THRESHOLDS,
    EXPERT_LATENCY_THRESHOLDS_S,
    GREEN_RED_BINARY,
    LATENCY_THRESHOLDS_S,
    VOICE_LATENCY_THRESHOLDS_S,
    VOICE_TTS_THRESHOLDS_S,
    gauge,
    logs,
    piechart,
    stat,
    timeseries,
)


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # -------------------------------------------------------- Request Flow
    r, y = row_divider("Request Flow", y)
    panels.append(r)

    request_flow = [
        timeseries(
            "Request Rate by Mode",
            [('sum by (mode) (rate(bgw_requests_total{mode=~"$mode"}[5m])) * 60', "{{mode}}")],
            unit="none",
            stack=True,
            fill=40,
        ),
        timeseries(
            "Request Latency (p50 / p95 / p99) by Mode",
            [
                (
                    'histogram_quantile(0.50, sum by (le, mode) (rate(bgw_request_duration_seconds_bucket{mode=~"$mode"}[5m])))',
                    "p50 {{mode}}",
                ),
                (
                    'histogram_quantile(0.95, sum by (le, mode) (rate(bgw_request_duration_seconds_bucket{mode=~"$mode"}[5m])))',
                    "p95 {{mode}}",
                ),
                (
                    'histogram_quantile(0.99, sum by (le, mode) (rate(bgw_request_duration_seconds_bucket{mode=~"$mode"}[5m])))',
                    "p99 {{mode}}",
                ),
            ],
            unit="s",
        ),
        timeseries(
            "Request Errors by Type",
            [("sum by (error_type) (rate(bgw_request_errors_total[5m])) * 60", "{{error_type}}")],
            unit="none",
            stack=True,
            fill=60,
        ),
        timeseries(
            "Active Requests + Tool Loop Rounds",
            [
                ("bgw_active_requests", "active requests"),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_tool_loop_rounds_bucket[10m])))",
                    "p95 loop rounds",
                ),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(request_flow, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Voice Pipeline
    r, y = row_divider("Voice Pipeline", y)
    panels.append(r)

    voice_desc = (
        "Text-in to text-out latency for voice-channel requests. Covers the "
        "LLM + tool-loop slice we control; excludes Whisper STT upstream and "
        "TTS synthesis + speaker playback downstream. Target: keep p95 under "
        "13s (prior floor). Investigate spikes with the logs row below."
    )
    voice_row = [
        timeseries(
            "Voice Pipeline Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_voice_pipeline_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_voice_pipeline_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_voice_pipeline_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            thresholds=VOICE_LATENCY_THRESHOLDS_S,
            description=voice_desc,
        ),
        timeseries(
            "Voice Request Rate",
            [("sum(rate(bgw_voice_pipeline_seconds_count[5m])) * 60", "voice turns/min")],
            unit="none",
        ),
        stat(
            "Voice Pipeline Mean (5m)",
            "(sum(rate(bgw_voice_pipeline_seconds_sum[5m])) "
            "/ sum(rate(bgw_voice_pipeline_seconds_count[5m]))) "
            "or on() vector(0)",
            unit="s",
            decimals=1,
            thresholds=VOICE_LATENCY_THRESHOLDS_S,
        ),
        stat(
            "Voice Turns (24h)",
            "sum(increase(bgw_voice_pipeline_seconds_count[24h]))",
            unit="none",
        ),
    ]
    row, y = grid_row(voice_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- TTS Synthesis
    tts_desc = (
        "TTS round-trip at the orchestrator's /v1/audio/speech proxy (the hop "
        "between OWUI and Qwen3-TTS on port 8002). Excludes browser playback. "
        "The proxy also prepends ~150ms of silence so the first-word stutter "
        "from cold prosody state doesn't reach the user — see TTS_SILENCE_PAD_MS."
    )
    tts_row = [
        timeseries(
            "TTS Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_voice_tts_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_voice_tts_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_voice_tts_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            thresholds=VOICE_TTS_THRESHOLDS_S,
            description=tts_desc,
        ),
        timeseries(
            "TTS Call Rate",
            [("sum(rate(bgw_voice_tts_seconds_count[5m])) * 60", "tts calls/min")],
            unit="none",
        ),
        stat(
            "TTS Mean (5m)",
            "(sum(rate(bgw_voice_tts_seconds_sum[5m])) / sum(rate(bgw_voice_tts_seconds_count[5m]))) or on() vector(0)",
            unit="s",
            decimals=2,
            thresholds=VOICE_TTS_THRESHOLDS_S,
        ),
        stat(
            "TTS Calls (24h)",
            "sum(increase(bgw_voice_tts_seconds_count[24h]))",
            unit="none",
        ),
    ]
    row, y = grid_row(tts_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- LLM Performance
    r, y = row_divider("LLM Performance", y)
    panels.append(r)

    llm_row = [
        timeseries(
            "LLM Calls by Model + Purpose",
            [("sum by (model, purpose) (rate(bgw_llm_calls_total[5m])) * 60", "{{model}} / {{purpose}}")],
            unit="none",
            stack=True,
            fill=40,
        ),
        timeseries(
            "LLM Latency p95 by Purpose",
            [
                (
                    "histogram_quantile(0.95, sum by (le, purpose) (rate(bgw_llm_call_duration_seconds_bucket[5m])))",
                    "{{purpose}}",
                )
            ],
            unit="s",
            thresholds=LATENCY_THRESHOLDS_S,
        ),
        timeseries(
            "LLM Errors by Type",
            [("sum by (model, error_type) (rate(bgw_llm_call_errors_total[5m])) * 60", "{{model}} / {{error_type}}")],
            unit="none",
            stack=True,
        ),
        stat(
            "Primary / Fallback Online",
            "bgw_helios_online + (2 * bgw_fallback_online)",
            text_mode="value_and_name",
            color_mode="background",
            graph_mode="none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "BOTH DOWN", "color": "red", "index": 0},
                        "1": {"text": "PRIMARY ONLY", "color": "green", "index": 1},
                        "2": {"text": "FALLBACK ONLY", "color": "yellow", "index": 2},
                        "3": {"text": "BOTH UP", "color": "green", "index": 3},
                    },
                }
            ],
            thresholds=GREEN_RED_BINARY,
        ),
    ]
    row, y = grid_row(llm_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Expert Agent
    r, y = row_divider("Expert Agent (Saturn 3090 Qwen3-32B)", y)
    panels.append(r)

    expert_desc = (
        "ask_expert tool delegates hard reasoning to Qwen3-32B Thinking on "
        "Saturn 3090 (10.0.0.58:8084). 30-150s latency is normal for full "
        "reasoning passes. Circuit breaker opens after N consecutive failures "
        "(EXPERT_CIRCUIT_BREAKER_FAILURES, default 3); the stat goes red."
    )
    expert_row = [
        stat(
            "Circuit Breaker",
            "bgw_expert_circuit_open",
            text_mode="value_and_name",
            color_mode="background",
            graph_mode="none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "CLOSED", "color": "green", "index": 0},
                        "1": {"text": "OPEN", "color": "red", "index": 1},
                    },
                }
            ],
            thresholds=EXPERT_CIRCUIT_THRESHOLDS,
        ),
        timeseries(
            "Expert Calls by Result",
            [
                (
                    "sum by (result) (rate(bgw_expert_call_count_total[5m])) * 60",
                    "{{result}}",
                )
            ],
            unit="none",
            stack=True,
            fill=40,
            description=expert_desc,
        ),
        timeseries(
            "Expert Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_expert_call_latency_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_expert_call_latency_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_expert_call_latency_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            thresholds=EXPERT_LATENCY_THRESHOLDS_S,
        ),
        stat(
            "Expert Reasoning Tokens (mean)",
            "(sum(rate(bgw_expert_reasoning_tokens_sum[5m])) "
            "/ sum(rate(bgw_expert_reasoning_tokens_count[5m]))) "
            "or on() vector(0)",
            unit="none",
            decimals=0,
        ),
    ]
    row, y = grid_row(expert_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Tools
    r, y = row_divider("Tools", y)
    panels.append(r)

    tools_row = [
        timeseries(
            "Tool Call Rate (filtered by $tool)",
            [('sum by (tool) (rate(bgw_tool_calls_total{tool=~"$tool"}[5m])) * 60', "{{tool}}")],
            unit="none",
            stack=True,
            fill=60,
        ),
        timeseries(
            "Tool Latency p95 (filtered by $tool)",
            [
                (
                    'histogram_quantile(0.95, sum by (le, tool) (rate(bgw_tool_call_duration_seconds_bucket{tool=~"$tool"}[5m])))',
                    "{{tool}}",
                )
            ],
            unit="s",
            thresholds=LATENCY_THRESHOLDS_S,
        ),
        timeseries(
            "Tool Errors (filtered by $tool)",
            [('sum by (tool) (rate(bgw_tool_call_errors_total{tool=~"$tool"}[5m])) * 60', "{{tool}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(tools_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Memory (Palace + RAG)
    r, y = row_divider("Memory (MemPalace + RAG)", y)
    panels.append(r)

    memory_row = [
        timeseries(
            "Palace Stores by Wing (filtered by $wing)",
            [('sum by (wing) (rate(bgw_palace_stores_total{wing=~"$wing"}[5m])) * 60', "{{wing}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Palace Search Latency p50/p95",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_palace_search_duration_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_palace_search_duration_seconds_bucket[5m])))",
                    "p95",
                ),
            ],
            unit="s",
        ),
        stat(
            "Total Memories",
            "bgw_palace_memories_total",
            unit="none",
            graph_mode="area",
        ),
        timeseries(
            "RAG Query Latency + Results/Query",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_rag_query_duration_seconds_bucket[5m])))",
                    "p95 latency",
                ),
                (
                    "histogram_quantile(0.5, sum by (le) (rate(bgw_rag_results_count_bucket[5m])))",
                    "p50 results",
                ),
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(memory_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Auto-Learn
    r, y = row_divider("Auto-Learn", y)
    panels.append(r)

    autolearn_row = [
        timeseries(
            "Extractions + Facts Stored",
            [
                ("rate(bgw_auto_learn_extractions_total[15m]) * 60", "extractions/min"),
                ("sum by (category) (rate(bgw_auto_learn_facts_stored_total[15m])) * 60", "stored {{category}}"),
            ],
            unit="none",
        ),
        timeseries(
            "Dedup + Sensitive Filtered",
            [
                ("rate(bgw_auto_learn_duplicates_skipped_total[15m]) * 60", "dedup skipped"),
                ("rate(bgw_auto_learn_sensitive_filtered_total[15m]) * 60", "sensitive filtered"),
            ],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Extraction Latency p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_auto_learn_extraction_duration_seconds_bucket[15m])))",
                    "p95",
                )
            ],
            unit="s",
            thresholds=LATENCY_THRESHOLDS_S,
        ),
    ]
    row, y = grid_row(autolearn_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Brain Dump
    r, y = row_divider("Brain Dump", y)
    panels.append(r)

    braindump_row = [
        timeseries(
            "Items Captured by Category",
            [("sum by (category) (rate(bgw_brain_dump_items_captured_total[15m])) * 60", "{{category}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Items Routed by Destination",
            [("sum by (destination) (rate(bgw_brain_dump_items_routed_total[15m])) * 60", "{{destination}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "RAG Upsert Latency p95 + Errors",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_brain_dump_rag_duration_seconds_bucket[15m])))",
                    "p95 latency",
                ),
                ("sum by (operation) (rate(bgw_brain_dump_errors_total[15m])) * 60", "errors {{operation}}"),
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(braindump_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Task Decomposition
    r, y = row_divider("Task Decomposition", y)
    panels.append(r)

    taskdecomp_row = [
        timeseries(
            "Task Lifecycle",
            [
                ("rate(bgw_task_decomp_tasks_created_total[15m]) * 60", "created"),
                ("rate(bgw_task_decomp_tasks_abandoned_total[15m]) * 60", "abandoned"),
            ],
            unit="none",
        ),
        timeseries(
            "Step Outcomes",
            [
                ("rate(bgw_task_decomp_steps_completed_total[15m]) * 60", "completed"),
                ("rate(bgw_task_decomp_steps_skipped_total[15m]) * 60", "skipped"),
            ],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Errors",
            [("rate(bgw_task_decomp_errors_total[15m]) * 60", "errors/min")],
            unit="none",
        ),
    ]
    row, y = grid_row(taskdecomp_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Progress Tracking
    r, y = row_divider("Progress Tracking", y)
    panels.append(r)

    progress_row = [
        timeseries(
            "Events by Type",
            [("sum by (event_type) (rate(bgw_progress_events_total[15m])) * 60", "{{event_type}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Streak Milestones by Category",
            [("sum by (category) (rate(bgw_progress_streak_milestones_total[1h])) * 3600", "{{category}}")],
            unit="none",
        ),
    ]
    row, y = grid_row(progress_row, y, heights=[8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Focus Timer
    r, y = row_divider("Focus Timer", y)
    panels.append(r)

    focus_row = [
        timeseries(
            "Starts by Soundscape + Active",
            [
                ("sum by (soundscape) (rate(bgw_focus_sessions_started_total[1h])) * 3600", "started {{soundscape}}"),
                ("bgw_focus_active", "active"),
            ],
            unit="none",
        ),
        gauge(
            "Completion Rate (24h)",
            "(sum(increase(bgw_focus_sessions_completed_total[24h])) / clamp_min(sum(increase(bgw_focus_sessions_started_total[24h])), 1)) * 100",
            unit="percent",
            thresholds=[(None, "red"), (40, "yellow"), (70, "green")],
        ),
        timeseries(
            "Session Duration Distribution",
            [
                (
                    "histogram_quantile(0.5, sum by (le) (rate(bgw_focus_session_actual_minutes_bucket[24h])))",
                    "p50 min",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_focus_session_actual_minutes_bucket[24h])))",
                    "p95 min",
                ),
            ],
            unit="m",
        ),
        timeseries(
            "Pi-hole Blocking Toggles",
            [("sum by (action) (rate(bgw_pihole_blocking_toggles_total[1h])) * 3600", "{{action}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(focus_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Calendar + Gmail + Email→Cal
    r, y = row_divider("Calendar + Gmail + Email→Calendar", y)
    panels.append(r)

    calgmail_row_1 = [
        timeseries(
            "Calendar API (calls + errors by operation)",
            [
                ("sum by (operation) (rate(bgw_calendar_api_calls_total[15m])) * 60", "{{operation}}"),
                ("sum by (operation) (rate(bgw_calendar_api_errors_total[15m])) * 60", "err {{operation}}"),
            ],
            unit="none",
        ),
        timeseries(
            "Calendar API Latency p95 by operation",
            [
                (
                    "histogram_quantile(0.95, sum by (le, operation) (rate(bgw_calendar_api_duration_seconds_bucket[15m])))",
                    "{{operation}}",
                )
            ],
            unit="s",
        ),
        timeseries(
            "Gmail API (calls + errors by operation)",
            [
                ("sum by (operation) (rate(bgw_gmail_api_calls_total[15m])) * 60", "{{operation}}"),
                ("sum by (operation) (rate(bgw_gmail_api_errors_total[15m])) * 60", "err {{operation}}"),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(calgmail_row_1, y, heights=[8, 8, 8])
    panels.extend(row)

    calgmail_row_2 = [
        timeseries(
            "Calendar Poll Events Announced",
            [("rate(bgw_calendar_poll_events_announced_total[1h]) * 3600", "events/hr")],
            unit="none",
        ),
        timeseries(
            "Email → Calendar",
            [
                ("rate(bgw_email_to_calendar_emails_scanned_total[1h]) * 3600", "scanned/hr"),
                ("rate(bgw_email_to_calendar_events_created_total[1h]) * 3600", "created/hr"),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(calgmail_row_2, y, heights=[8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Vision + Web Search + TTS
    r, y = row_divider("Vision + Web Search + TTS", y)
    panels.append(r)

    vwstts_row_1 = [
        timeseries(
            "Vision Requests by Status",
            [("sum by (status) (rate(bgw_vision_requests_total[15m])) * 60", "{{status}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Vision Latency p95 + Image Size p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_vision_request_duration_seconds_bucket[15m])))",
                    "p95 latency (s)",
                ),
            ],
            unit="s",
        ),
        timeseries(
            "Web Search Rate + Latency",
            [
                ("rate(bgw_web_search_total[15m]) * 60", "searches/min"),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_web_search_duration_seconds_bucket[15m])))",
                    "p95 latency (s)",
                ),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(vwstts_row_1, y, heights=[8, 8, 8])
    panels.extend(row)

    vwstts_row_2 = [
        timeseries(
            "TTS Announcements (filtered by $speaker)",
            [
                (
                    'sum by (type, speaker) (rate(bgw_tts_announcements_total{speaker=~"$speaker"}[15m])) * 60',
                    "{{type}} → {{speaker}}",
                )
            ],
            unit="none",
            stack=True,
        ),
        timeseries(
            "TTS Latency p50/p95/p99",
            [
                ("histogram_quantile(0.50, sum by (le) (rate(bgw_tts_latency_seconds_bucket[15m])))", "p50"),
                ("histogram_quantile(0.95, sum by (le) (rate(bgw_tts_latency_seconds_bucket[15m])))", "p95"),
                ("histogram_quantile(0.99, sum by (le) (rate(bgw_tts_latency_seconds_bucket[15m])))", "p99"),
            ],
            unit="s",
        ),
        piechart(
            "TTS Errors by Type (24h)",
            "sum by (error_type) (increase(bgw_tts_errors_total[24h]))",
        ),
    ]
    row, y = grid_row(vwstts_row_2, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Reminders + Fast Path
    r, y = row_divider("Reminders + Fast Path", y)
    panels.append(r)

    reminders_row = [
        timeseries(
            "Reminders Set by Target",
            [("sum by (target) (rate(bgw_reminders_set_total[1h])) * 3600", "set {{target}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Reminders Delivered vs Pending",
            [
                ("rate(bgw_reminders_delivered_total[1h]) * 3600", "delivered/hr"),
                ("bgw_reminders_pending", "pending"),
            ],
            unit="none",
        ),
        timeseries(
            "Fast Path Usage",
            [
                ("sum by (action) (rate(bgw_fast_path_total[15m])) * 60", "fast {{action}}"),
                ("rate(bgw_fast_path_bypass_total[15m]) * 60", "bypass → LLM"),
            ],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(reminders_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Background Jobs
    r, y = row_divider("Background Jobs", y)
    panels.append(r)

    bg_desc = (
        "Nightly training-corpus drain: in-process counter only — records "
        "written by the out-of-process manual backfill are not counted here. "
        "A flat-zero series on days you used Jess via OWUI / voice means the "
        "drain broke; investigate via the logs row."
    )
    background_row = [
        stat(
            "Training Corpus Records (cumulative, in-process)",
            "sum(bgw_training_corpus_records_total)",
            unit="none",
        ),
        timeseries(
            "Training Corpus New Records (daily, by source)",
            [
                (
                    "sum by (source) (increase(bgw_training_corpus_records_total[1d]))",
                    "{{source}}",
                )
            ],
            unit="none",
            stack=True,
            fill=40,
            description=bg_desc,
        ),
        stat(
            "Training Corpus Records (by source)",
            "sum by (source) (bgw_training_corpus_records_total)",
            unit="none",
            text_mode="value_and_name",
        ),
    ]
    row, y = grid_row(background_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Logs (with $request_id trace)
    r, y = row_divider("Logs", y)
    panels.append(r)

    logs_row = [
        logs(
            "Trace Request (paste $request_id)",
            '{container="brain-orchestrator"} | json | request_id="$request_id"',
        ),
        logs(
            "Errors + Warnings",
            '{container="brain-orchestrator"} | json | level=~"error|critical|warning"',
        ),
    ]
    row, y = grid_row(logs_row, y, heights=[10, 10])
    panels.extend(row)

    assign_ids(panels)

    templating = [
        var_query("tool", "Tool", "label_values(bgw_tool_calls_total, tool)"),
        var_query("mode", "Mode", "label_values(bgw_requests_total, mode)"),
        var_query("speaker", "Speaker", "label_values(bgw_tts_announcements_total, speaker)"),
        var_query("wing", "Palace Wing", "label_values(bgw_palace_stores_total, wing)"),
        var_text("request_id", "Request ID"),
    ]

    return dashboard(
        title="Brain Gateway SRE",
        uid="brain-gateway-sre",
        description=(
            "Full application telemetry for Brain Gateway. Every metric in "
            "orchestrator/metrics.py is covered here, grouped by subsystem. "
            "Use filters at the top to drill into a specific tool, mode, "
            "speaker, or palace wing. Paste a request_id into the text "
            "variable to trace a single request end-to-end in the logs row. "
            "Generated — edit monitoring/grafana/dashgen/dashboards/brain_gateway_sre.py, not the JSON."
        ),
        tags=["brain-gateway", "sre"],
        refresh="30s",
        time_from="now-3h",
        templating=templating,
        panels=panels,
    )
