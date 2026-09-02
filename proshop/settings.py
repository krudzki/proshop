"""Settings specific to the Proshop scanner."""

from __future__ import annotations

from typing import ClassVar

from deal_pipeline.config import CoreSettings, flatten_config, load_yaml_config
from pydantic import AliasChoices, Field


class Settings(CoreSettings):
    CHANNELS: ClassVar[dict[str, str]] = {
        **CoreSettings.CHANNELS,
        "proshop": "proshop_webhook_url",
    }

    proshop_webhook_url: str = Field(default="")
    proshop_enabled: bool = Field(default=False)
    proshop_notify: bool = Field(default=False)
    pages_per_pass: int = Field(
        default=80,
        validation_alias=AliasChoices("pages_per_pass", "proshop_pages_per_pass"),
        ge=1,
        le=400,
    )
    focus_share: float = Field(
        default=0.5,
        validation_alias=AliasChoices("focus_share", "proshop_focus_share"),
        ge=0.0,
        le=1.0,
    )
    request_delay_s: float = Field(
        default=1.2,
        validation_alias=AliasChoices("request_delay_s", "proshop_request_delay_s"),
        ge=0.5,
    )
    category_refresh_hours: int = Field(
        default=24,
        validation_alias=AliasChoices("category_refresh_hours", "proshop_category_refresh_hours"),
        ge=1,
    )
    max_listing_pages: int = Field(default=400, ge=1, le=400)
    max_alerts_per_cycle: int = Field(default=5, ge=1, le=10)

    def webhook(self, channel: str) -> str:
        """Retain the shared webhook lookup while keeping type checkers happy."""
        return super().webhook(channel)


def get_settings() -> Settings:
    return Settings(**flatten_config(load_yaml_config()))  # type: ignore[call-arg]
