"""Configuration for the Spotify Manager Automatization bot.

This is a *separate* bot from the main payment bot. It connects to the
manager's personal Telegram account via Telegram Business / Chat Automation
and answers / nudges customers on the manager's behalf, using free LLMs via
OpenRouter and grounding every message in the main bot's PostgreSQL database.
"""

from typing import List, Optional
from urllib.parse import quote

from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram (this automation bot — a NEW bot, not the payment bot) ---
    bot_token: SecretStr = Field(..., description="Token of the automation bot")
    owner_ids: List[int] = Field(
        ...,
        description="Telegram user IDs allowed to configure this bot "
        "(your manager account(s))",
    )

    # --- LLM (OpenRouter, free-model fallback chain) ---
    openrouter_api_key: SecretStr = Field(
        ..., description="OpenRouter API key (https://openrouter.ai/keys)"
    )
    openrouter_base_url: str = Field(
        "https://openrouter.ai/api/v1", description="OpenRouter API base URL"
    )
    llm_models: List[str] = Field(
        default_factory=lambda: [
            "deepseek/deepseek-chat-v3.1:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "mistralai/mistral-small-3.2-24b-instruct:free",
        ],
        description="Ordered free-model chain; on 429/error the next is tried",
    )
    llm_timeout: int = Field(60, description="LLM request timeout (s)")
    llm_max_tokens: int = Field(280, description="Max tokens per reply")

    # --- Main payment bot database (read-only grounding source) ---
    db_host: str = Field(..., description="Postgres host of the payment bot")
    db_port: Optional[int] = Field(5432, description="Postgres port")
    db_username: SecretStr = Field(..., description="Postgres user")
    db_password: SecretStr = Field(..., description="Postgres password")
    db_database: str = Field(..., description="Postgres database name")
    db_ssl_mode: str = Field("require", description="SSL mode")

    # --- This bot's own small state store (SQLite) ---
    local_db_path: str = Field(
        "./data/manager.db",
        description="SQLite file for connections, history, drafts",
    )

    # --- Behaviour ---
    auto_reply: bool = Field(
        True,
        description="Auto-send AI replies to incoming customer messages "
        "(sensitive topics are always escalated regardless)",
    )
    proactive_mode: str = Field(
        "approve",
        description="Proactive outreach mode: 'auto' (send as manager), "
        "'approve' (manager taps Send), or 'off'",
    )
    proactive_hour: int = Field(
        12, description="Hour of day (local tz) to run the outreach job"
    )
    proactive_overdue_days: int = Field(
        1, description="Start nudging customers this many days overdue"
    )
    proactive_cooldown_days: int = Field(
        3, description="Don't nudge the same customer again within N days"
    )
    bot_timezone: str = Field("Asia/Almaty", description="Timezone")

    # --- Pricing defaults used to state amounts owed (grounding) ---
    kz_group_price: int = Field(700, description="₸/month, KZ group slot")
    ru_group_price: int = Field(200, description="₽/month, RU group slot")

    support_username: str = Field(
        "sptfy_premium", description="Manager/support @username (no @)"
    )

    @property
    def database_dsn(self) -> str:
        # asyncpg's `dsn=` expects a URI (not a libpq keyword string).
        # URL-encode credentials so special chars in the password are safe.
        user = quote(self.db_username.get_secret_value(), safe="")
        password = quote(self.db_password.get_secret_value(), safe="")
        port = self.db_port or 5432
        dsn = f"postgresql://{user}:{password}@{self.db_host}:{port}/{self.db_database}"
        if self.db_ssl_mode:
            dsn += f"?sslmode={self.db_ssl_mode}"
        return dsn


settings = Settings()
