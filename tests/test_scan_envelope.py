"""Guard Proshop's scan budget against both measured refusal boundaries.

The older production incident established an hourly penalty band: bursts that
accumulated 193-313 pages in the preceding hour led to a penalty that survived
at least 10.5 minutes of complete silence. That justified the rolling-hour
guard, but did not explain later refusals at much lower hourly volume.

A controlled probe on 2026-09-22 stopped production, waited 32 minutes, then
requested GPU pagination at the real 2.5s cadence. Pages 1..12 returned HTTP
200 and request 13 returned HTTP 429 after 32.9 seconds. The same deep URLs had
returned 200 in a 45-second-spaced control. The binding production constraint
is therefore a 12-contact cycle burst; the hourly cap remains a secondary
failsafe.

The guard must also remain useful: GPU and RAM retain a fast-lap guarantee,
and the general catalogue retains an explicit anti-starvation bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from proshop.settings import Settings

# Highest hourly load that was served, and lowest that drew a refusal.
MEASURED_SERVED_MAX_PER_HOUR = 218
MEASURED_REFUSED_MIN_PER_HOUR = 193
# Sized below the overlapping band, not at its edge: the penalty outlives the
# traffic that caused it (>=10.5 min of 429 on an idle address), so the cost
# of being slightly wrong is asymmetric.
SAFE_RATE_PER_HOUR = 170

# Measured 2026-09-22 after 32 minutes of complete quiet: GPU pagination
# pages 1..12 returned HTTP 200 at production's 2.5s cadence; request 13
# returned HTTP 429. This is stricter than the older one-off 129-page probe
# and must cap every network contact in a cycle, including category refreshes
# and transport retries.
MEASURED_SAFE_CONTACTS_PER_CYCLE = 12
# proshop.timer fires every 15 minutes.
TIMER_PERIOD_S = 15 * 60
TIMER_PERIOD_H = TIMER_PERIOD_S / 3600
# A pass must leave room for parsing, database writes and delivery.
TIMER_BUDGET_SHARE = 0.75
# Listing queue size, measured 2026-09-21. NOT the 35,858-row
# `proshop-products` store: the cycle walks listing pages.
MEASURED_LISTING_PAGES = 1489
# Current live pagination: 15 GPU pages + 53 RAM pages. These are the user's
# highest-value categories and keep an explicit fast-lap guarantee even when
# the shop's hard burst threshold forces the broad catalogue to run slowly.
MEASURED_FOCUS_PAGES = 68
TARGET_FOCUS_LAP_HOURS = 4.0
TARGET_GENERAL_LAP_HOURS = 72.0


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
    assert pages <= MEASURED_SAFE_CONTACTS_PER_CYCLE, (
        f"{origin}: a {pages}-page burst would send the measured-refused "
        f"contact 13 (safe contacts: {MEASURED_SAFE_CONTACTS_PER_CYCLE})"
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


def test_the_shipped_pass_stays_below_the_measured_burst_threshold():
    settings = _defaults()
    assert settings.pages_per_pass <= MEASURED_SAFE_CONTACTS_PER_CYCLE, (
        f"the shop served 12 consecutive contacts and refused contact 13, "
        f"but the shipped pass still asks for {settings.pages_per_pass} pages"
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


def test_focus_budget_laps_gpu_and_ram_within_four_hours():
    """The burst guard must not make the highest-value categories stale."""
    settings = _defaults()
    focus_per_pass = round(settings.pages_per_pass * settings.focus_share)
    focus_per_hour = focus_per_pass / TIMER_PERIOD_H
    lap_hours = MEASURED_FOCUS_PAGES / focus_per_hour
    assert lap_hours <= TARGET_FOCUS_LAP_HOURS, (
        f"GPU+RAM would lap every {lap_hours:.1f}h, past the "
        f"{TARGET_FOCUS_LAP_HOURS}h target"
    )


def test_general_budget_still_laps_the_catalogue_within_three_days():
    """Safety may slow the tail, but must not silently starve it."""
    settings = _defaults()
    general_per_pass = settings.pages_per_pass - round(
        settings.pages_per_pass * settings.focus_share
    )
    general_per_hour = general_per_pass / TIMER_PERIOD_H
    general_pages = MEASURED_LISTING_PAGES - MEASURED_FOCUS_PAGES
    lap_hours = general_pages / general_per_hour
    assert lap_hours <= TARGET_GENERAL_LAP_HOURS, (
        f"the general catalogue would lap every {lap_hours:.1f}h, past the "
        f"{TARGET_GENERAL_LAP_HOURS}h starvation guard"
    )


@pytest.mark.parametrize(
    ("pages", "delay", "allowed"),
    [
        (12, 2.5, True),  # measured: contact 13 refused
        (40, 2.5, False),  # hourly-safe, but crosses the burst threshold
        (120, 2.5, False),  # 480/h - deployed on burst evidence, refused live
        (100, 2.5, False),  # 400/h
        (60, 2.5, False),  # 240/h - still above the refusal band
        (20, 2.0, False),  # hourly-safe, but contact 13 was refused live
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
