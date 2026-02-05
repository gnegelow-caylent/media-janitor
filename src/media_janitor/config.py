"""Configuration management."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class PathMapping(BaseModel):
    """Maps container paths to host paths."""

    from_path: str  # Path as reported by Radarr/Sonarr (e.g., "/tv")
    to_path: str    # Actual path in media-janitor container (e.g., "/media/tv")


class ArrInstance(BaseModel):
    """Configuration for a Radarr or Sonarr instance."""

    name: str
    url: str
    api_key: str
    path_mappings: list[PathMapping] = Field(default_factory=list)


class PlexConfig(BaseModel):
    """Plex server configuration."""

    enabled: bool = False
    url: str = ""
    token: str = ""
    # Trigger library refresh after replacements
    refresh_on_replace: bool = True


class ValidationConfig(BaseModel):
    """Validation settings."""

    check_duration_sanity: bool = True
    max_duration_hours: int = 12

    check_bitrate: bool = True
    min_bitrate_720p: int = 1500
    min_bitrate_1080p: int = 3000
    min_bitrate_4k: int = 8000

    deep_scan_enabled: bool = True
    sample_duration_seconds: int = 30

    full_decode_enabled: bool = False


class ScannerConfig(BaseModel):
    """Background scanner settings."""

    enabled: bool = True
    files_per_hour: int = 300
    # Number of files to process in parallel (higher = faster but more CPU/memory)
    concurrency: int = 10
    schedule: str | None = None
    # Mode: "continuous" = keep re-scanning forever, "watch_only" = scan once then only watch new imports
    mode: Literal["continuous", "watch_only"] = "watch_only"
    # Cron schedule for TV library refresh (slow operation, runs in background)
    # Default: 3am daily. Set to null to disable automatic TV refresh.
    tv_refresh_schedule: str | None = "0 3 * * *"


class WebhookConfig(BaseModel):
    """Webhook server settings."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9000


class ActionsConfig(BaseModel):
    """Action settings."""

    auto_replace: bool = True
    auto_delete_duplicates: bool = False
    blocklist_bad_releases: bool = True
    max_replacements_per_day: int = 10
    dry_run: bool = False  # Report only, no actual changes


class EmailConfig(BaseModel):
    """Email notification settings."""

    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    to_address: str = ""
    daily_summary_time: str = "08:00"


class DiscordConfig(BaseModel):
    """Discord notification settings."""

    enabled: bool = False
    webhook_url: str = ""


class SlackConfig(BaseModel):
    """Slack notification settings."""

    enabled: bool = False
    webhook_url: str = ""


class TelegramConfig(BaseModel):
    """Telegram notification settings."""

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class PushoverConfig(BaseModel):
    """Pushover notification settings."""

    enabled: bool = False
    user_key: str = ""
    api_token: str = ""


class GotifyConfig(BaseModel):
    """Gotify notification settings."""

    enabled: bool = False
    server_url: str = ""
    app_token: str = ""


class NotificationsConfig(BaseModel):
    """All notification settings."""

    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    pushover: PushoverConfig = Field(default_factory=PushoverConfig)
    gotify: GotifyConfig = Field(default_factory=GotifyConfig)


class LoggingConfig(BaseModel):
    """Logging settings."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = "/data/logs/media-janitor.log"


class UIConfig(BaseModel):
    """UI settings."""

    theme: Literal["dark", "light"] = "dark"
    timezone: str = "America/New_York"


class Config(BaseModel):
    """Main configuration."""

    radarr: list[ArrInstance] = Field(default_factory=list)
    sonarr: list[ArrInstance] = Field(default_factory=list)
    plex: PlexConfig = Field(default_factory=PlexConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


def load_config(path: str | Path = "/data/config.yaml") -> Config:
    """Load configuration from YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return Config(**data)
