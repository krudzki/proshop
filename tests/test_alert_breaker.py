"""The alert circuit breaker must scale with the size of the pass.

The breaker exists to catch a pass that has gone WRONG - a parser regression
where every product suddenly looks like a 90% discount - and to refuse to
flood Discord with it. It is not a volume control for a healthy pass.

`max_alerts_per_cycle=5` was sized when a pass read ~400 products. Raising the
budget to 120 pages took a pass to ~2,200 products, and the same absolute cap
started tripping on ordinary work. Measured 2026-09-21, the day the budget was
raised:

    103 pages  2,210 products  qualified 7  reported 0  TRIPPED
    100 pages  2,213 products  qualified 5  reported 5
     95 pages  2,142 products  qualified 9  reported 0  TRIPPED

22 real alerts were swallowed that day, while the unit exited 0 and every
counter looked healthy. Better coverage had turned into worse delivery.

An anomaly is only meaningful relative to the work done, so the cap is a SHARE
of the products actually seen, with an absolute floor so a tiny pass still
cannot spam. These tests pin that behaviour against the measured rates.
"""

from __future__ import annotations

import pytest

from proshop.scanner import alert_cap
from proshop.settings import Settings


def _settings(**overrides) -> Settings:
    fields = Settings.model_fields
    values = {
        "discord_webhook_url": "",
        "max_alerts_per_cycle": fields["max_alerts_per_cycle"].default,
        "alert_rate_ceiling": fields["alert_rate_ceiling"].default,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[call-arg]


# Qualification rates actually observed on healthy passes, 2026-09-21.
# The 350/6 entry was the highest rate in 255 cycles (1.71%) and was verified
# healthy from its discount bands before being admitted here - see
# `.hermes/size_alert_ceiling.py`. Keeping it pins the ceiling above real
# behaviour instead of above a convenient guess.
HEALTHY_OBSERVATIONS = [
    (350, 6),  # 1.71% - the measured maximum, tripped by the old absolute cap
    (2210, 7),
    (2213, 5),
    (2142, 9),
]


@pytest.mark.parametrize(("products", "qualified"), HEALTHY_OBSERVATIONS)
def test_healthy_measured_passes_are_delivered(products: int, qualified: int):
    """Every pass observed to be healthy must get through the breaker."""
    cap = alert_cap(products, _settings())
    assert qualified <= cap, (
        f"a pass that saw {products} products and qualified {qualified} deals "
        f"is within measured normality, but the cap of {cap} would swallow it"
    )


def test_a_runaway_pass_still_trips_the_breaker():
    """A parser regression qualifying most of the catalogue must be stopped."""
    settings = _settings()
    products = 2200
    runaway = products // 2
    assert runaway > alert_cap(products, settings), (
        "half the catalogue qualifying is the failure the breaker exists for"
    )


def test_a_tiny_pass_cannot_be_used_to_spam():
    """The share must never drop the cap below the absolute floor."""
    settings = _settings()
    assert alert_cap(0, settings) == settings.max_alerts_per_cycle
    assert alert_cap(10, settings) == settings.max_alerts_per_cycle


def test_the_cap_grows_with_the_pass():
    """A pass five times larger must not be held to the same absolute cap."""
    settings = _settings()
    assert alert_cap(2200, settings) > alert_cap(400, settings), (
        "an absolute cap turns extra coverage into swallowed alerts"
    )


def test_the_ceiling_sits_above_every_measured_healthy_rate():
    """The ceiling must clear real behaviour, not just the cases listed here.

    Measured over 255 cycles with >=50 products across three days: median
    0.26%, p90 0.92%, p99 1.75%, max 2.02%.
    """
    settings = _settings()
    assert settings.alert_rate_ceiling > 0.0202, (
        f"a {settings.alert_rate_ceiling*100:.1f}% ceiling sits under the "
        "2.02% maximum qualification rate measured on healthy passes"
    )


def test_the_floor_and_the_share_are_both_honoured():
    """Whichever of the two is larger wins, at every size."""
    settings = _settings(max_alerts_per_cycle=5, alert_rate_ceiling=0.02)
    assert alert_cap(100, settings) == 5  # floor wins: 2 < 5
    assert alert_cap(1000, settings) == 20  # share wins: 20 > 5
