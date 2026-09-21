"""Guard Proshop's scan budget against the shop's HOURLY allowance.

The mistake this file now exists to prevent: an isolated probe walked 129
pages at 2.5s spacing with no refusal, a 120-page budget was deployed on that
evidence, and production refused within two hours. A probe measures ONE burst
after a long idle. Production repeats that burst every 15 minutes, and the
shop meters cumulatively.

Measured 2026-09-21 across 43 production cycles, counting the pages fetched
in the HOUR PRECEDING each pass:

    39 served passes    0-218 pages in the previous hour (median 50)
     4 refused passes   193, 222, 222, 313

The bands overlap around 200/h, so that is where the ceiling sits. Refusals
landed on the hottest category (Karta-graficzna, 3 of 4) once the hour's
allowance was spent - not on a specific page, and not because any single pass
was too long: the three longest passes of the day (103, 100, 95 pages) were
all served.

So the quantity to pin is pages PER HOUR, which is a property of the budget
and the timer together. A test that only checks `pages_per_pass` cannot see
it, which is exactly why the first version of this file passed a
configuration that failed in production within two hours.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from proshop.settings import Settings

# Highest hourly load that was served, and lowest that drew a refusal.
MEASURED_SERVED_MAX_PER_HOUR = 218
MEASURED_REFUSED_MIN_PER_HOUR = 193
# Stay below the overlapping band rather than at its edge.
SAFE_RATE_PER_HOUR = 170

# The longest single pass observed without refusal, from idle. Still a real
# bound within one pass, just not the binding one.
MEASURED_SAFE_BURST = 129
# proshop.timer fires every 15 minutes.
TIMER_PERIOD_S = 15 * 60
TIMER_PERIOD_H = TIMER_PERIOD_S / 3600
# A pass must leave room for parsing, database writes and delivery.
TIMER_BUDGET_SHARE = 0.75
# Listing queue size, measured 2026-09-21. NOT the 35,858-row
# `proshop-products` store: the cycle walks listing pages.
MEASURED_LISTING_PAGES = 1489
# One lap per working day. The previous 20-page runtime took 18.6h.
TARGET_LAP_HOURS = 12.0


def hourly_rate(pages: int, period_h: float = TIMER_PERIOD_H) -> float:
    return pages / period_h


def assert_within_envelope(pages: int, delay_s: float, origin: str) -> None:
    rate = hourly_rate(pages)
    assert rate <= SAFE_RATE_PER_HOUR, (
        f"{origin}: {pages} pages every {TIMER_PERIOD_S / 60:.0f} min is "
        f"{rate:.0f} pages/h; passes were refused from "
        f"{MEASURED_REFUSED_MIN_PER_HOUR}/h and the highest served was "
        f"{MEASURED_SERVED_MAX_PER_HOUR}/h"
    )
    assert pages <= MEASURED_SAFE_BURST, (
        f"{origin}: a {pages}-page burst exceeds the longest refusal-free "
        f"run measured ({MEASURED_SAFE_BURST})"
    )
    duration = pages * delay_s
    assert duration <= TIMER_PERIOD_S * TIMER_BUDGET_SHARE, (
        f"{origin}: a pass spends ~{duration:.0f}s on pacing alone against a "
        f"{TIMER_PERIOD_S}s timer, leaving no room for parsing and delivery"
    )


def _defaults() -> Settings:
    fields = Settings.model_fields
    return Settings(
        discord_webhook_url="",
        pages_per_pass=fields["pages_per_pass"].default,
        request_delay_s=fields["request_delay_s"].default,
        focus_share=fields["focus_share"].default,
    )


def test_the_shipped_defaults_stay_within_the_hourly_allowance():
    settings = _defaults()
    assert_within_envelope(
        settings.pages_per_pass, settings.request_delay_s, "code defaults"
    )


def test_the_resolved_runtime_stays_within_the_hourly_allowance():
    """`.env` overrides the defaults, so it needs the same guard.

    Production really ran 20 pages at 2.0s from `.env` while the defaults
    said 80 at 1.2s, so reasoning from the defaults alone described a lane
    nobody was running.
    """
    if not Path(".env").exists():
        pytest.skip("no .env present - resolved settings are the defaults")
    settings = Settings()  # type: ignore[call-arg]
    assert_within_envelope(
        settings.pages_per_pass, settings.request_delay_s, "resolved runtime"
    )


def test_focus_keeps_its_reserved_share():
    settings = _defaults()
    assert settings.focus_share > 0.0, (
        "focus_share=0 lets a larger general budget crowd out the "
        "highest-resale categories entirely"
    )
    assert round(settings.pages_per_pass * settings.focus_share) >= 1


def test_the_budget_still_laps_the_catalogue_often_enough():
    """Safety is only half the rule: a safe pass can be uselessly small.

    20 pages at 2.0s sat well inside every safety bound and was exactly what
    production ran, lapping the 1,489-page listing queue once every ~18.6h.
    """
    settings = _defaults()
    pages_per_day = settings.pages_per_pass * (24 / TIMER_PERIOD_H)
    lap_hours = 24 * MEASURED_LISTING_PAGES / pages_per_day
    assert lap_hours <= TARGET_LAP_HOURS, (
        f"a {settings.pages_per_pass}-page pass laps the "
        f"{MEASURED_LISTING_PAGES}-page listing queue every {lap_hours:.1f}h, "
        f"past the {TARGET_LAP_HOURS}h target"
    )


@pytest.mark.parametrize(
    ("pages", "delay", "allowed"),
    [
        (40, 2.5, True),  # deployed: 160/h
        (120, 2.5, False),  # 480/h - deployed on burst evidence, refused live
        (100, 2.5, False),  # 400/h
        (60, 2.5, False),  # 240/h - still above the refusal band
        (20, 2.0, True),  # 80/h - safe, but see the lap test above
    ],
)
def test_the_envelope_rejects_rates_the_shop_refused(
    pages: int, delay: float, allowed: bool
):
    try:
        assert_within_envelope(pages, delay, "case")
    except AssertionError:
        accepted = False
    else:
        accepted = True
    assert accepted is allowed
