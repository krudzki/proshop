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
        # The binding limit is a short-burst threshold, not the older hourly
        # estimate. Measured 2026-09-22 after 32 minutes of complete quiet:
        # GPU pagination requests 1..12 returned HTTP 200 at the production
        # 2.5s cadence; request 13 returned HTTP 429. The same URLs all served
        # when spaced 45 seconds apart, proving the URL was not the cause.
        #
        # Keep the listing budget at 12, and separately cap ALL real contacts
        # in the cycle (category refreshes and retries included) in scanner.py.
        # A breach is not self-healing: the penalty survived >=10.5 minutes of
        # silence, so crossing the line costs much more than one skipped page.
        default=12,
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
        # Keep the measured production pacing. It is not sufficient on its
        # own: contact 13 was refused at this exact 2.5s cadence, so the hard
        # per-cycle contact budget is the binding protection.
        default=2.5,
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
    alert_rate_ceiling: float = Field(
        # The breaker catches a pass that has gone wrong - a parser regression
        # making every product look like a 90% discount - not a pass that
        # simply covered more ground. An absolute cap conflates the two.
        #
        # Measured 2026-09-21, the day the page budget went from 20 to 120:
        #
        #     103 pages  2,210 products  qualified 7  reported 0  TRIPPED
        #     100 pages  2,213 products  qualified 5  reported 5
        #      95 pages  2,142 products  qualified 9  reported 0  TRIPPED
        #
        # 22 genuine alerts were swallowed that day while the unit exited 0.
        #
        # Sized from the real distribution rather than a guess: across 255
        # cycles over three days with >=50 products, the qualification rate
        # ran median 0.26%, p90 0.92%, p99 1.75%, max 2.02%. The one cycle
        # above 1% (350 products, 6 alerts, 1.71%) was verified healthy - its
        # rejected discounts sat in the 0-10% band with a 37.3% maximum, not
        # the flat 90% signature of a parser fault.
        #
        # 3% clears the measured maximum with margin while still stopping a
        # runaway pass: at ~2,200 products the cap is 66, and a regression
        # qualifying a meaningful share of the catalogue trips it at once.
        default=0.03,
        validation_alias=AliasChoices("alert_rate_ceiling", "proshop_alert_rate_ceiling"),
        ge=0.0,
        le=1.0,
    )

    def webhook(self, channel: str) -> str:
        """Retain the shared webhook lookup while keeping type checkers happy."""
        return super().webhook(channel)


def get_settings() -> Settings:
    return Settings(**flatten_config(load_yaml_config()))  # type: ignore[call-arg]
