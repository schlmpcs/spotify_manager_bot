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

    # --- LLM (any OpenAI-compatible API: OpenAI, OpenRouter, or local Ollama) ---
    llm_base_url: str = Field(
        "https://api.openai.com/v1",
        description="OpenAI-compatible base URL. "
        "OpenRouter: https://openrouter.ai/api/v1 | "
        "Ollama: http://host.docker.internal:11434/v1",
    )
    llm_api_key: SecretStr = Field(
        ...,
        description="API key for the provider above. For local Ollama any "
        "non-empty placeholder works (e.g. 'ollama').",
    )
    llm_models: List[str] = Field(
        default_factory=lambda: ["gpt-4o-mini"],
        description="Ordered model chain; on 429/error the next is tried",
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
    reply_rate_per_min: int = Field(
        15,
        description="Abuse guard: max AI replies to one customer per minute; "
        "further messages in the window are recorded but not answered. Set high "
        "enough not to clip a fast human chatter — only blocks bot-speed floods",
    )
    reply_rate_per_hour: int = Field(
        150, description="Abuse guard: max AI replies to one customer per hour"
    )
    reply_debounce_seconds: float = Field(
        2.5,
        description="Wait this long for more messages before replying. People "
        "often split one thought across several quick messages — batching them "
        "lets the bot answer the whole thing at once instead of line by line.",
    )
    manager_takeover_hours: int = Field(
        24,
        description="When the manager replies to a customer by hand, stay silent "
        "in that chat for this many hours so the bot doesn't talk over them. "
        "Each new manual manager message resets the window.",
    )
    proactive_mode: str = Field(
        "auto",
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
        3,
        description="Legacy cooldown (kept for compatibility). The staged nudge "
        "sequence now advances at most one step per calendar day instead.",
    )
    bot_timezone: str = Field("Asia/Almaty", description="Timezone")

    # --- Pricing defaults used to state amounts owed + quote prices (grounding) ---
    # Mirror the main bot's settings.py. The bot may state these to anyone,
    # including prospects not yet in the payment DB.
    kz_group_price: int = Field(700, description="₸/month, KZ group slot")
    ru_group_price: int = Field(200, description="₽/month, RU group slot")
    kz_individual_price: int = Field(1500, description="₸/month, KZ individual plan")
    ru_individual_price: int = Field(250, description="₽/month, RU individual plan")
    kz_duo_price: int = Field(2500, description="₸/month, KZ duo plan")
    ru_duo_price: int = Field(600, description="₽/month, RU duo plan")

    support_username: str = Field(
        "sptfy_premium", description="Manager/support @username (no @)"
    )
    purchase_bot_username: str = Field(
        "sptfy_premium_bot",
        description="@username (no @) of the payment bot where NEW customers "
        "buy/connect a subscription. New prospects are directed here.",
    )
    channel_url: str = Field(
        "https://t.me/sptfykz",
        description="Public channel link mentioned to new customers for details.",
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
