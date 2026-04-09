"""
Centralized configuration for Brain Gateway.

All environment variables are defined here with typed defaults.
Import `settings` to access configuration anywhere:

    from config import settings
    url = settings.model_url
"""

from typing import List

from dotenv import load_dotenv
from pydantic import field_validator
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

    # -- Primary LLM -------------------------------------------------------------
    model_url: str = "http://localhost:8080/v1"
    model_name: str = ""

    # -- Fallback LLM (empty = disabled) -----------------------------------------
    fallback_model_url: str = ""
    fallback_model_name: str = ""

    # -- Embedding ---------------------------------------------------------------
    embedding_model: str = "nomic-ai/nomic-embed-text-v2-moe"

    # -- Agentic loop ------------------------------------------------------------
    max_tool_rounds: int = 5

    # -- Home Assistant (empty = disabled) ----------------------------------------
    ha_url: str = ""
    ha_token: str = ""

    # -- RAG / ChromaDB ----------------------------------------------------------
    chroma_persist: str = "/app/data/chroma"
    chroma_collection: str = "personal_rag"
    min_cos: float = 0.30
    top_k: int = 6
    rag_base: str = "/app/data/rag"

    # -- TTS (empty = disabled) --------------------------------------------------
    tts_url: str = ""
    tts_voice: str = "jessica"

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
    morning_briefing_enabled: bool = False
    morning_briefing_speaker: str = ""

    # -- Email -------------------------------------------------------------------
    email_poll_interval: int = 30
    email_poll_enabled: bool = True
    email_to_calendar_enabled: bool = True
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

    # -- Routines (F-006) --------------------------------------------------------
    routines_yaml_path: str = "/app/data/routines.yaml"
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

    # -- Code Agent --------------------------------------------------------------
    code_agent_enabled: bool = False
    code_agent_model_url: str = ""
    code_agent_model_name: str = ""
    code_agent_codebase_path: str = "/opt/helios/gateway_mvp"
    code_agent_max_rounds: int = 10

    # -- Do Not Disturb ----------------------------------------------------------
    # (runtime state, not env var — but included for completeness)

    # -- Open WebUI deep link ----------------------------------------------------
    webui_url: str = ""

    # -- CORS --------------------------------------------------------------------
    cors_origins: str = "http://localhost:3001"

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

    @field_validator("calendar_poll_interval", "email_poll_interval", "travel_time_buffer")
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        if v < 1:
            return 1
        return v

    model_config = {
        "env_file": "/app/.env",
        "extra": "ignore",
        "case_sensitive": False,
    }


# Module-level singleton — import this everywhere
try:
    settings = Settings()
except Exception as e:
    import sys

    print(f"FATAL: Failed to load configuration: {e}", file=sys.stderr)
    print("Check your .env file or run scripts/setup.sh to generate one.", file=sys.stderr)
    sys.exit(1)
