"""Pixy configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_free_api_key: str = Field(..., alias="GEMINI_FREE_API_KEY")
    # Vertex is optional — free tier works alone until you add a service account.
    google_application_credentials: Path | None = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    vertex_project_id: str = Field(default="", alias="VERTEX_PROJECT_ID")
    vertex_location: str = Field(default="us-central1", alias="VERTEX_LOCATION")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_MODEL")

    free_tier_rpm: int = Field(default=15, alias="FREE_TIER_RPM")
    free_tier_rpd: int = Field(default=1000, alias="FREE_TIER_RPD")

    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    # Public HTTPS URL Telegram POSTs to (e.g. Tailscale Funnel / reverse proxy).
    telegram_webhook_url: str = Field(..., alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET")

    history_persist_every: int = Field(default=20, alias="HISTORY_PERSIST_EVERY")
    history_max_messages: int = Field(default=40, alias="HISTORY_MAX_MESSAGES")

    # Bind 0.0.0.0 when the webhook is reached via Funnel/proxy on this host.
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8080, alias="API_PORT")

    tz: str = Field(default="Asia/Singapore", alias="TZ")

    core_md_path: Path = Field(default=Path("core.md"), alias="CORE_MD_PATH")
    memory_dir: Path = Field(default=Path(".memory"), alias="MEMORY_DIR")
    skills_dir: Path = Field(default=Path("skills"), alias="SKILLS_DIR")
    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")

    # Aspire bank API (optional — aspire_bank skill no-ops cleanly if unset)
    aspire_client_id: str = Field(default="", alias="ASPIRE_CLIENT_ID")
    aspire_client_secret: str = Field(default="", alias="ASPIRE_CLIENT_SECRET")
    aspire_base_url: str = Field(
        default="https://api.aspireapp.com/public/v1",
        alias="ASPIRE_BASE_URL",
    )

    @field_validator(
        "google_application_credentials",
        "core_md_path",
        "memory_dir",
        "skills_dir",
        "data_dir",
        mode="before",
    )
    @classmethod
    def _resolve_path(cls, value: str | Path | None) -> Path | None:
        if value is None or value == "":
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path

    @property
    def vertex_enabled(self) -> bool:
        creds = self.google_application_credentials
        return bool(
            self.vertex_project_id
            and creds is not None
            and creds.is_file()
        )

    @property
    def logs_db_path(self) -> Path:
        return self.data_dir / "logs.db"

    @property
    def jobs_db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "conversations").mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
