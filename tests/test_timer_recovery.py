"""The timer must give the shop a full recovery gap after every pass.

Measured 2026-09-21 over 41 production cycles: refusals did not track pass
size, they tracked the idle gap before the pass started.

    37 served passes    idle 12.9-26.6 min (median 15.5)
     2 refused passes   idle 2.2 min and 10.1 min
     3 passes >= 95 pages, 384-477s each, zero refusals

The trap is that `proshop.timer` used to declare both OnUnitActiveSec and
OnUnitInactiveSec at 15min. systemd arms both and fires on whichever comes
first, so OnUnitActiveSec (counted from the START of the run) silently
subtracts the run's own duration from the rest period. A 70-second pass hid
this completely; a 400-second pass at the raised budget did not.

These tests read the unit file this repository ships, because that file is
the deployed artefact - the setting is not expressible in Python.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parents[1] / "systemd" / "proshop.timer"

# The shortest idle gap observed before a pass that was served, 2026-09-21.
MEASURED_SAFE_IDLE_MIN = 12.9
# The longest idle gap that still drew HTTP 429.
MEASURED_REFUSED_IDLE_MIN = 10.1
# The longest pass observed at the 120-page budget.
MEASURED_LONGEST_PASS_S = 477


def _timer_section() -> dict[str, str]:
    """Parse [Timer] into a dict, ignoring comments and other sections."""
    settings: dict[str, str] = {}
    section = ""
    for raw in UNIT.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Timer" and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    return settings


def _minutes(value: str) -> float:
    """Parse the systemd time spans this unit actually uses."""
    total = 0.0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(min|s|h)", value):
        total += float(amount) * {"min": 1.0, "s": 1 / 60, "h": 60.0}[unit]
    if not total:
        raise ValueError(f"unparsed systemd time span: {value!r}")
    return total


def test_the_timer_does_not_count_the_gap_from_the_run_start():
    """OnUnitActiveSec shortens the rest by however long the pass took."""
    settings = _timer_section()
    assert "OnUnitActiveSec" not in settings, (
        "OnUnitActiveSec is armed alongside OnUnitInactiveSec and fires "
        "first, so the shop's recovery gap shrinks by the pass duration "
        f"(up to {MEASURED_LONGEST_PASS_S}s at the current budget). A pass "
        f"that began {MEASURED_REFUSED_IDLE_MIN} min after the previous one "
        "ended was refused with HTTP 429."
    )


def test_the_recovery_gap_clears_the_measured_refusal_band():
    """The scheduled gap must sit above every gap seen to draw a refusal."""
    settings = _timer_section()
    assert "OnUnitInactiveSec" in settings, (
        "without OnUnitInactiveSec nothing guarantees any gap after a pass"
    )
    gap = _minutes(settings["OnUnitInactiveSec"])
    assert gap >= MEASURED_SAFE_IDLE_MIN, (
        f"a {gap:.1f} min gap is inside the band where Proshop refused "
        f"(refusals seen up to {MEASURED_REFUSED_IDLE_MIN} min idle; the "
        f"shortest served gap was {MEASURED_SAFE_IDLE_MIN} min)"
    )


@pytest.mark.parametrize(
    ("unit_body", "allowed"),
    [
        ("[Timer]\nOnUnitInactiveSec=15min\n", True),
        # The shipped-before state: both armed, so rest shrinks by pass time.
        ("[Timer]\nOnUnitActiveSec=15min\nOnUnitInactiveSec=15min\n", False),
        # Honest but too eager.
        ("[Timer]\nOnUnitInactiveSec=5min\n", False),
        # No gap guarantee at all.
        ("[Timer]\nOnCalendar=*:0/15\n", False),
    ],
)
def test_the_rule_discriminates(monkeypatch, tmp_path, unit_body, allowed):
    """Prove the guard rejects the shapes it claims to reject."""
    candidate = tmp_path / "proshop.timer"
    candidate.write_text(unit_body, encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.UNIT", candidate)
    try:
        test_the_timer_does_not_count_the_gap_from_the_run_start()
        test_the_recovery_gap_clears_the_measured_refusal_band()
    except AssertionError:
        accepted = False
    else:
        accepted = True
    assert accepted is allowed
