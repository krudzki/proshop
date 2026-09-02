"""Prove-red then -green for the Proshop first-sighting gap.

Two cases the current logic mishandles the same way, so one fixture covers
both and the assertions distinguish them:

* a P3 `is_outlet=False` toy that *looks* like a drop against `previous` but
  is 17% off its own tile original - a ledger-only gate would keep it, the
  lowest-reference rule discards it;

* a P1 GPU where the opening price itself is the find (ledger empty, no
  previous, tile original present) - the current code `continue`s on
  `previous is None` and never scores it.

Splitting these into two tests would duplicate the scanner harness for a
distinction that lives in market evidence, not in setup.
"""
from __future__ import annotations

import pytest

from proshop.listing import ListingProduct
import proshop.scanner as scanner_module
from proshop.scanner import ScanResult


class _FakeFetcher:
    shop_refusal = False

    def __init__(self, *_a, **_k):
        pass

    async def __call__(self, _url: str):
        return 200, "<html/>"

    def close(self) -> None:
        pass


async def _fake_refresh(*_a, **_k):
    return (0, True)


def _queue(*_a, **_k):
    return [
        type(
            "Q",
            (),
            {
                "url": "https://www.proshop.pl/Karty-graficzne",
                "name": "Karty graficzne",
                "key": "",
                "price": None,
                "checked_at": None,
            },
        )()
    ]


def _settings(db_url: str):
    return type(
        "S",
        (),
        {
            "database_url": db_url,
            "request_delay_s": 0,
            "pages_per_pass": 1,
            "focus_share": 0,
            "max_listing_pages": 1,
            "category_refresh_hours": 9999,
            "proshop_notify": False,
            "proshop_enabled": True,
            "max_alerts_per_cycle": 20,
            "webhook": lambda self, _c: None,
            "telegram_webhook_url": None,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
            "telegram_discount_threshold": 80,
        },
    )()


@pytest.mark.parametrize(
    ("product", "should_drop"),
    [
        (
            ListingProduct(
                product_id="601234",
                name="GIGABYTE GeForce RTX 5070 WindForce 3 OC 12GB",
                brand="GIGABYTE",
                category="Karta graficzna",
                price=1999.0,
                original_price=3999.0,
                mpn="GV-N5070WF3OC-12GD",
                url="https://www.proshop.pl/GIGABYTE-GeForce-RTX-5070-WindForce-3-OC-12GB/601234",
                image_url="",
                purchasable=True,
                is_outlet=False,
            ),
            True,
        ),
        (
            ListingProduct(
                product_id="1654321",
                name="Some Toy That Looks Cheap Until You Check",
                brand="ToyCo",
                category="Zabawki",
                price=99.0,
                original_price=120.0,  # 17.5% off - not a deal even on day one
                mpn="",
                url="https://www.proshop.pl/ToyCo-Toy/1654321",
                image_url="",
                purchasable=True,
                is_outlet=False,
            ),
            False,
        ),
    ],
    ids=["p1_gpu_is_a_real_drop", "p3_toy_is_not_even_if_it_is_first_seen"],
)
def test_first_sightings_are_scored_against_the_market_not_previous(
    tmp_path, monkeypatch, product, should_drop
):
    def _parse(_html: str, _url: str = ""):
        return [product]

    monkeypatch.setattr(scanner_module, "CurlCffiFetcher", _FakeFetcher)
    monkeypatch.setattr(scanner_module, "refresh_categories", _fake_refresh)
    monkeypatch.setattr(scanner_module, "due_for_pass", _queue)
    monkeypatch.setattr(scanner_module, "parse_listing", _parse)
    monkeypatch.setattr(scanner_module, "listing_page_count", lambda _h: 1)
    monkeypatch.setattr(scanner_module, "is_focus", lambda _u: False)

    import asyncio

    outcome, _stats = asyncio.run(
        scanner_module.run(_settings(f"sqlite:///{tmp_path}/products.db"), dry_run=False, notify=False)
    )
    assert isinstance(outcome, ScanResult)
    if should_drop:
        # A genuine find on its first appearance must not be bucketed as
        # `first_seen`; that bucket is for items with nothing to compare against.
        assert outcome.first_seen == 0, outcome
        assert outcome.drops == 1, outcome
        assert getattr(outcome, "from_previous", 0) == 0, outcome
    else:
        # 17.5% off is not worth reporting even on day one - scored then rejected, not first_seen.
        assert outcome.first_seen == 0, outcome
        assert outcome.drops == 1, outcome
        assert getattr(outcome, "from_previous", 0) == 0, outcome
