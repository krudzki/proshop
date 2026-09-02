"""Listing parser tests reduced from live Proshop pages."""

from __future__ import annotations

import base64
import json

from proshop.listing import (
    category_links,
    listing_page_count,
    parse_amount,
    parse_listing,
)


def _fixture() -> str:
    analytics = {
        "event": "view_item_list",
        "ecommerce": {
            "items": [
                {
                    "item_id": "3331078",
                    "item_name": "GIGABYTE GeForce RTX 5070 WindForce 3 OC - 12GB GDDR7 RAM - Karta graficzna",
                    "item_brand": "GIGABYTE",
                    "item_category": "Karta graficzna",
                    "price": 3298.999974,
                    "quantity": 1,
                },
                {
                    "item_id": "3441236d",
                    "item_name": "Motorola moto g86 256GB/8GB - PANTONE Spellbound *DEMO*",
                    "item_brand": "Motorola",
                    "item_category": "Telefon komórkowy",
                    "price": 49.99,
                    "quantity": 1,
                },
            ]
        },
    }
    encoded = base64.b64encode(json.dumps(analytics).encode()).decode()
    return f"""
    <html><body><main>
      <ul id="subCategoryList">
        <li><a href="/RAM">RAM</a></li>
        <li><a href="/Karta-graficzna">Karta graficzna</a></li>
      </ul>
      <ul id="products" class="site-productlist-container" data-gtm="{encoded}">
        <li class="site-productlist-item">
          <a class="show" href="/Karta-graficzna/GIGABYTE-RTX-5070/3331078"
             title="GIGABYTE GeForce RTX 5070 WindForce 3 OC - 12GB GDDR7 RAM - Karta graficzna - GV-N5070WF3OC-12GD">
            <img src="/Images/174x116/3331078.png">
          </a>
          <div class="site-stock"><i class="site-icon-stock-in"></i><span>Na stanie</span></div>
          <div class="presales-price">Normalna cena 3 999,00 zł</div>
          <span class="site-currency-lg">3 299,00 zł</span>
          <form action="/Basket/AddItem"><input name="productId" value="3331078"></form>
        </li>
        <li class="site-productlist-item">
          <a class="show" href="/Telefon-komorkowy/Motorola-moto-g86-DEMO/3441236d"
             title="Motorola moto g86 256GB/8GB - PANTONE Spellbound *DEMO* - PB7L0086SE">
            <img src="/Images/174x116/3441236.jpg">
          </a>
          <div class="site-stock"><i class="site-icon-stock-comming"></i><span>Zamówiony</span></div>
          <span class="site-currency-lg">49,99 zł</span>
          <form action="/Basket/AddItem"><input name="productId" value="3441236d"></form>
        </li>
      </ul>
      <a href="/RAM?pn=2">2</a><a href="/RAM?pn=51">51</a>
    </main></body></html>
    """


def test_listing_extracts_clean_identity_price_and_stock():
    products = parse_listing(_fixture(), "https://www.proshop.pl/RAM")
    assert len(products) == 2
    gpu = products[0]
    assert gpu.product_id == "3331078"
    assert gpu.name.startswith("GIGABYTE GeForce RTX 5070")
    assert gpu.brand == "GIGABYTE"
    assert gpu.category == "Karta graficzna"
    assert gpu.price == 3299.00
    assert gpu.mpn == "GV-N5070WF3OC-12GD"
    assert gpu.purchasable is True
    assert gpu.original_price == 3999.00
    assert gpu.url.endswith("/3331078")
    assert gpu.image_url == "https://www.proshop.pl/Images/174x116/3331078.png"


def test_demo_product_is_a_separate_condition_bucket():
    demo = parse_listing(_fixture(), "https://www.proshop.pl/Telefon-komorkowy")[1]
    assert demo.product_id == "3441236d"
    assert demo.is_outlet is True
    assert demo.condition == "used"
    assert demo.price == 49.99
    assert demo.mpn == "PB7L0086SE"


def test_normal_price_is_parsed_but_not_exposed_as_reference_evidence():
    gpu = parse_listing(_fixture(), "https://www.proshop.pl/RAM")[0]
    assert gpu.original_price == 3999.00
    assert not hasattr(gpu, "reference_price")


def test_amount_parser_keeps_decimal_separator_and_thousands():
    assert parse_amount("49,99 zł") == 49.99
    assert parse_amount("9 599,99 zł") == 9599.99
    assert parse_amount("11 479,90 zł") == 11479.90
    assert parse_amount("no price") is None


def test_mpn_is_not_guessed_when_title_is_not_an_exact_name_suffix():
    html = _fixture().replace(
        ' - GV-N5070WF3OC-12GD">', ' unrelated title token">', 1
    )
    assert parse_listing(html, "https://www.proshop.pl/RAM")[0].mpn == ""


def test_page_count_and_leaf_discovery_are_deterministic():
    assert listing_page_count(_fixture()) == 51
    assert category_links(_fixture()) == [
        ("https://www.proshop.pl/Karta-graficzna", "Karta graficzna"),
        ("https://www.proshop.pl/RAM", "RAM"),
    ]


def test_challenge_shell_is_not_a_valid_listing():
    assert parse_listing("<html><title>Too Many Requests</title></html>", "https://www.proshop.pl/RAM") == []
