"""Canonical routing and RAM/GPU focus tests."""

from proshop.categories import canonical_category, is_focus, priority_of_slug


def test_ram_and_graphics_pages_are_the_only_reserved_focus():
    assert is_focus("https://www.proshop.pl/RAM")
    assert is_focus("https://www.proshop.pl/RAM?pn=51")
    assert is_focus("https://www.proshop.pl/Karta-graficzna?pn=16")
    assert not is_focus("https://www.proshop.pl/Plyta-glowna")
    assert not is_focus("https://www.proshop.pl/Komputer?pn=2")


def test_core_electronics_use_shared_p1_routes():
    for category in ("RAM", "Karta graficzna", "Procesor", "Dysk SSD", "Laptop", "Telefon komórkowy"):
        slug = canonical_category(category, "", "")
        assert priority_of_slug(slug) == "P1", (category, slug)


def test_photo_video_and_smart_home_do_not_fall_into_unclassified():
    assert canonical_category("Aparat fotograficzny", "", "") == "electronics:photo-video"
    assert canonical_category("Inteligentny dom", "", "") == "other"


def test_accessories_are_collected_but_not_promoted_to_focus():
    assert priority_of_slug(canonical_category("Kabel USB", "", "")) == "P3"
    assert not is_focus("https://www.proshop.pl/Kabel-USB?pn=4")
