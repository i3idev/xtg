"""Configuration handling.

Settings are read from (in order of precedence):

1. real environment variables prefixed with ``XTG_``
2. the ``.env`` file located in the project home
3. the defaults declared below

Nothing in this module performs I/O beyond creating the data directories, and
no secret (``api_hash``) is ever rendered directly - use
:meth:`Settings.redacted` for display.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from xtg.core.errors import ConfigError

ENV_PREFIX = "XTG_"
PACKAGE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROXY_FILE = PACKAGE_DIR / "proxy" / "list_proxy.json"


def find_project_root(start: Path | None = None) -> Path:
    """Return the directory that holds ``pyproject.toml``.

    The search walks upwards from this file so that ``xtg`` works no matter
    where the interpreter is started from. ``XTG_HOME`` always wins.
    """
    override = _home_from_env()
    if override is not None:
        return override

    for candidate in (start or PACKAGE_DIR).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return PACKAGE_DIR.parent


def _home_from_env() -> Path | None:
    import os

    raw = os.environ.get(f"{ENV_PREFIX}HOME")
    return Path(raw).expanduser().resolve() if raw else None


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )

    # --- Telegram ----------------------------------------------------------
    api_id: int | None = Field(default=None, description="Telegram API id.")
    api_hash: str | None = Field(default=None, repr=False, description="Telegram API hash.")
    session_name: str = Field(default="xtg", description="Telethon session name.")

    # --- Reddit ------------------------------------------------------------
    # Both spellings are accepted: the bare REDDIT_* names used by PRAW's own
    # documentation and the XTG_-prefixed ones that match every other setting.
    # Secrets are never given a default value and never rendered in clear text.
    reddit_client_id: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("REDDIT_CLIENT_ID", "XTG_REDDIT_CLIENT_ID"),
        description="Reddit application client id.",
    )
    reddit_client_secret: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("REDDIT_CLIENT_SECRET", "XTG_REDDIT_CLIENT_SECRET"),
        description="Reddit application client secret.",
    )
    reddit_user_agent: str | None = Field(
        default=None,
        validation_alias=AliasChoices("REDDIT_USER_AGENT", "XTG_REDDIT_USER_AGENT"),
        description="Descriptive user agent Reddit requires for API clients.",
    )
    reddit_username: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("REDDIT_USERNAME", "XTG_REDDIT_USERNAME"),
        description="Optional Reddit account used for authenticated requests.",
    )
    reddit_password: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("REDDIT_PASSWORD", "XTG_REDDIT_PASSWORD"),
        description="Optional Reddit account password; only used with the username.",
    )
    reddit_listing: str = Field(
        default="new",
        validation_alias=AliasChoices("REDDIT_LISTING", "XTG_REDDIT_LISTING"),
        description="Default subreddit listing: new, hot, top or controversial.",
    )

    # --- Storage -----------------------------------------------------------
    home: Path = Field(default_factory=find_project_root, description="Project root.")
    database_path: Path = Field(
        default=Path("data/database.db"), description="SQLite file, relative to home."
    )
    session_dir: Path = Field(
        default=Path("data/sessions"), description="Telethon session directory."
    )
    export_dir: Path = Field(default=Path("data/exports"), description="Default export folder.")
    log_dir: Path = Field(default=Path("data/logs"), description="Rotating log directory.")

    # --- Proxy -------------------------------------------------------------
    proxy_enabled: bool = Field(default=True, description="Route connections through proxies.")
    proxy_file: Path | None = Field(
        default=None, description="Proxy list file; defaults to the bundled list."
    )

    # --- Logging -----------------------------------------------------------
    log_level: str = Field(default="INFO", description="Default loguru level.")
    log_to_file: bool = Field(default=True, description="Mirror logs into a rotating file.")

    # --- Collection tuning -------------------------------------------------
    request_delay: float = Field(default=0.5, ge=0.0, description="Delay between API calls.")
    max_retries: int = Field(default=3, ge=1, description="Retries for transient failures.")
    connect_timeout: float = Field(default=10.0, gt=0.0, description="Socket timeout.")

    # --- Performance -------------------------------------------------------
    #: Rows per database transaction. Larger batches mean fewer commits; the
    #: defaults trade a little memory for a lot less write overhead.
    batch_size: int = Field(
        default=500, ge=1, le=100_000, description="Rows written per transaction."
    )
    reddit_batch_size: int = Field(
        default=200, ge=1, le=100_000, description="Reddit rows written per transaction."
    )
    #: Rows buffered between the API reader and the database writer. This is the
    #: bound on in-flight work, so it also bounds memory.
    queue_size: int = Field(
        default=1000, ge=1, description="Rows buffered between reader and writer."
    )
    #: How many items may be in flight at once. The pool adapts inside
    #: ``min_concurrency``..``max_concurrency`` based on success rate.
    concurrency: int = Field(
        default=4, ge=1, description="Concurrent workers for the bounded pool."
    )
    min_concurrency: int = Field(
        default=1, ge=1, description="Lower bound the adaptive pool shrinks to."
    )
    max_concurrency: int = Field(
        default=8, ge=1, description="Upper bound the adaptive pool grows to."
    )
    #: Continue after the stored cursor instead of re-walking a whole history.
    resume: bool = Field(default=False, description="Resume from the stored collection cursor.")
    retry_base_delay: float = Field(
        default=0.5, gt=0.0, description="First backoff delay, doubled per attempt."
    )
    retry_max_delay: float = Field(
        default=30.0, gt=0.0, description="Cap for a computed backoff delay."
    )

    @field_validator("max_concurrency", mode="after")
    @classmethod
    def _at_least_min_concurrency(cls, value: int, info: Any) -> int:
        minimum = info.data.get("min_concurrency", 1)
        return max(value, minimum)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "api_id",
        "api_hash",
        "proxy_file",
        "reddit_client_id",
        "reddit_client_secret",
        "reddit_user_agent",
        "reddit_username",
        "reddit_password",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """``XTG_API_ID=`` in .env means "not configured", not "invalid int"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("reddit_listing", mode="before")
    @classmethod
    def _lower_reddit_listing(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    # --- Derived paths -----------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def database_file(self) -> Path:
        return self._resolve(self.database_path)

    @property
    def sessions_path(self) -> Path:
        return self._resolve(self.session_dir)

    @property
    def exports_path(self) -> Path:
        return self._resolve(self.export_dir)

    @property
    def logs_path(self) -> Path:
        return self._resolve(self.log_dir)

    @property
    def proxy_path(self) -> Path:
        return self._resolve(self.proxy_file) if self.proxy_file else DEFAULT_PROXY_FILE

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.database_file}"

    @property
    def session_file(self) -> Path:
        return self.sessions_path / self.session_name

    # --- Helpers -----------------------------------------------------------
    def _resolve(self, path: Path) -> Path:
        path = path.expanduser()
        return path if path.is_absolute() else (self.home / path)

    def ensure_directories(self) -> None:
        """Create every runtime directory. Safe to call repeatedly."""
        for directory in (
            self.data_dir,
            self.sessions_path,
            self.exports_path,
            self.logs_path,
            self.database_file.parent,
            self.proxy_path.parent if self.proxy_file else None,
        ):
            if directory is None:
                continue
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(
                    f"Cannot create directory {directory}: {exc}",
                    hint="Check the permissions of the project directory or set XTG_HOME.",
                ) from exc

    @property
    def has_api_credentials(self) -> bool:
        return self.api_id is not None and bool(self.api_hash)

    @property
    def has_reddit_credentials(self) -> bool:
        """True when PRAW can build a read-only client."""
        return bool(self.reddit_client_id and self.reddit_client_secret and self.reddit_user_agent)

    @property
    def has_reddit_account(self) -> bool:
        """True when both optional account fields are present."""
        return bool(self.reddit_username and self.reddit_password)

    @property
    def reddit_enabled(self) -> bool:
        return self.has_reddit_credentials

    def require_api_credentials(self) -> None:
        """Fail loudly when Telegram credentials are missing."""
        missing = [
            name
            for name, value in (("XTG_API_ID", self.api_id), ("XTG_API_HASH", self.api_hash))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing Telegram API credentials: {', '.join(missing)}",
                hint=(
                    "Create them at https://my.telegram.org and add them to "
                    f"{self.env_file} (see .env.example)."
                ),
            )

    def require_reddit_credentials(self) -> None:
        """Fail loudly when Reddit credentials are missing."""
        missing = [
            name
            for name, value in (
                ("REDDIT_CLIENT_ID", self.reddit_client_id),
                ("REDDIT_CLIENT_SECRET", self.reddit_client_secret),
                ("REDDIT_USER_AGENT", self.reddit_user_agent),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing Reddit credentials: {', '.join(missing)}",
                hint=(
                    "Create a script app at https://www.reddit.com/prefs/apps and add the "
                    f"values to {self.env_file} (see .env.example)."
                ),
            )
        if bool(self.reddit_username) != bool(self.reddit_password):
            raise ConfigError(
                "REDDIT_USERNAME and REDDIT_PASSWORD must be set together",
                hint="Both are optional; either fill in both or neither.",
            )

    def reddit_client_kwargs(self) -> dict[str, str]:
        """Keyword arguments for :class:`praw.Reddit` (never logs them)."""
        self.require_reddit_credentials()
        kwargs = {
            "client_id": str(self.reddit_client_id),
            "client_secret": str(self.reddit_client_secret),
            "user_agent": str(self.reddit_user_agent),
        }
        if self.has_reddit_account:
            kwargs["username"] = str(self.reddit_username)
            kwargs["password"] = str(self.reddit_password)
        return kwargs

    def redacted(self) -> dict[str, object]:
        """Settings safe to print: secrets are replaced by a placeholder."""
        api_hash = self.api_hash
        return {
            "home": str(self.home),
            "database_file": str(self.database_file),
            "sessions_path": str(self.sessions_path),
            "proxy_file": str(self.proxy_path),
            "proxy_enabled": self.proxy_enabled,
            "log_level": self.log_level,
            "log_to_file": self.log_to_file,
            "session_name": self.session_name,
            "api_id": self.api_id if self.api_id else None,
            "api_hash": mask_secret(api_hash) if api_hash else None,
            "reddit_configured": self.has_reddit_credentials,
            "reddit_client_id": mask_secret(self.reddit_client_id)
            if self.reddit_client_id
            else None,
            "reddit_user_agent": self.reddit_user_agent,
            "reddit_account": str(self.reddit_username) if self.has_reddit_account else None,
        }


def mask_secret(value: str | None, keep: int = 4) -> str:
    """Return ``abcd********wxyz`` style masking for secret values."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Drop the cached settings and read them again (used by tests)."""
    get_settings.cache_clear()
    return get_settings()
