"""Canonical category routing and listing-page focus."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_DIACRITICS = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
_FOCUS_PATHS = {"ram", "karta-graficzna"}


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_DIACRITICS).lower()).strip()


def is_focus(url: str) -> bool:
    """RAM and graphics-card listing pages receive the reserved pass share."""
    path = urlsplit(url or "").path.strip("/").lower()
    return path in _FOCUS_PATHS


def canonical_category(category: str, name: str = "", url: str = "") -> str:
    """Map shop labels into the fleet taxonomy before shared fallback."""
    value = normalise(category)
    if any(token in value for token in ("aparat", "obiektyw", "kamera", "foto", "zdjec", "dron")):
        return "electronics:photo-video"
    if "inteligentny dom" in value or "smart home" in value:
        return "other"
    try:
        from deal_pipeline.category_map import classify

        return classify(category=category, name=name, url=url, store="proshop")
    except ImportError:  # pragma: no cover - shared core is a hard dependency
        return "other"


def priority_of_slug(slug: str) -> str:
    try:
        from deal_pipeline.category_map import priority_of

        return priority_of(slug)
    except ImportError:  # pragma: no cover
        return "P3"
