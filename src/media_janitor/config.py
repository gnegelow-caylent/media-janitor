"""Configuration management."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, EmailStr, Field


class ArrInstance(BaseModel):
    """Configuration for a Radarr or Sonarr instance."""

    name: str
    url: str
    api_key: str


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
    files_per_hour: int = 100
    schedule: str | None = None
    # Mode: "continuous" = keep re-scanning forever, "watch_only" = scan once then only watch new imports
    mode: Literal["continuous", "watch_only"] = "watch_only"


class WebhookConfig(BaseModel):
    """Webhook server settings."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9000


class ActionsConfig(BaseModel):
    """Action settings."""

    auto_replace: bool = True
    blocklist_bad_releases: bool = True
    max_replacements_per_day: int = 10


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


class LoggingConfig(BaseModel):
    """Logging settings."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = "/data/logs/media-janitor.log"


class Config(BaseModel):
    """Main configuration."""

    radarr: list[ArrInstance] = Field(default_factory=list)
    sonarr: list[ArrInstance] = Field(default_factory=list)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path = "/data/config.yaml") -> Config:
    """Load configuration from YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return Config(**data)
