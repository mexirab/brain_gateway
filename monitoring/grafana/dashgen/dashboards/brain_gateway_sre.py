"""
brain-gateway (uid brain-gateway-overview) — full application telemetry.

The single canonical Brain Gateway app dashboard. Consolidates the former
hand-maintained "Overview" and "Deep Dive" dashboards into this generated one.
Covers every metric defined in orchestrator/metrics.py, grouped by subsystem
into collapsible rows. Infra (host CPU/mem/GPU/disk) lives in the separate
Homelab Infrastructure dashboard — intentionally none of that here. Use when
Jess Glance shows a symptom but you need to drill into which subsystem is
misbehaving.

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

    # Real streaming (SSE token relay through the unified tool loop): TTFT is the
    # metric the feature exists to improve; the outcome counter surfaces silent
    # degradation (buffered fallback, mid-emission truncation).
    streaming_flow = [
        timeseries(
            "Chat TTFT (p50 / p95 / p99)",
            [
                ("histogram_quantile(0.50, sum by (le) (rate(bgw_chat_ttft_seconds_bucket[5m])))", "p50"),
                ("histogram_quantile(0.95, sum by (le) (rate(bgw_chat_ttft_seconds_bucket[5m])))", "p95"),
                ("histogram_quantile(0.99, sum by (le) (rate(bgw_chat_ttft_seconds_bucket[5m])))", "p99"),
            ],
            unit="s",
            description="Time to first streamed token on /v1/chat/completions. If this "
            "collapses toward Request Latency, streaming has silently degraded to the "
            "buffered path — cross-check the degradation panel.",
        ),
        timeseries(
            "Stream Degradation Events (/min)",
            [("sum by (outcome) (rate(bgw_chat_stream_outcome_total[5m])) * 60", "{{outcome}}")],
            unit="none",
            stack=True,
            description="Non-happy streaming outcomes (happy path is uncounted; denominator "
            "is the TTFT count). died_mid_emission = the user got a truncated answer.",
        ),
        stat(
            "Truncated Answers (died_mid_emission, 24h)",
            'sum(increase(bgw_chat_stream_outcome_total{outcome="died_mid_emission"}[24h])) or vector(0)',
            unit="none",
            thresholds=[(None, "green"), (1, "orange")],
            description="Streams that died after emitting → user saw a silently truncated "
            "reply. Watched by the ChatStreamTruncating alert.",
        ),
        timeseries(
            "Empty Model Replies (/min)",
            [("sum by (outcome) (rate(bgw_llm_empty_reply_total[5m])) * 60", "{{outcome}}")],
            unit="none",
            description="Rounds where the brain returned no text and no tool call. retried = "
            "re-asked once with thinking on; recovered = the retry answered; still_empty = "
            "the user got a blank. A sustained retried rate (>5% of voice turns) means the "
            "voice no-think fast path no longer pays off.",
        ),
        stat(
            "Blank Replies (still_empty, 24h)",
            'sum(increase(bgw_llm_empty_reply_total{outcome="still_empty"}[24h])) or vector(0)',
            unit="none",
            thresholds=[(None, "green"), (1, "orange")],
            description="Turns where the user got no answer even after the retry. Watched by "
            "the EmptyRepliesReachingUsers alert.",
        ),
    ]
    row, y = grid_row(streaming_flow[:3], y, heights=[8, 8, 8])
    panels.extend(row)
    row, y = grid_row(streaming_flow[3:], y, heights=[8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Mode Router
    r, y = row_divider("Mode Router", y)
    panels.append(r)

    mode_router_row = [
        timeseries(
            "Mode Routing (1h)",
            [("sum by (mode) (increase(bgw_mode_route_total[1h]))", "{{mode}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Intensity (1h)",
            [("sum by (intensity) (increase(bgw_mode_route_total[1h]))", "{{intensity}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(mode_router_row, y, heights=[8, 8])
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
        "Successful TTS round-trips to the TTS server at TTS_URL on Helios (VoxCPM2 :8004 or Qwen3-TTS :8002), from the "
        "dashboard chat's /api/tts/synthesize (one call per spoken sentence) and "
        "the OpenAI-compatible /v1/audio/speech proxy. Failures are NOT in this "
        "histogram — see Dashboard TTS Outcomes. Excludes browser playback. Both "
        "paths prepend ~150ms of silence against first-word stutter (TTS_SILENCE_PAD_MS)."
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
            "Dashboard STT Outcomes",
            [("sum by (result) (rate(bgw_dashboard_stt_total[5m])) * 60", "{{result}}")],
            unit="none",
            description=(
                "Per-utterance /api/stt/transcribe results per minute (mic button + "
                "hands-free call). A high share of 'empty' means the call-mode voice "
                "detector is false-triggering on room noise; unreachable = Helios asleep."
            ),
        ),
        timeseries(
            "Dashboard TTS Outcomes",
            [("sum by (result) (rate(bgw_dashboard_tts_total[5m])) * 60", "{{result}}")],
            unit="none",
            description=(
                "Per-sentence /api/tts/synthesize results per minute. unreachable = "
                "Helios asleep (chat shows 'Jess's voice is asleep'); timeout = TTS "
                "awake but stuck; upstream_error = TTS returned an error."
            ),
        ),
        stat(
            "TTS Mean (5m)",
            "(sum(rate(bgw_voice_tts_seconds_sum[5m])) / sum(rate(bgw_voice_tts_seconds_count[5m]))) or on() vector(0)",
            unit="s",
            decimals=2,
            thresholds=VOICE_TTS_THRESHOLDS_S,
        ),
        stat(
            "TTS Sentences (24h)",
            "sum(increase(bgw_voice_tts_seconds_count[24h]))",
            unit="none",
        ),
    ]
    # 3 time series (latency, STT outcomes, TTS outcomes) over 2 stats.
    row, y = grid_row(tts_row[:3], y, heights=[8, 8, 8])
    panels.extend(row)
    row, y = grid_row(tts_row[3:], y, heights=[4, 4])
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
        # Lives in the LLM row, not Tools: the decision it informs is a vLLM
        # serving upgrade, not tool health.
        # source="none" is excluded — it is the ordinary-reply denominator and
        # dwarfs everything else, which would flatten the interesting series to
        # invisibility. increase(...[1h]) rather than a per-second rate because
        # Helios is power-tiered (~18 model rounds/week); a rate() would read as
        # a flat zero.
        timeseries(
            "Tool Call Delivery (native vs rescued vs dropped)",
            [
                (
                    'sum by (source) (increase(bgw_tool_call_source_total{source!="none"}[1h]))',
                    "{{source}}",
                ),
            ],
            unit="short",
            stack=True,
            description=(
                "How tool calls arrived from the model. 'dropped' is user-visible "
                "breakage — a <tool_call> marker was present but nothing parsed, so "
                "the agentic loop ended the turn early. It is the signal for the "
                "vLLM<0.20.0 qwen3 reasoning-parser defect (fixed upstream in PR "
                "#35687). 'xml_fallback' is a recovered call and is NOT expected to "
                "fire for that defect. Low traffic: allow weeks before reading a "
                "trend, and do not read an early zero as 'not happening'."
            ),
        ),
    ]
    row, y = grid_row(llm_row, y, heights=[8, 8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Brain Server (llama-server)
    # Scraped straight from llama-server's /metrics (Prometheus job
    # llama-primary). Helios is power-tiered, so every series has long "no
    # data" gaps and the counters reset on each wake — increase()/rate() handle
    # the resets; sum() collapses per-instance series. Idle windows are 0/0 =
    # NaN and render as gaps, which is the honest picture.
    # prompt_tokens_total counts only the tokens actually prefilled (cache hits
    # are excluded), so the hit ratio is cached / (cached + prefilled).
    r, y = row_divider("Brain Server (llama-server)", y)
    panels.append(r)

    cache_hit_1h = (
        'sum(increase(llamacpp:prompt_tokens_cached_total{job="llama-primary"}[1h])) / '
        '(sum(increase(llamacpp:prompt_tokens_cached_total{job="llama-primary"}[1h])) '
        '+ sum(increase(llamacpp:prompt_tokens_total{job="llama-primary"}[1h])))'
    )
    mtp_accept_1h = (
        'sum(increase(llamacpp:spec_decode_num_accepted_tokens_total{job="llama-primary"}[1h])) / '
        'sum(increase(llamacpp:spec_decode_num_draft_tokens_total{job="llama-primary"}[1h]))'
    )
    llama_row = [
        timeseries(
            "Prompt Cache Hit Ratio (1h)",
            [(cache_hit_1h, "hit ratio")],
            unit="percentunit",
            span_nulls=False,
            min_value=0,
            max_value=1,
            description="Share of prompt tokens served from llama-server's prompt cache instead of "
            "re-prefilled. Typical 1h windows: ~0.8 with chat, ~0.5 when background jobs dominate; a single "
            "conversation alone reaches ~0.9+. "
            "A drop means something volatile moved above DYNAMIC_CONTEXT_MARKER in prompt_builder, "
            "or the tool schemas stopped being byte-stable (e.g. HA entity list reordered). "
            "Background jobs with different prompts lower it a little. No data = Helios asleep.",
        ),
        timeseries(
            "Decode / Prefill Speed (tok/s)",
            [
                (
                    'sum(rate(llamacpp:tokens_predicted_total{job="llama-primary"}[15m])) / '
                    'sum(rate(llamacpp:tokens_predicted_seconds_total{job="llama-primary"}[15m]))',
                    "decode",
                ),
                (
                    'sum(rate(llamacpp:prompt_tokens_total{job="llama-primary"}[15m])) / '
                    'sum(rate(llamacpp:prompt_seconds_total{job="llama-primary"}[15m]))',
                    "prefill",
                ),
            ],
            unit="none",
            span_nulls=False,
            description="Tokens per second while actually generating (decode) and prefilling (prompt), "
            "averaged over 15 min of busy time — idle time is excluded. Baseline on the 5090 with MTP: "
            "~105-120 decode; prefill ~2500 on large cold prompts, much lower on small cached turns (normal). Decode well below that = MTP acceptance collapsed or the "
            "model fell back to CPU (see BrainOnCPU).",
        ),
        timeseries(
            "MTP Draft Acceptance (1h)",
            [
                (mtp_accept_1h, "accepted / drafted"),
                (
                    "sum by (position) (increase(llamacpp:spec_decode_num_accepted_tokens_per_pos_total"
                    '{job="llama-primary"}[1h])) / '
                    'scalar(sum(increase(llamacpp:spec_decode_num_drafts_total{job="llama-primary"}[1h])))',
                    "pos {{position}}",
                ),
            ],
            unit="percentunit",
            span_nulls=False,
            min_value=0,
            max_value=1,
            description="Share of MTP draft tokens the main model accepted (--spec-draft-n-max 2). "
            "'pos N' = share of draft steps that accepted at least N+1 tokens (pos 1 = both accepted; "
            "pos0 + pos1 = mean tokens accepted per step), so the overall line is not their average. The pos "
            "lines read low for ~1h after a Helios wake (first sample missed); trust the overall line. Healthy: ~0.7-0.9. A collapse means sampling drifted (temperature > 1, "
            "repeat_penalty != 1) or the weights/template changed; if output also garbles, the first lever "
            "is --spec-type none on the llama-server-primary unit.",
        ),
        timeseries(
            "Brain Queue (llama-server)",
            [
                ('max_over_time(llamacpp:requests_deferred{job="llama-primary"}[5m])', "deferred"),
                ('max_over_time(llamacpp:requests_processing{job="llama-primary"}[5m])', "processing"),
            ],
            unit="none",
            description="Requests waiting for / using the single primary slot. 'deferred' > 0 "
            "means chat or a background job (plan, scan, auto-learn) waited. No data = Helios asleep.",
        ),
    ]
    row, y = grid_row(llama_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Helios Power Management
    r, y = row_divider("Helios Power Management", y)
    panels.append(r)

    helios_row = [
        stat(
            "Helios Running",
            "bgw_helios_running",
            text_mode="value_and_name",
            color_mode="background",
            graph_mode="none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "OFF", "color": "red", "index": 0},
                        "1": {"text": "ON", "color": "green", "index": 1},
                    },
                }
            ],
            thresholds=GREEN_RED_BINARY,
        ),
        stat(
            "Helios Plug Power",
            "bgw_helios_plug_watts",
            unit="watt",
            # The gauge is NaN when HA can't read the power sensor; the stat's
            # lastNotNull reducer drops NaN to null, so match both.
            mappings=[
                {
                    "type": "special",
                    "options": {
                        "match": "null+nan",
                        "result": {"text": "unreadable", "color": "orange", "index": 0},
                    },
                }
            ],
            description="Smart-plug draw from HA. 'unreadable' = the power sensor is unavailable, "
            "so asleep vs hung can't be told apart (HeliosUnreachablePlugBlind covers it).",
        ),
        timeseries(
            "Model Server Start/Stop (1h)",
            [
                ("increase(bgw_model_server_starts_total[1h])", "starts"),
                ("increase(bgw_model_server_stops_total[1h])", "stops"),
            ],
            unit="none",
        ),
        timeseries(
            "code_agent Preflight vs Helios (1h)",
            [
                ("increase(bgw_code_agent_preflight_failures_total[1h])", "preflight skips"),
                ("bgw_helios_running", "helios running (0/1)"),
            ],
            unit="none",
            description=(
                "code_agent runs skipped because the code model endpoint was unreachable "
                "at preflight. Skips while 'helios running' is 0 are EXPECTED (Helios asleep). "
                "Skips while it is 1 mean Helios is powered but the code model endpoint is "
                "dead — the user then gets a misleading 'wake Helios' message and it needs "
                "investigation."
            ),
        ),
    ]
    row, y = grid_row(helios_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Helios Health (self-monitoring)
    # What Jess can sense about her own body: the Helios journal shipped to Loki
    # (promtail-helios -> loki-push-gw), the GPU driver, decode speed and whether
    # the shipping itself still works. Alerts: HeliosGpuDriverBroken, BrainOnCPU,
    # HeliosLogsSilent/HeliosLogCheckFailing, HeliosHardwareFault (Loki ruler).
    r, y = row_divider("Helios Health (self-monitoring)", y)
    panels.append(r)

    helios_health_stats = [
        stat(
            "GPU Driver (nvidia-smi exit)",
            'nvidia_smi_command_exit_code{job="gpu-exporter", node="helios"}',
            color_mode="background",
            graph_mode="none",
            thresholds=[(None, "green"), (1, "red")],
            mappings=[{"type": "value", "options": {"0": {"text": "OK", "color": "green", "index": 0}}}],
            description="Non-zero = the GPU exporter can't talk to the NVIDIA driver (18 = "
            "driver/library mismatch, the 2026-10-15 CPU-fallback cause) -> HeliosGpuDriverBroken. "
            "'No data' = Helios asleep.",
        ),
        stat(
            "Brain Decode Speed (15m)",
            # Same guard as BrainOnCPU: without it an awake-but-idle brain gives
            # 0/0 = NaN instead of "No data".
            '(rate(llamacpp:tokens_predicted_total{job="llama-primary"}[15m]) '
            '/ rate(llamacpp:tokens_predicted_seconds_total{job="llama-primary"}[15m])) '
            'and increase(llamacpp:tokens_predicted_total{job="llama-primary"}[15m]) >= 1',
            unit="none",
            thresholds=[(None, "red"), (30, "yellow"), (80, "green")],
            description="Mean tok/s while generating. ~105-125 on the RTX 5090; under 30 = "
            "running on CPU -> BrainOnCPU. 'No data' = idle or asleep.",
        ),
        stat(
            "Helios Logs Reaching Loki",
            # Only trust the gauge when a check actually completed recently: it
            # reads 0 from process start, while the check is disabled, and keeps
            # its last value while the check fails (HeliosLogCheckFailing).
            'bgw_helios_logs_silent and on() increase(bgw_helios_log_check_total{result=~"lines|silent"}[15m]) > 0',
            color_mode="background",
            graph_mode="none",
            thresholds=[(None, "green"), (1, "blue")],
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "Flowing", "color": "green", "index": 0},
                        "1": {"text": "Silent 30m", "color": "blue", "index": 1},
                    },
                }
            ],
            description="Flowing = the last check saw brain log lines in Loki. Silent is normal while "
            "the brain is idle; HeliosLogsSilent only fires when it is silent AND the brain served "
            "prompts (promtail-helios or loki-push-gw down). 'No data' = no check completed in 15m "
            "(self-audit disabled, just restarted, or HeliosLogCheckFailing).",
        ),
        stat(
            "Self-Audit Truncated Groups (1d)",
            "round(sum(increase(bgw_self_audit_truncated_total[1d])))",
            graph_mode="none",
            thresholds=[(None, "green"), (1, "yellow")],
            description="Self-audit log groups (orchestrator / jupiter_other / helios / ups) whose "
            "Loki sample hit its line limit in the last day; counts are still exact, but clusters "
            "only saw the sample (e.g. a UPS USB flap).",
        ),
    ]
    row, y = grid_row(helios_health_stats, y, heights=[5, 5, 5, 5])
    panels.extend(row)

    helios_health_logs = [
        logs(
            "Helios Kernel Faults",
            '{host="helios", service="kernel"} |~ "IO_PAGE_FAULT|HC died|host controller not '
            'responding|NVRM: Xid|PCIe Bus Error|Hardware Error"',
        ),
        logs(
            "Helios Errors & Warnings",
            # A UPS USB-link flap logs ~1800 warnings/h from nut-driver and would bury
            # everything else here; the self-audit and the UPS dashboard still see it.
            '{host="helios", level=~"error|critical|warning", service!~"nut-driver@.*"}',
        ),
    ]
    row, y = grid_row(helios_health_logs, y, heights=[9, 9])
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
        timeseries(
            "HA: Unknown entity_id refusals (/1h)",
            [("sum(increase(bgw_ha_unknown_entity_total[1h])) or vector(0)", "refused")],
            unit="none",
            description="home_assistant calls refused because the entity_id doesn't exist (the model guessed; "
            "closest matches were returned). Sustained non-zero = the schema listing is over its budget "
            "('+N more not listed') or entities were renamed in HA.",
        ),
        timeseries(
            "HA: Guard refusals (/1h)",
            [
                ("sum by (path) (increase(bgw_ha_call_refused_total[1h]))", "all {{path}}"),
                (
                    "sum by (path, source) (increase(bgw_ha_device_setting_refused_total[1h]))",
                    "device setting {{path}} ({{source}})",
                ),
            ],
            unit="none",
            description="Steerable HA calls refused by ha_guard. 'device setting' = config/diagnostic entity "
            "(registry), camera/motion/LED-style name (name) or a scene/group containing one (member) - "
            "chat and the fast path can't change these; the dashboard can. A burst on path=tool can mean a "
            "prompt-injection attempt.",
        ),
        stat(
            "HA: Registry entities",
            "bgw_ha_registry_entities",
            unit="none",
            description="Entities in the last good HA registry read (rooms + entity categories). 0 = never "
            "read since start: the guard's device-setting check runs on entity names only.",
        ),
    ]
    row, y = grid_row(tools_row, y, heights=[8] * len(tools_row))
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

    embedding_row = [
        stat(
            "Embedding Model Healthy (boot self-test)",
            "bgw_embedding_healthy",
            thresholds=[(None, "red"), (1, "green")],
            mappings=[{"options": {"0": {"text": "BROKEN"}, "1": {"text": "OK"}}, "type": "value"}],
            graph_mode="none",
        ),
        timeseries(
            # Runtime encode() failures; EmbeddingErrors fires at >= 3 in 1h.
            "Embedding Errors by Caller (/1h)",
            [("sum by (caller) (increase(bgw_embedding_errors_total[1h]))", "{{caller}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(embedding_row, y, heights=[6, 6])
    panels.extend(row)

    # -------------------------------------------------------- Auto-Learn
    r, y = row_divider("Auto-Learn", y)
    panels.append(r)

    autolearn_row = [
        timeseries(
            # One extraction per conversation (not per turn) since 2026-10-15.
            "Extractions (per conversation) + Facts Stored",
            [
                ("rate(bgw_auto_learn_extractions_total[15m]) * 60", "extractions/min"),
                ("sum by (category) (rate(bgw_auto_learn_facts_stored_total[15m])) * 60", "stored {{category}}"),
                ("sum by (reason) (rate(bgw_auto_learn_dropped_total[15m])) * 60", "dropped {{reason}}"),
                ("rate(bgw_auto_learn_deferred_total[15m]) * 60", "deferred (brain asleep)"),
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

    autolearn_health_row = [
        timeseries(
            # AutoLearnFailing: errors (minus empty) >= 3 in 24h with no good run.
            "Auto-Learn Runs by Outcome (/1h)",
            [("sum by (outcome) (increase(bgw_auto_learn_runs_total[1h]))", "{{outcome}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Auto-Learn Errors by Stage (/1h) + Re-arms",
            [
                ("sum by (stage) (increase(bgw_auto_learn_errors_total[1h]))", "{{stage}}"),
                ("increase(bgw_auto_learn_rearmed_total[1h])", "re-armed"),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(autolearn_health_row, y, heights=[8, 8])
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

    # -------------------------------------------------------- Task Backlog
    r, y = row_divider("Task Backlog", y)
    panels.append(r)

    backlog_desc = (
        "The durable to-do list (state_store tasks table). Distinct from Task "
        "Decomposition above (ephemeral step-tracking) and Reminders (time-based). "
        "'Open Tasks' is a live gauge, primed on startup and updated on every "
        "add/complete/drop. A steadily climbing open count with little completion "
        "is the backlog-graveyard smell the Sunday review job is meant to catch."
    )
    backlog_row = [
        stat(
            "Open Tasks",
            "bgw_tasks_open",
            unit="none",
            graph_mode="area",
            thresholds=[(None, "green"), (15, "yellow"), (30, "red")],
        ),
        timeseries(
            "Task Flow (created / completed / dropped)",
            [
                ("sum(rate(bgw_tasks_created_total[1h])) * 3600", "created/hr"),
                ("rate(bgw_tasks_completed_total[1h]) * 3600", "completed/hr"),
                ("rate(bgw_tasks_dropped_total[1h]) * 3600", "dropped/hr"),
            ],
            unit="none",
            description=backlog_desc,
        ),
        timeseries(
            "Tasks Created by Source (24h)",
            [("sum by (source) (increase(bgw_tasks_created_total[24h]))", "{{source}}")],
            unit="none",
            stack=True,
            fill=40,
        ),
    ]
    row, y = grid_row(backlog_row, y, heights=[8, 8, 8])
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
    ]
    row, y = grid_row(focus_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Calendar sources
    # calendar_sources merges personal (CalDAV/Radicale), work (iPhone Exchange
    # push) and Google. Alerts: CalendarSourceFailing, ExchangeCalendarSyncStale,
    # RadicaleDown. See docs/CALENDAR.md.
    r, y = row_divider("Calendar Sources", y)
    panels.append(r)

    calsrc_row_1 = [
        timeseries(
            "Source reads by result (per min)",
            [("sum by (source, result) (rate(bgw_calendar_source_fetch_total[15m])) * 60", "{{source}} {{result}}")],
            unit="none",
        ),
        timeseries(
            "Source success ratio (1h)",
            [
                (
                    'sum by (source) (rate(bgw_calendar_source_fetch_total{result="ok"}[1h]))'
                    " / sum by (source) (rate(bgw_calendar_source_fetch_total[1h]))",
                    "{{source}}",
                )
            ],
            unit="percentunit",
        ),
        timeseries(
            "Source read latency p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le, source) (rate(bgw_calendar_source_fetch_duration_seconds_bucket[15m])))",
                    "{{source}}",
                )
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(calsrc_row_1, y, heights=[8, 8, 8])
    panels.extend(row)

    calsrc_row_2 = [
        stat(
            "Work calendar sync age",
            "(time() - bgw_phone_calendar_last_sync_timestamp_seconds) and bgw_phone_calendar_last_sync_timestamp_seconds > 0",
            unit="s",
            thresholds=[(None, "green"), (86400, "orange"), (93600, "red")],
            description="Age of the last iPhone work-calendar (Exchange) push. No data = the work Shortcut has never synced.",
        ),
        timeseries(
            "iPhone push rejections by reason (24h)",
            [("sum by (reason) (increase(bgw_phone_calendar_sync_rejected_total[24h]))", "{{reason}}")],
            unit="none",
        ),
        timeseries(
            "Calendar writes by target/result (24h)",
            [("sum by (target, result) (increase(bgw_calendar_write_total[24h]))", "{{target}} {{result}}")],
            unit="none",
        ),
        timeseries(
            "Radicale probe",
            [
                ('probe_success{service="radicale"}', "up"),
                ('probe_duration_seconds{service="radicale"}', "probe s"),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(calsrc_row_2, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Calendar + Gmail + Email→Cal
    r, y = row_divider("Calendar + Gmail + Email→Calendar", y)
    panels.append(r)

    calgmail_row_1 = [
        timeseries(
            "Calendar tools: check/create (calls + errors)",
            [
                ("sum by (operation) (rate(bgw_calendar_api_calls_total[15m])) * 60", "{{operation}}"),
                ("sum by (operation) (rate(bgw_calendar_api_errors_total[15m])) * 60", "err {{operation}}"),
            ],
            unit="none",
        ),
        timeseries(
            "Calendar tools latency p95 (check/create)",
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
            "Document Photo Name Suggestions (1h)",
            [("sum by (result) (increase(bgw_document_name_suggest_total[1h]))", "{{result}}")],
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
    row, y = grid_row(vwstts_row_1, y, heights=[8, 8, 8, 8])
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

    # -------------------------------------------------------- Reminder Delivery — Trust
    # The trust layer: is delivery actually reaching the user, and if not,
    # which channel is lying? Failed/missed counters come from the PR #32
    # state machine; per-channel push results from ntfy (F-011), Pushover
    # (F-013), and Telegram; TTS per-speaker success from announcement
    # observability.
    r, y = row_divider("Reminder Delivery — Trust", y)
    panels.append(r)

    trust_outcomes_row = [
        timeseries(
            "Delivery Outcomes (/day)",
            [
                ("sum(increase(bgw_reminders_delivered_total[1d]))", "delivered"),
                ("sum(increase(bgw_reminders_failed_total[1d]))", "failed"),
                ("sum(increase(bgw_reminders_missed_total[1d]))", "missed"),
            ],
            unit="none",
            description="Rolling 1-day totals. Failed = every channel exhausted; missed = found >24h past due at startup.",
        ),
        stat(
            "Failed (7d)",
            "sum(increase(bgw_reminders_failed_total[7d])) or vector(0)",
            unit="none",
            thresholds=[(0, "green"), (1, "red")],
        ),
        stat(
            "Missed (7d)",
            "sum(increase(bgw_reminders_missed_total[7d])) or vector(0)",
            unit="none",
            thresholds=[(0, "green"), (1, "orange")],
        ),
        timeseries(
            "Ack Latency (trigger → Done tap)",
            [
                (
                    "histogram_quantile(0.50, sum(rate(bgw_reminder_ack_latency_seconds_bucket[6h])) by (le))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum(rate(bgw_reminder_ack_latency_seconds_bucket[6h])) by (le))",
                    "p95",
                ),
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(trust_outcomes_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    trust_channels_row = [
        timeseries(
            "Push Channels — OK (/h)",
            [
                ('sum(rate(bgw_ntfy_push_total{result="ok",kind="reminder"}[1h])) * 3600', "ntfy"),
                ('sum(rate(bgw_pushover_push_total{result="ok",kind="reminder"}[1h])) * 3600', "pushover"),
                ('sum(rate(bgw_telegram_send_total{result="ok",kind="reminder"}[1h])) * 3600', "telegram"),
                ('sum(rate(bgw_webpush_send_total{result="ok"}[1h])) * 3600', "web push"),
            ],
            unit="none",
        ),
        timeseries(
            "Push Channels — Failures (/h)",
            [
                ('sum(rate(bgw_ntfy_push_total{result="fail"}[1h])) * 3600', "ntfy fail"),
                ('sum(rate(bgw_pushover_push_total{result="fail"}[1h])) * 3600', "pushover fail"),
                ('sum(rate(bgw_telegram_send_total{result="fail"}[1h])) * 3600', "telegram fail"),
                (
                    'sum(rate(bgw_webpush_send_total{result=~"auth|http_4xx|http_5xx|timeout|redirect|error"}[1h])) * 3600',
                    "web push fail",
                ),
            ],
            unit="none",
            thresholds=[(0, "green"), (1, "red")],
        ),
        timeseries(
            "TTS by Speaker (success vs fail, /h)",
            [
                (
                    'sum by (speaker) (rate(bgw_tts_announcements_total{success="true"}[1h])) * 3600',
                    "ok {{speaker}}",
                ),
                (
                    'sum by (speaker) (rate(bgw_tts_announcements_total{success="false"}[1h])) * 3600',
                    "FAIL {{speaker}}",
                ),
            ],
            unit="none",
            description="Per-speaker announcement health — a failing speaker group shows up here before anyone notices missed audio.",
        ),
    ]
    row, y = grid_row(trust_channels_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------- Dashboard Inbox & Web Push
    # Reminders + selfcare nudges become Done/Snooze cards in /chat, pushed to
    # phones via self-hosted Web Push. "Resolved by" shows WHERE cards get
    # cleared (dashboard tap vs telegram vs logged by voice); a card count that
    # climbs with no dashboard resolutions means nobody is seeing the tray.
    # Web Push "auth" = the VAPID key changed — devices must re-enable.
    r, y = row_divider("Dashboard Inbox & Web Push", y)
    panels.append(r)
    inbox_row = [
        stat(
            "Web Push Devices",
            "max(bgw_webpush_subscriptions)",
            thresholds=[(None, "yellow"), (1, "green")],
            description="Devices subscribed via Settings → Notifications. 0 with WEBPUSH_ENABLED=false is expected.",
        ),
        timeseries(
            "Inbox Cards Added (6h)",
            [("sum by (kind) (increase(bgw_inbox_items_total[6h]))", "{{kind}}")],
            unit="none",
        ),
        timeseries(
            "Inbox Cards Resolved by (6h)",
            [("sum by (via, resolution) (increase(bgw_inbox_resolved_total[6h]))", "{{via}} / {{resolution}}")],
            unit="none",
        ),
        timeseries(
            "Web Push Sends by Result (6h)",
            [("sum by (result) (increase(bgw_webpush_send_total[6h]))", "{{result}}")],
            unit="none",
        ),
    ]
    row, y = grid_row(inbox_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Telegram Bot
    # The away-from-home surface: inbound updates, media (voice→STT / photo→vision
    # / spoken walkie-talkie reply), and Done/Snooze button taps. The callback
    # panel is the forensic view for "I tapped Done but kept getting nagged" — a
    # rising ack{result=not_found} or a flat callback rate during active reminders
    # is the signal. Media failures while Helios sleeps (stt_unreachable) are
    # expected; tts_failed/vision_failed are the actionable ones.
    r, y = row_divider("Telegram Bot", y)
    panels.append(r)

    telegram_row = [
        timeseries(
            "Inbound Updates (6h)",
            [("sum by (kind, result) (increase(bgw_telegram_update_total[6h]))", "{{kind}} / {{result}}")],
            unit="none",
        ),
        timeseries(
            "Media: voice / photo / reply (6h)",
            [("sum by (kind, result) (increase(bgw_telegram_media_total[6h]))", "{{kind}} / {{result}}")],
            unit="none",
            description=(
                "Inbound voice (STT) + photo (vision) + spoken voice_reply (TTS) outcomes. "
                "voice/result=stt_unreachable is expected when Helios is asleep; "
                "tts_failed / vision_failed / send_failed are actionable."
            ),
        ),
        timeseries(
            "Button Taps: Done / Snooze (6h)",
            [("sum by (action, result) (increase(bgw_telegram_callback_total[6h]))", "{{action}} / {{result}}")],
            unit="none",
            description=(
                "Done/Snooze/selfcare taps. ack/snooze result=not_found or a flat rate while "
                "reminders fire points at taps not landing (the 'Done didn't stick' class)."
            ),
        ),
    ]
    row, y = grid_row(telegram_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Sleep Wind-Down
    # The wind-down ladder (PR #56): T-60 lights rung + T-30 nudge rung ahead
    # of bedtime. The T-30 nudge is watched by the WindDownNudgeStale alert
    # (quiet Pushover) because a missing spoken nudge is invisible by omission;
    # the T-60 lights rung has no alert (a failed dim is self-evident in the
    # house), so its heartbeat panel here is the only place a silently-dropped
    # dim job shows up.
    r, y = row_divider("Sleep Wind-Down", y)
    panels.append(r)

    wind_down_row = [
        stat(
            "Nudge Heartbeat Age (T-30)",
            # The > 0 guard mirrors the briefing stale-alert rules: the gauge is
            # unseeded (default 0) until the job first stamps it, so this shows
            # "No data" instead of a ~56-year orange before the first real run.
            "time() - (bgw_wind_down_last_run_timestamp_seconds > 0)",
            unit="s",
            # 25.5h, not 25h: the largest legitimate gap is 25h (the DST
            # fall-back night) + 300s misfire grace; 25.5h adds ~25m of headroom.
            thresholds=[(None, "green"), (91800, "orange")],
            description="Seconds since the T-30 screens-away nudge job last fired. "
            "Fires nightly; > 25.5h (orange) means the scheduler dropped a rung. "
            "Also alerts (WindDownNudgeStale, quiet Pushover) since a missing "
            "spoken nudge is invisible in the house. NOT seeded at startup (as of "
            "2026-07-08): 'No data' means the job hasn't fired since the last "
            "restart, or wind-down is disabled — the gauge only stamps on a real run.",
        ),
        stat(
            "Dim Heartbeat Age (T-60)",
            # Stamped at the top of wind_down_dim before its early returns, so
            # this ticks even on DND / no-scene nights. Unseeded (default 0)
            # until the first real run, same > 0 guard as the nudge panel.
            "time() - (bgw_wind_down_dim_last_run_timestamp_seconds > 0)",
            unit="s",
            thresholds=[(None, "green"), (91800, "orange")],
            description="Seconds since the T-60 lights-dim job last fired. "
            "Ticks nightly regardless of whether it attempted a scene, so an "
            "orange here means the scheduler dropped ONLY the dim rung while the "
            "Scene Outcomes panel would otherwise stay silently empty. No alert "
            "watches this rung — a failed dim is self-evident in the house. "
            "NOT seeded at startup: 'No data' means the dim job hasn't fired since "
            "the last restart, or wind-down is disabled.",
        ),
        timeseries(
            "Scene Outcomes (T-60 lights, /day)",
            [
                (
                    "sum by (scene, result) (increase(bgw_wind_down_scene_result_total[1d]))",
                    "{{scene}} {{result}}",
                )
            ],
            unit="none",
            description="Rolling 1-day scene activations by outcome (ok | failed | error). "
            "The forensic answer to 'lights didn't dim last night'.",
        ),
        stat(
            "Scene Failures (7d)",
            'sum(increase(bgw_wind_down_scene_result_total{result=~"failed|error"}[7d])) or vector(0)',
            unit="none",
            thresholds=[(None, "green"), (1, "orange")],
            description="Green 0 alongside an empty Scene Outcomes panel means the "
            "T-60 rung isn't attempting scenes (WIND_DOWN_SCENE unset, or the job "
            "isn't firing) — not that scenes are healthy.",
        ),
    ]
    row, y = grid_row(wind_down_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Budget Sync
    # Actual Budget read-only sync (replaced YNAB 2026-10). The
    # BudgetSyncFailing alert (quiet Pushover) is counter-based so it also
    # fires for a sync that has never succeeded since a restart; these panels
    # are the forensic view.
    r, y = row_divider("Budget Sync (Actual)", y)
    panels.append(r)

    budget_row = [
        stat(
            "Budget Sync Age",
            "time() - (bgw_budget_sync_last_success_timestamp_seconds > 0)",
            unit="s",
            thresholds=[(None, "green"), (7200, "orange"), (21600, "red")],
            description="Seconds since the last successful Actual Budget sync (every "
            "ACTUAL_SYNC_INTERVAL, default 30 min). 'No data' means no success since "
            "the orchestrator last started, or the sync is not configured — check "
            "Budget Sync Outcomes and GET /api/finance/sync/status (last_error).",
        ),
        timeseries(
            "Budget Sync Outcomes (/h)",
            [("sum by (result) (increase(bgw_budget_sync_total[1h]))", "{{result}}")],
            unit="none",
            description="ok | error | busy per hour. 'busy' = a manual sync or the "
            "settings page overlapped a running one (harmless); sustained 'error' "
            "with no 'ok' triggers BudgetSyncFailing.",
        ),
        logs(
            "Budget Sync Logs",
            '{container="brain-orchestrator"} |= "[ACTUAL]"',
        ),
    ]
    row, y = grid_row(budget_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Pantry & Meal Plan
    # F-015. Vision stock-take scans (phase 1) and the weekly meal planner
    # (phase 2). Both put long calls on the single llama.cpp slot, so the
    # planner latency and llama's own queue are shown side by side. Volume is
    # ~weekly, so no rate alerts — this row is the forensic view.
    r, y = row_divider("Pantry & Meal Plan", y)
    panels.append(r)

    pantry_row = [
        timeseries(
            "Meal Plan Generations (/d)",
            [("sum by (result) (increase(bgw_meal_plan_generated_total[1d]))", "{{result}}")],
            unit="none",
            description="ok | retry_ok (first reply unusable, retry worked) | invalid | "
            "llm_error | asleep (Helios off) | error (bug — check logs) | interrupted.",
        ),
        timeseries(
            "Meal Plan Latency",
            [
                ("histogram_quantile(0.5, sum by (le) (rate(bgw_meal_plan_latency_seconds_bucket[1d])))", "p50 plan"),
                ("histogram_quantile(0.95, sum by (le) (rate(bgw_meal_plan_latency_seconds_bucket[1d])))", "p95 plan"),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_meal_plan_swap_latency_seconds_bucket[1d])))",
                    "p95 swap",
                ),
            ],
            unit="s",
            description="Whole-plan generation (thinking on, waits for chat to go idle first) and "
            "single-meal swaps. Long plans hold the one model slot.",
        ),
        timeseries(
            "Pantry Scans & Swaps (/d)",
            [
                ("sum by (kind, result) (increase(bgw_pantry_scan_total[1d]))", "{{kind}} {{result}}"),
                ("sum by (result) (increase(bgw_meal_plan_swap_total[1d]))", "swap {{result}}"),
            ],
            unit="none",
            description="Photo stock-takes (proposed/empty/failed/committed/discarded) and meal swaps.",
        ),
        logs(
            "Pantry & Meal Plan Logs",
            '{container="brain-orchestrator"} |~ "\\\\[(PANTRY|MEALPLAN)\\\\]"',
        ),
    ]
    pantry_row += [
        timeseries(
            "Receipt Lines (/7d)",
            [("sum by (result) (increase(bgw_pantry_receipt_lines_total[7d]))", "{{result}}")],
            unit="none",
            description="How receipt lines were matched: matched_alias (a correction Jess remembered — "
            "should grow over time), matched_inventory, new, non_food.",
        ),
        timeseries(
            "Scan Latency p95 by Kind",
            [
                (
                    "histogram_quantile(0.95, sum by (le, kind) (rate(bgw_pantry_scan_latency_seconds_bucket[1d])))",
                    "{{kind}}",
                )
            ],
            unit="s",
            description="Photo stock-takes and receipts, end to end (all photos of a scan, one at a time).",
        ),
    ]
    row, y = grid_row(pantry_row, y, heights=[8, 8, 8, 8, 8, 8])
    panels.extend(row)

    # Phase 4: consumption tracking. Weekly volume — no alerts.
    consumption_row = [
        timeseries(
            "Cooked & Portions Eaten (/7d)",
            [
                ("sum by (slot, source) (increase(bgw_pantry_cook_events_total[7d]))", "cooked {{slot}} {{source}}"),
                ("sum by (source) (increase(bgw_pantry_portions_eaten_total[7d]))", "ate {{source}}"),
            ],
            unit="none",
            description="Meals marked cooked — planned prep/dinner or an off-plan meal (slot=other); dashboard / chat / push = reminder Done — and prepped "
            "portions eaten. source=undo is the mis-tap rate.",
        ),
        timeseries(
            "Cooking Reminder Done + Fridge Check (/30d)",
            [
                ("sum by (result) (increase(bgw_pantry_reminder_done_total[30d]))", "done {{result}}"),
                ("sum by (result) (increase(bgw_pantry_fridge_check_nudge_total[30d]))", "fridge {{result}}"),
                ("sum by (kind) (increase(bgw_pantry_decay_total[30d]))", "decay {{kind}}"),
            ],
            unit="none",
            description="Done taps on cooking reminders: unlinked = reminder from before phase 4, refused = plan "
            "replaced. Weekly fridge-check nudges. Items/batches the daily expiry pass retired.",
        ),
        stat(
            "Pantry Decay Heartbeat Age",
            "time() - (bgw_pantry_decay_last_run_timestamp_seconds > 0)",
            unit="s",
            thresholds=[(None, "green"), (91800, "orange"), (176400, "red")],
            description="Seconds since the 04:15 expiry pass last completed (re-seeded from the DB at startup, "
            "so a restart doesn't hide a dead job). If it stops, the morning 'use today' line goes stale. "
            "No data = it hasn't completed since the feature was deployed.",
        ),
    ]
    row, y = grid_row(consumption_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Background Jobs
    r, y = row_divider("Background Jobs", y)
    panels.append(r)

    bg_desc = (
        "Nightly training-corpus drain: in-process counter only — records "
        "written by the out-of-process manual backfill are not counted here. "
        "A flat-zero series on days you used Jess via the dashboard / voice means the "
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

    # Scheduler reliability: APScheduler drops any job whose 300s
    # misfire_grace_time lapses during an event-loop stall (shared.py
    # job_defaults). One-shot date jobs (reminder delivery, focus break,
    # dnd_auto_unmute) have no next occurrence, so a drop is a permanently lost
    # action. The EVENT_JOB_MISSED listener (orchestrator.py startup) turns each
    # drop into an ERROR log + a bgw_scheduler_jobs_missed_total bump, collapsed
    # to a bounded job_family label. Any non-zero series here means a job was
    # silently discarded — cross-reference the Errors + Warnings logs panel.
    sched_missed_desc = (
        "Jobs APScheduler dropped after their 300s misfire grace lapsed during an "
        "event-loop stall. One-shot families (reminder, focus) are lost permanently — "
        "there is no runtime re-schedule. Any bar here is a dropped job; 0/flat is healthy."
    )
    scheduler_reliability_row = [
        timeseries(
            "Scheduler Missed Jobs (by family, /day)",
            [
                (
                    "sum by (job_family) (increase(bgw_scheduler_jobs_missed_total[1d]))",
                    "{{job_family}}",
                )
            ],
            unit="none",
            stack=True,
            fill=40,
            thresholds=[(None, "green"), (1, "red")],
            description=sched_missed_desc,
        ),
        stat(
            "Missed Jobs (7d)",
            "sum(increase(bgw_scheduler_jobs_missed_total[7d])) or vector(0)",
            unit="none",
            thresholds=[(None, "green"), (1, "red")],
            description="Total scheduler jobs dropped in the last 7 days across all "
            "families. Non-zero = the event loop stalled past the misfire grace and "
            "silently discarded work.",
        ),
        stat(
            "Missed Jobs (by family, total)",
            "sum by (job_family) (bgw_scheduler_jobs_missed_total)",
            unit="none",
            text_mode="value_and_name",
            thresholds=[(None, "green"), (1, "red")],
            description="Cumulative drops per family since orchestrator start. "
            "Which family is being lost points at where to look first.",
        ),
    ]
    row, y = grid_row(scheduler_reliability_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # Daily self-audit (F-014). Silently dead from ~June to 2026-10-02 (wrong
    # Loki label + broken probe) with nothing on any dashboard. Heartbeat is
    # persisted in app_state and re-seeded from the real last-run time (first
    # enable stamps once), so unlike the wind-down gauges it survives deploys.
    self_audit_row = [
        stat(
            "Self-Audit Heartbeat Age",
            "time() - (bgw_self_audit_last_run_timestamp_seconds > 0)",
            unit="s",
            thresholds=[(None, "green"), (91800, "orange")],
            description="Seconds since the daily self-audit last completed a run "
            "(ok, partial or failed). > 25.5h (orange) = SelfAuditStale. 'No data' = "
            "self-audit disabled (SELF_AUDIT_ENABLED / JESS_ADVANCED).",
        ),
        timeseries(
            "Self-Audit Runs (by result, /day)",
            [("sum by (result) (increase(bgw_self_audit_runs_total[1d]))", "{{result}}")],
            unit="none",
            stack=True,
            fill=40,
            description="ok = logs read + diagnosed; partial = logs read, Helios asleep so no "
            "LLM diagnosis (healthy); failed = Loki unreachable/empty (2 in 2d with no "
            "ok/partial = SelfAuditFailing). A run of partials means the audit never gets a diagnosis "
            "(Helios asleep, or the investigator failed — see SelfInvestigatorFailing).",
        ),
        timeseries(
            "Investigator & Alert Webhook (/day)",
            [
                (
                    "sum by (trigger, result) (increase(bgw_self_investigator_runs_total[1d]))",
                    "run {{trigger}} {{result}}",
                ),
                ("sum by (result) (increase(bgw_self_audit_alert_trigger_total[1d]))", "webhook {{result}}"),
                (
                    'sum by (tool, result) (increase(bgw_self_investigator_tool_calls_total{result=~"denied|capped|error|duplicate"}[1d]))',
                    "tool {{tool}} {{result}}",
                ),
            ],
            unit="none",
            description="Read-only investigator runs (daily / alert-triggered) and Alertmanager "
            "webhook deliveries, plus tool calls that were denied/capped/errored/duplicated (a growing count = the model is fighting its allow-list or looping). On since 2026-10-21; SelfInvestigatorFailing alerts on repeated daily errors.",
        ),
    ]
    row, y = grid_row(self_audit_row, y, heights=[8, 8, 8])
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
        title="Brain Gateway",
        uid="brain-gateway-overview",
        description=(
            "Full application telemetry for Brain Gateway — the single canonical "
            "app dashboard (merged from the former Overview + SRE + Deep Dive). "
            "Every metric in orchestrator/metrics.py is covered here, grouped by "
            "subsystem. Infra lives in the Homelab Infrastructure dashboard. "
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
