"""
Centralized configuration for Brain Gateway.

All environment variables are defined here with typed defaults.
Import `settings` to access configuration anywhere:

    from orchestrator.config import settings
    url = settings.model_url
"""

from typing import List

from dotenv import load_dotenv
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Load .env before Settings reads os.environ
load_dotenv("/app/.env", override=False)


class Settings(BaseSettings):
    """
    Brain Gateway configuration.

    Values come from environment variables (case-insensitive).
    Empty string means "not configured / disabled" for optional services.
    """

    # -- API / Auth --------------------------------------------------------------
    api_token: str = ""
    log_level: str = "INFO"
    tz: str = "America/Chicago"

    # The box's LAN IP, set by install.sh on Stage 2. Used by the first-chat
    # welcome message to render a clickable /settings URL — Docker network
    # NAT means the orchestrator container can't enumerate the host's LAN IP
    # from inside, so host-side detection is required. Empty if unknown.
    jess_lan_ip: str = ""

    # -- Distribution profile ----------------------------------------------------
    # JESS_ADVANCED gates owner-specific tools and integrations from the
    # shippable single-box build (e.g. code_agent, ask_expert, query_budget,
    # finance_status, check_claude_activity). Default OFF so a fresh install
    # only exposes the core ADHD toolset; flip to true in .env to unlock the
    # full advanced surface used in development.
    jess_advanced: bool = False

    # Optional feature areas — default OFF in the shippable build (productization
    # 2026-06-14). Workout generation/logging and calorie meal logging are
    # opt-in; the owner's box flips these true in .env. Core self-care logging
    # (selfcare_log: meals/meds/water/movement) is unaffected and stays on.
    # Gating spans tool schemas, tool handlers, API routers, and dashboard nav.
    workouts_enabled: bool = False
    meals_enabled: bool = False

    # -- Primary LLM -------------------------------------------------------------
    # backend selects the wire protocol: "openai_compatible" (vLLM / Ollama /
    # LM Studio / any OpenAI-shaped server — the local-first default), "anthropic",
    # or "openai". api_key is only needed for cloud backends (or a secured local
    # endpoint); leave empty for local. Both can also be set per-model in
    # user_profile.yaml, which takes precedence over these env vars.
    model_backend: str = "openai_compatible"
    model_url: str = "http://localhost:8080/v1"
    model_name: str = ""
    model_api_key: str = ""

    # -- Fallback LLM (empty = disabled) -----------------------------------------
    fallback_model_backend: str = "openai_compatible"
    fallback_model_url: str = ""
    fallback_model_name: str = ""
    fallback_model_api_key: str = ""

    # -- Embedding ---------------------------------------------------------------
    embedding_model: str = "nomic-ai/nomic-embed-text-v2-moe"

    # -- Agentic loop ------------------------------------------------------------
    max_tool_rounds: int = 5
    # Real token streaming: relay the model's tokens through SSE as they
    # generate (vLLM primary only; fallback backends stay buffered). Kill
    # switch — flip to false to restore chunk-the-finished-string behavior
    # without a code change.
    real_streaming_enabled: bool = True

    # -- Home Assistant (empty = disabled) ----------------------------------------
    ha_url: str = ""
    ha_token: str = ""

    # -- Helios wake-on-demand (PT-C) --------------------------------------------
    # Power-tier the GPU box: Helios runs only the model layer (vLLM/TTS/STT) and
    # is powered off most of the time to save electricity. Its NIC Wake-on-LAN is
    # a dead end (Aquantia atlantic driver), so remote wake is a power-cycle via a
    # TP-Link Tapo smart plug, controlled through Home Assistant (reuses HA_URL /
    # HA_TOKEN — no python-kasa, no TP-Link creds). BIOS AC Back = Last State, so
    # restoring plug power auto-boots Helios only if it was running when cut →
    # "sleep" = turn the plug OFF while running (a hard power-cut, acceptable now
    # that Helios is stateless: all DBs/ChromaDB live on Jupiter, it just reloads
    # model servers on boot). Wake is automatic from the brain-asleep chat path;
    # sleep is manual only. Default OFF so the shippable build is unaffected.
    helios_wake_enabled: bool = False
    helios_plug_entity: str = "switch.helios_monitoring_plug"
    helios_plug_power_sensor: str = "sensor.helios_monitoring_plug_current_consumption"
    # Debounce window: repeated chat attempts while Helios is booting must not
    # spam switch.turn_on. A wake within this many seconds of a prior wake no-ops.
    helios_wake_debounce_seconds: int = 300

    # -- RAG / ChromaDB ----------------------------------------------------------
    chroma_persist: str = "/app/data/chroma"
    chroma_collection: str = "personal_rag"  # legacy collection name (for migration)
    min_cos: float = 0.30
    top_k: int = 6
    rag_base: str = "/app/data/rag"

    # -- TTS (empty = disabled) --------------------------------------------------
    tts_url: str = ""
    tts_voice: str = "default"

    # -- STT (empty = disabled) --------------------------------------------------
    stt_url: str = ""

    # -- Orchestrator callback URL -----------------------------------------------
    orchestrator_url: str = "http://localhost:8888"

    # -- SSH model server management (empty = disabled) --------------------------
    model_server_ip: str = ""
    model_ssh_user: str = ""
    model_ssh_key: str = "/root/.ssh/id_ed25519"
    model_start_cmd: str = "sudo systemctl start llama-server"
    model_stop_cmd: str = "sudo systemctl stop llama-server"

    # -- Reminders / speakers (empty = skip TTS) ---------------------------------
    reminder_speaker: str = ""
    fallback_speaker: str = ""
    # Writable shadow for the per-category speaker map written by the
    # `/settings → Speakers` panel. Loader prefers this when present;
    # missing categories fall back to reminder_speaker / morning_briefing_speaker
    # / focus_audio_player as appropriate.
    announcement_routes_path: str = "/app/data/announcement_routes.yaml"

    # -- Focus -------------------------------------------------------------------
    focus_audio_player: str = ""
    endel_enabled: bool = True
    focus_audio_lofi_url: str = ""
    focus_audio_coffee_url: str = ""
    focus_blocking_enabled: bool = False

    # -- Pi-hole (empty = disabled) ----------------------------------------------
    pihole_urls: str = ""
    pihole_password: str = ""
    pihole_focus_group: str = "focus_blocklist"

    # -- SearXNG -----------------------------------------------------------------
    searxng_url: str = "http://searxng:8080"
    searxng_max_results: int = 5

    # -- Google Calendar / Gmail -------------------------------------------------
    google_credentials_path: str = "/app/credentials/google_credentials.json"
    google_token_path: str = "/app/credentials/google_token.json"
    calendar_poll_interval: int = 5
    calendar_tiered_alerts: bool = True
    calendar_alert_tiers: str = "60,30,15,5"
    morning_briefing_time: str = "07:00"
    # On by default (product decision 2026-06-14). No-ops safely until a calendar
    # is connected and a speaker/push route is set, so it's harmless on a fresh
    # install and "just works" once those are configured.
    morning_briefing_enabled: bool = True
    morning_briefing_speaker: str = ""
    # Volume floor (0.0–1.0) the morning briefing forces on its target speaker
    # before play_media. Defeats "speaker still at sleep-sound volume" — see
    # the 2026-04-30 incident where the briefing played at volume_level=0.10.
    # Set to 0 to disable the floor entirely.
    morning_briefing_min_volume: float = 0.4
    # Evening shutdown ritual: tomorrow's first event + leave-by, evening meds
    # check, park one unfinished thing (F-007). Mirror of the morning briefing,
    # same on-by-default rationale — no-ops safely until routes are configured.
    # Default 21:30 keeps it ahead of the 22:00 quiet-hours start.
    evening_briefing_time: str = "21:30"
    evening_briefing_enabled: bool = True

    # -- Sleep wind-down ladder ----------------------------------------------------
    # Two rungs ahead of the bedtime anchor: T-60 dims the house via HA scene(s)
    # (silent), T-30 speaks a screens-away nudge. The evening briefing is the
    # ladder's spoken tomorrow-preview anchor. Lights rung no-ops until
    # WIND_DOWN_SCENE is set (comma-separated scene entity ids). Short nights
    # (goodnight → morning briefing under the threshold) soften the briefing.
    wind_down_enabled: bool = True
    wind_down_bedtime: str = "22:30"
    wind_down_scene: str = ""
    wind_down_short_night_hours: float = 6.5

    # -- Email -------------------------------------------------------------------
    # Email-to-calendar autonomy is dormant by default. Implementation is
    # complete (see jobs_calendar.process_emails_for_events) and the
    # scheduler trigger is wired in orchestrator.py startup, gated on this
    # flag. Flip to true in .env when ready to turn on; no code change needed.
    email_to_calendar_enabled: bool = False
    email_to_calendar_interval: int = 60

    # -- Phone calendar sync -----------------------------------------------------
    phone_calendar_sync_enabled: bool = True

    # -- Travel time (Google Maps) -----------------------------------------------
    google_maps_api_key: str = ""
    home_address: str = ""
    travel_time_buffer: int = 10

    # -- Temperature monitoring --------------------------------------------------
    closet_temp_warning: float = 85.0
    closet_temp_critical: float = 95.0

    # -- Finance -----------------------------------------------------------------
    finance_db_path: str = "/app/data/finance.db"
    ynab_access_token: str = ""
    ynab_budget_id: str = ""
    ynab_sync_interval: int = 30
    ynab_fun_money_category: str = "Fun Money"

    # -- Auto-learn (F-007) ------------------------------------------------------
    auto_learn_enabled: bool = True
    auto_learn_delay_minutes: int = 10
    auto_learn_max_facts: int = 5
    auto_learn_dedup_threshold: float = 0.85
    auto_learn_markdown: bool = False
    auto_learn_encrypt: bool = True
    auto_learn_encryption_key: str = ""
    # Comma-separated categories auto-learn must never store — these are owned by
    # a structured YAML source of truth (medications.yaml, etc.), so a
    # conversation-extracted claim in these domains must NOT become a shadow
    # "fact" the model reads back. Exposed parsed as shared.AUTO_LEARN_BLOCKED_CATEGORIES.
    auto_learn_blocked_categories: str = "health,medication,medications,meds,schedule,routine"

    # -- Routines (F-006) --------------------------------------------------------
    # NOTE: /app/data is a named Docker volume (finance-data); config files ship
    # from the repo and get bind-mounted into /app/config/ instead.
    routines_yaml_path: str = "/app/config/routines.yaml"
    # Writable shadow that the settings page writes to. Loader prefers this
    # when present; falls back to `routines_yaml_path` (read-only ship copy).
    routines_overrides_path: str = "/app/data/routines.yaml"
    routine_enabled: bool = True
    routine_nudge_max: int = 3
    routine_auto_skip: bool = False

    # -- Progress tracking (F-005) -----------------------------------------------
    progress_enabled: bool = True
    progress_db_path: str = "/app/data/progress.db"
    daily_summary_time: str = "18:00"
    weekly_summary_day: str = "sunday"
    weekly_summary_time: str = "19:00"

    # -- Interruption recovery (F-007) -------------------------------------------
    interrupt_checkin_delay: int = 5
    context_stack_size: int = 10

    # -- Self-care nudges (F-008) ------------------------------------------------
    selfcare_enabled: bool = True
    meal_nudge_hours: int = 4
    hydration_interval: int = 90
    movement_interval: int = 90
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"

    # -- Ambient awareness (F-010) -----------------------------------------------
    ambient_enabled: bool = True
    ambient_summary_times: str = "10:00,12:00,14:00,16:00"
    ambient_led_entity: str = ""
    ambient_speaker: str = ""

    # -- Presence awareness ------------------------------------------------------
    presence_enabled: bool = False
    presence_entity: str = ""
    presence_motion_sensors: str = "{}"
    presence_poll_interval: int = 60
    presence_targeted_tts: bool = True
    presence_welcome_home: bool = True
    presence_welcome_cooldown: int = 30

    # -- Vision (empty URL = disabled) -------------------------------------------
    vision_enabled: bool = False
    vision_model_url: str = ""
    vision_model_name: str = ""
    vision_max_image_size: int = 10 * 1024 * 1024
    vision_timeout: int = 60

    # -- MemPalace ---------------------------------------------------------------
    palace_enabled: bool = True
    palace_collection: str = "mempalace"
    palace_yaml_path: str = "/app/config/palace.yaml"
    palace_wakeup_enabled: bool = True
    palace_wakeup_max_tokens: int = 170
    palace_dedup_threshold: float = 0.85
    palace_session_mine_path: str = ""

    # -- Code Agent --------------------------------------------------------------
    code_agent_enabled: bool = False
    code_agent_model_url: str = ""
    code_agent_model_name: str = ""
    code_agent_codebase_path: str = "/opt/gateway_mvp"
    code_agent_max_rounds: int = 10

    # -- Expert Model (Qwen3-32B Thinking on Saturn 3090) ------------------------
    # Hard-reasoning delegate. Primary model calls this via the `ask_expert`
    # tool for multi-step math, complex planning, deep analysis. Stateless,
    # blocking, text-only. NOT in voice path (latency incompatible).
    # Per Phase A bench: p50 ~40s, p95 ~120s, p99/timeout 180s on a 3090.
    # Phase A found llama.cpp separates `message.content` from
    # `message.reasoning_content` automatically with `--jinja`, so we do NOT
    # strip <think> tags in the handler — we just consume `content`.
    # There is intentionally no THINKING_BUDGET_TOKENS: it's a non-lever in
    # llama-server for Qwen3 (scout misreported). Setting max_tokens below
    # the reasoning length truncates mid-thought and yields empty content.
    expert_enabled: bool = False
    expert_model_url: str = ""  # e.g. http://expert.example.tld:8084/v1
    expert_model_name: str = "default"
    expert_timeout_seconds: int = 180
    expert_max_tokens: int = 8000
    expert_circuit_breaker_failures: int = 3
    expert_circuit_breaker_cooldown_seconds: int = 120

    # -- Do Not Disturb ----------------------------------------------------------
    # (runtime state, not env var — but included for completeness)

    # -- Open WebUI deep link ----------------------------------------------------
    webui_url: str = ""

    # -- CORS --------------------------------------------------------------------
    cors_origins: str = "http://localhost:3001"

    # -- Pushover bridge (F-013) ----------------------------------------------
    # Parallel iOS push channel. Pushover's native APNs integration is more
    # reliable than ntfy-upstream on self-hosted for lockscreen banners.
    # Runs alongside ntfy when both are enabled; toggle ntfy off to go
    # pushover-only. Callbacks reuse F-011's HMAC-signed routes.
    pushover_enabled: bool = False
    pushover_user_key: str = ""  # 30-char alphanum from pushover.net home page
    pushover_app_token: str = ""  # 30-char alphanum from an application you create
    pushover_default_priority: int = 0  # -2..2
    pushover_api_url: str = "https://api.pushover.net/1/messages.json"
    pushover_upload_timeout_seconds: int = 10

    # -- Telegram bot ------------------------------------------------------------
    # Two-way capture + reminder channel: a long-polling background task
    # relays inbound text through /v1/chat/completions (full Jess, tools
    # included) and delivers reminders with inline Done/Snooze buttons.
    # Long-polling = outbound HTTPS only; no webhook, no public ingress.
    # Setup: create a bot with @BotFather, set the token + enable, message
    # the bot once, copy your chat ID from the orchestrator log into
    # TELEGRAM_ALLOWED_CHAT_ID, restart. Updates from any other chat are
    # dropped (ID logged, content never).
    telegram_enabled: bool = False
    telegram_bot_token: str = ""  # from @BotFather
    telegram_allowed_chat_id: str = ""  # your chat ID; comma-separated for several
    telegram_api_base: str = "https://api.telegram.org"
    telegram_self_url: str = "http://127.0.0.1:8888"  # orchestrator base for the chat relay
    telegram_poll_timeout_seconds: int = 50  # getUpdates long-poll window
    telegram_snooze_minutes: int = 10  # snooze button duration
    telegram_history_turns: int = 16  # rolling RAM-only context window (messages)
    # Which F-008 selfcare nudge kinds mirror to Telegram with a one-tap
    # ✓ Done log button. Comma list of medication|meal|water|movement, or
    # "all". Default meds-only: that's the high-stakes nudge; hourly
    # movement/hydration pings would be phone spam.
    telegram_selfcare_nudges: str = "medication"

    # -- Paperless bridge (F-012) ----------------------------------------------
    # Hands files off to Paperless-ngx for OCR + auto-tagging. Doesn't
    # mirror state — Paperless owns its files and metadata. document_vault
    # is untouched (it stays the home for typed/pasted text notes).
    paperless_enabled: bool = False
    paperless_url: str = ""  # e.g. http://paperless.example.tld:8777
    paperless_api_token: str = ""
    paperless_inbox_path: str = "/app/data/paperless_inbox"
    paperless_default_tags: str = ""  # comma-separated, optional
    paperless_upload_timeout_seconds: int = 30

    # -- ntfy feedback loop (F-011) ---------------------------------------------
    # Third delivery channel for reminders (alongside TTS + HA Companion push).
    # Includes HMAC-signed Done/Snooze action buttons that POST back to the
    # orchestrator, closing the ack loop and firing the selfcare bridge.
    ntfy_enabled: bool = False
    ntfy_url: str = ""  # e.g. http://ntfy.example.tld:8889
    ntfy_topic: str = "jess-reminders"
    ntfy_default_priority: int = 3  # 1..5
    ntfy_callback_base_url: str = ""  # e.g. https://jess.example.tld
    ntfy_hmac_secret: str = ""  # required when ntfy_enabled; signs callback URLs
    ntfy_ack_exp_seconds: int = 1800  # signature validity window
    ntfy_max_snooze_count: int = 5  # guardrail against indefinite snooze loops
    # After a successful Done/Snooze button tap, push a low-priority
    # confirmation ntfy message so the user sees visible feedback
    # ("✓ Logged" / "💤 Snoozed until 3:15 PM"). iOS `UNNotificationAction`
    # can't mutate buttons post-tap, so this side-channel is the closest
    # substitute for "button turns into a checkmark."
    ntfy_confirm_enabled: bool = False

    # -- Training corpus drain ---------------------------------------------------
    # Nightly job that appends user/assistant turns from OWUI, state_store, and
    # Claude Code sessions to append-only monthly JSONL files. See
    # orchestrator/jobs_training_corpus.py.
    training_corpus_dir: str = "/app/data/training_corpus"
    training_corpus_owui_db: str = "/app/owui_data/webui.db"
    training_corpus_state_db: str = "/app/data/brain_state.db"
    training_corpus_cc_dir: str = "/root/.claude/projects/-opt-gateway-mvp"

    # -- Self-audit (F-014) ------------------------------------------------------
    # Daily 7am job that queries Loki for the last 24h of error/warn logs across
    # all Helios services, asks Jess to diagnose each cluster, and pushes a
    # one-line digest via Pushover. Read-only by design — Jess emits text, the
    # orchestrator never executes her suggestions. Full report saved under
    # SELF_AUDIT_OUTPUT_DIR for the user to review and discuss with Claude Code.
    self_audit_enabled: bool = False
    self_audit_hour_utc: int = 7
    self_audit_lookback_hours: int = 24
    self_audit_loki_url: str = ""  # required when self_audit_enabled; e.g. http://loki.example.tld:3100
    self_audit_prom_url: str = ""  # optional, used by weekly review job; e.g. http://prom.example.tld:9090
    self_audit_max_clusters: int = 30
    self_audit_output_dir: str = "/app/data/self_audits"
    self_audit_llm_timeout_sec: int = 120

    # ---- Computed properties ---------------------------------------------------

    @property
    def alert_tiers(self) -> List[int]:
        """Parse comma-separated alert tiers into a list of ints."""
        try:
            return [int(x) for x in self.calendar_alert_tiers.split(",")]
        except ValueError:
            return [60, 30, 15, 5]

    @property
    def pihole_url_list(self) -> List[str]:
        """Parse comma-separated Pi-hole URLs into a list."""
        if not self.pihole_urls:
            return []
        return [u.strip() for u in self.pihole_urls.split(",") if u.strip()]

    @field_validator("calendar_poll_interval", "travel_time_buffer")
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        if v < 1:
            return 1
        return v

    @field_validator("ntfy_default_priority")
    @classmethod
    def validate_ntfy_priority(cls, v: int) -> int:
        if v < 1:
            return 1
        if v > 5:
            return 5
        return v

    @model_validator(mode="after")
    def validate_pushover_config(self) -> "Settings":
        """Auto-disable F-013 Pushover bridge on missing creds. Log + disable,
        never raise — optional feature must not block startup.
        """
        if self.pushover_enabled and (len(self.pushover_user_key) < 8 or len(self.pushover_app_token) < 8):
            import logging

            logging.getLogger(__name__).error(
                "[CONFIG] PUSHOVER_ENABLED=true but PUSHOVER_USER_KEY or "
                "PUSHOVER_APP_TOKEN is missing/short; disabling Pushover. "
                "Set both (30-char alphanum each) in .env to re-enable."
            )
            object.__setattr__(self, "pushover_enabled", False)
        if self.pushover_default_priority < -2:
            object.__setattr__(self, "pushover_default_priority", -2)
        elif self.pushover_default_priority > 2:
            object.__setattr__(self, "pushover_default_priority", 2)
        return self

    @model_validator(mode="after")
    def validate_telegram_config(self) -> "Settings":
        """Auto-disable the Telegram bot on a missing token. Log + disable,
        never raise — optional feature must not block startup.

        An empty TELEGRAM_ALLOWED_CHAT_ID is deliberately allowed: the setup
        flow is to start the bot without it, message the bot once, and copy
        your chat ID out of the orchestrator log. Until it's set, every
        update is dropped and reminder delivery skips the channel.
        """
        if self.telegram_enabled and len(self.telegram_bot_token) < 20:
            import logging

            logging.getLogger(__name__).error(
                "[CONFIG] TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN is "
                "missing/short; disabling Telegram. Get a token from "
                "@BotFather and set it in .env to re-enable."
            )
            object.__setattr__(self, "telegram_enabled", False)
        if self.telegram_snooze_minutes < 1:
            object.__setattr__(self, "telegram_snooze_minutes", 1)
        elif self.telegram_snooze_minutes > 120:
            object.__setattr__(self, "telegram_snooze_minutes", 120)
        return self

    @model_validator(mode="after")
    def validate_helios_wake_config(self) -> "Settings":
        """Auto-disable Helios wake-on-demand if HA isn't configured.

        Wake/sleep go entirely through Home Assistant (switch.turn_on /
        switch.turn_off). Without HA_URL + HA_TOKEN there's no control path, so
        flip the feature off and log — never raise, matches the F-011/F-013
        optional-feature pattern. Also clamp the debounce floor so a misconfigured
        0/negative value can't disable debouncing entirely.
        """
        if self.helios_wake_enabled and (not self.ha_url or not self.ha_token):
            import logging

            logging.getLogger(__name__).error(
                "[CONFIG] HELIOS_WAKE_ENABLED=true but HA_URL or HA_TOKEN is "
                "missing; disabling Helios wake-on-demand. Wake/sleep are routed "
                "through Home Assistant — set both to re-enable."
            )
            object.__setattr__(self, "helios_wake_enabled", False)
        # Entity ids are interpolated into HA REST URLs (GET /api/states/{id}),
        # so validate their format defensively even though they come from
        # operator config — mirrors ha_integration.call_service. A malformed id
        # disables the feature rather than emitting a broken request at runtime.
        if self.helios_wake_enabled:
            import logging
            import re

            _ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
            for field_name in ("helios_plug_entity", "helios_plug_power_sensor"):
                if not _ENTITY_RE.match(getattr(self, field_name)):
                    logging.getLogger(__name__).error(
                        "[CONFIG] HELIOS_WAKE_ENABLED=true but %s=%r is not a valid "
                        "HA entity id (domain.object); disabling Helios wake-on-demand.",
                        field_name.upper(),
                        getattr(self, field_name),
                    )
                    object.__setattr__(self, "helios_wake_enabled", False)
                    break
        if self.helios_wake_debounce_seconds < 0:
            object.__setattr__(self, "helios_wake_debounce_seconds", 0)
        return self

    @model_validator(mode="after")
    def validate_self_audit_config(self) -> "Settings":
        """Auto-disable F-014 self-audit on bad config. Log + disable,
        never raise — optional feature must not block startup.
        """
        if not self.self_audit_enabled:
            return self
        import logging

        log = logging.getLogger(__name__)
        url = self.self_audit_loki_url or ""
        if not url.startswith(("http://", "https://")):
            log.error(
                "[CONFIG] SELF_AUDIT_ENABLED=true but SELF_AUDIT_LOKI_URL is "
                "missing or not http(s); disabling self-audit."
            )
            object.__setattr__(self, "self_audit_enabled", False)
            return self
        if not 0 <= self.self_audit_hour_utc <= 23:
            log.warning(f"[CONFIG] SELF_AUDIT_HOUR_UTC={self.self_audit_hour_utc} out of range; clamping to 7.")
            object.__setattr__(self, "self_audit_hour_utc", 7)
        if self.self_audit_lookback_hours < 1:
            object.__setattr__(self, "self_audit_lookback_hours", 1)
        elif self.self_audit_lookback_hours > 168:
            object.__setattr__(self, "self_audit_lookback_hours", 168)
        if self.self_audit_max_clusters < 1:
            object.__setattr__(self, "self_audit_max_clusters", 1)
        elif self.self_audit_max_clusters > 200:
            object.__setattr__(self, "self_audit_max_clusters", 200)
        return self

    @model_validator(mode="after")
    def validate_paperless_config(self) -> "Settings":
        """Auto-disable F-012 paperless bridge if config is incomplete.

        Loud error log, not a ValueError: missing optional-feature config
        must not take down the whole orchestrator.
        """
        if self.paperless_enabled and (not self.paperless_url or len(self.paperless_api_token) < 8):
            import logging

            logging.getLogger(__name__).error(
                "[CONFIG] PAPERLESS_ENABLED=true but PAPERLESS_URL or "
                "PAPERLESS_API_TOKEN is missing; disabling Paperless bridge. "
                "Set both in .env to re-enable."
            )
            object.__setattr__(self, "paperless_enabled", False)
        return self

    @model_validator(mode="after")
    def validate_ntfy_config(self) -> "Settings":
        """Validate F-011 ntfy settings consistency.

        Uses `model_validator(mode="after")` so we see the fully-populated
        model and don't depend on field-declaration order (the previous
        `field_validator` on ntfy_hmac_secret silently broke if ntfy_enabled
        was moved above/below in the class body).

        If the feature is enabled without a strong secret, we **auto-disable
        it and log a loud error** rather than raising ValueError. A missing
        optional-feature secret should not take down the whole orchestrator
        (chat, HA, scheduler) at startup.
        """
        if self.ntfy_enabled and len(self.ntfy_hmac_secret) < 32:
            import logging

            logging.getLogger(__name__).error(
                "[CONFIG] NTFY_ENABLED=true but NTFY_HMAC_SECRET is missing or <32 chars; "
                "disabling ntfy. Set a 32+ char secret in .env to re-enable."
            )
            # Pydantic v2: assigning in model_validator(mode=after) is fine
            # because the model is already constructed.
            object.__setattr__(self, "ntfy_enabled", False)
        return self

    model_config = {
        "env_file": "/app/.env",
        "extra": "ignore",
        "case_sensitive": False,
    }


# Apply first-boot setup-wizard overrides to os.environ BEFORE Pydantic Settings
# reads it. Pydantic Settings: process env beats env_file, so writing the
# overrides directly into os.environ here is what lets the wizard's values win
# over the compose-injected env block (e.g. HA_TOKEN=${HA_TOKEN}). Missing file
# is a no-op. Kept out of the main config module to keep cycles in the import
# graph local.
try:
    from orchestrator import setup_env as _setup_env

    _applied = _setup_env.apply_to_environ()
    if _applied:
        import logging as _logging

        _logging.getLogger(__name__).info(
            "[CONFIG] applied %d setup_overrides.env keys to os.environ: %s",
            len(_applied),
            sorted(_applied),
        )
except Exception as _e:
    import logging as _logging

    _logging.getLogger(__name__).warning("[CONFIG] could not apply setup_overrides.env (continuing): %s", _e)

# Module-level singleton — import this everywhere
try:
    settings = Settings()
except Exception as e:
    import sys

    print(f"FATAL: Failed to load configuration: {e}", file=sys.stderr)
    print("Check your .env file or run scripts/setup.sh to generate one.", file=sys.stderr)
    sys.exit(1)
