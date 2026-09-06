"""An outlet Deal must declare used stock, not new.

The scanner already derives `condition` from `is_outlet` and uses it to pick
the right reference lane, but then hard-coded "new" when building the Deal.
The alert threshold reads that field, so scratched, opened and ex-display
units were judged against the new-stock bar.

Measured 2026-09-06 against the live ledger: 203 current Proshop outlet rows,
of which 32 obtain a reference when asked as new stock and 0 when asked as
used. The bars themselves differ only for a name-based reference (50% new vs
60% used), so the defect is latent today and would surface the moment outlet
name-matching produces a reference -- which is exactly what the RAM incident
did on the SuperTech side.
"""
from __future__ import annotations

from deal_pipeline.thresholds import minimum_discount_for

from proshop.scanner import condition_for


def test_condition_for_reports_outlet_stock_as_used():
    assert condition_for(True) == "used"
    assert condition_for(False) == "new"


def test_outlet_and_new_bars_differ_for_a_name_reference():
    """Why the hard-coded value mattered: the bar depends on the condition."""
    assert minimum_discount_for("model", "used") > minimum_discount_for("model", "new")


def test_scanner_passes_the_derived_condition_into_the_deal():
    """The Deal must carry the condition the scanner computed, not a literal.

    Read at the source level: constructing a full listing scan here would
    assert the shop's HTML, not this contract. The defect was a literal in the
    Deal(...) call, so that is what is pinned.
    """
    import inspect

    from proshop import scanner

    source = inspect.getsource(scanner.run)
    deal_call = source[source.index("deal = Deal("):]
    deal_call = deal_call[: deal_call.index(")\n")]
    assert "condition=condition" in deal_call, deal_call
    assert 'condition="new"' not in deal_call, deal_call


def test_outlet_lookups_use_the_used_stock_reference_lane():
    """The reference lane must be chosen by condition too, not just the bar.

    Found while proving the Deal guard red: mutating the condition passed to
    reference_from_ledger went undetected, because nothing pinned it. An
    outlet unit compared against new-stock prices would be measured against a
    different market than the one it is sold in.
    """
    import inspect

    from proshop import scanner

    source = inspect.getsource(scanner.run)
    lookup = source[source.index("reference_from_ledger("):]
    lookup = lookup[: lookup.index(")\n")]
    assert "condition=condition" in lookup, lookup
    assert 'condition="new"' not in lookup, lookup


def test_both_condition_uses_are_derived_not_hard_coded():
    """Neither call site may drift back to a literal.

    Two separate call sites take the condition (the reference lookup and the
    Deal), and they sit at the same indentation, so a careless edit to one
    reads exactly like the other. Pin the count.
    """
    import inspect

    from proshop import scanner

    source = inspect.getsource(scanner.run)
    assert source.count("condition=condition") == 2, source.count("condition=condition")
    assert 'condition="new"' not in source
