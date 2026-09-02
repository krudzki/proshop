"""Parser for server-rendered Proshop category listings."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

BASE_URL = "https://www.proshop.pl"
AMOUNT = re.compile(r"(\d[\d\s\u00a0]*),(\d{2})")
PRODUCT_ID = re.compile(r"^\d+d?$", re.I)


@dataclass(frozen=True)
class ListingProduct:
    """One orderable or visible product card."""

    product_id: str
    name: str
    brand: str
    category: str
    price: float
    original_price: float | None
    mpn: str
    url: str
    image_url: str
    purchasable: bool
    is_outlet: bool

    @property
    def condition(self) -> str:
        return "used" if self.is_outlet else "new"


def parse_amount(text: str) -> float | None:
    """Parse a Polish gross amount without dropping the decimal separator."""
    match = AMOUNT.search((text or "").replace(" ", " "))
    if not match:
        return None
    whole = re.sub(r"\s", "", match.group(1))
    try:
        value = Decimal(f"{whole}.{match.group(2)}")
    except InvalidOperation:
        return None
    return float(value) if value > 0 else None


def _analytics_items(page: BeautifulSoup) -> dict[str, dict]:
    container = page.select_one(".site-productlist-container[data-gtm]")
    if container is None:
        return {}
    raw = str(container.get("data-gtm") or "")
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        payload = json.loads(decoded)
        items = payload.get("ecommerce", {}).get("items", [])
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return {
        str(item.get("item_id", "")): item
        for item in items
        if isinstance(item, dict) and item.get("item_id")
    }


def _rounded_analytics_price(item: dict) -> float | None:
    try:
        value = Decimal(str(item.get("price"))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return None
    return float(value) if value > 0 else None


def _portable_suffix(full_title: str, clean_name: str) -> str:
    """Return only Proshop's exact title suffix after the analytics name.

    The visible title mixes the product description with arbitrary tokens. The
    analytics name provides a clean boundary; without that exact boundary no
    identifier is emitted.
    """
    prefix = f"{clean_name} - "
    if not full_title.startswith(prefix):
        return ""
    suffix = full_title[len(prefix) :].strip()
    forbidden = {"<", ">", chr(10), chr(13)}
    if not suffix or len(suffix) > 80 or any(ch in suffix for ch in forbidden):
        return ""
    return suffix


def _absolute_asset(value: str) -> str:
    return urljoin(BASE_URL + "/", value or "") if value else ""


def parse_listing(html: str, page_url: str = "") -> list[ListingProduct]:
    """Parse products from a real listing; malformed/challenge pages yield none."""
    if not (html or "").strip():
        return []
    page = BeautifulSoup(html, "lxml")
    analytics = _analytics_items(page)
    if not analytics:
        return []

    products: list[ListingProduct] = []
    seen: set[str] = set()
    for card in page.select(".site-productlist-item"):
        id_node = card.select_one("input[name=productId]")
        product_id = str(id_node.get("value") or "").strip() if id_node else ""
        if not PRODUCT_ID.fullmatch(product_id) or product_id in seen:
            continue
        item = analytics.get(product_id)
        if not item:
            continue
        name = re.sub(r"\s+", " ", str(item.get("item_name") or "")).strip()
        if not name:
            continue

        link = card.select_one("a.show[href]")
        href = str(link.get("href") or "") if link else ""
        if not href:
            continue
        price_node = card.select_one(".site-currency-lg")
        price = parse_amount(price_node.get_text(" ", strip=True)) if price_node else None
        price = price or _rounded_analytics_price(item)
        if price is None:
            continue

        title = str(link.get("title") or "") if link else ""
        image = link.select_one("img") if link else None
        original_node = card.select_one(".presales-price")
        original = parse_amount(original_node.get_text(" ", strip=True)) if original_node else None
        purchasable = card.select_one('form[action="/Basket/AddItem"]') is not None
        is_outlet = product_id.lower().endswith("d") or "*demo*" in name.lower()
        products.append(
            ListingProduct(
                product_id=product_id,
                name=name[:240],
                brand=re.sub(r"\s+", " ", str(item.get("item_brand") or "")).strip()[:80],
                category=re.sub(r"\s+", " ", str(item.get("item_category") or "")).strip()[:100],
                price=price,
                original_price=original,
                mpn=_portable_suffix(title, name),
                url=urljoin(BASE_URL + "/", href),
                image_url=_absolute_asset(str(image.get("src") or "") if image else ""),
                purchasable=purchasable,
                is_outlet=is_outlet,
            )
        )
        seen.add(product_id)
    return products


def listing_page_count(html: str) -> int:
    """Declared final page number, defaulting to one."""
    page = BeautifulSoup(html or "", "lxml")
    numbers = []
    for link in page.select('a[href*="pn="]'):
        text = link.get_text(strip=True)
        if text.isdigit():
            numbers.append(int(text))
    return max(numbers or [1])


def _clean_category_url(href: str) -> str:
    absolute = urljoin(BASE_URL + "/", href)
    parts = urlsplit(absolute)
    return urlunsplit(("https", "www.proshop.pl", parts.path.rstrip("/"), "", ""))


def category_links(html: str) -> list[tuple[str, str]]:
    """Unique leaf category links from a Proshop root page."""
    page = BeautifulSoup(html or "", "lxml")
    found: dict[str, str] = {}
    for link in page.select("#subCategoryList a[href]"):
        name = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
        url = _clean_category_url(str(link.get("href") or ""))
        if name and url.startswith(BASE_URL + "/"):
            found[url] = name
    return sorted(found.items())
