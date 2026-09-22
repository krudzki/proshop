"""Proshop Poland electronics listing scanner."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit, urlunsplit

import structlog
from deal_pipeline.catalog import Catalog, Page
from deal_pipeline.database import Database
from deal_pipeline.deal import Deal
from deal_pipeline.events import SOURCE_OWN, product_key
from deal_pipeline.models import OutletGroup, Product as NotificationProduct
from deal_pipeline.notifier import DiscordNotifier
from deal_pipeline.own_reference import reference_from_ledger
from deal_pipeline.rejected import RejectedDrops
from deal_pipeline.thresholds import report_reason
from dotenv import load_dotenv

from proshop import (
    LISTING_STORE,
    NEW_SOURCE,
    OUTLET_SOURCE,
    PRODUCT_STORE,
    SELLER,
    STORE,
)
from proshop.categories import canonical_category, is_focus
from proshop.fetcher import CurlCffiFetcher
from proshop.listing import category_links, listing_page_count, parse_listing
from proshop.queue import QueuedPage, due_for_pass
from proshop.settings import Settings, get_settings

logger = structlog.get_logger("proshop")
BASE_URL = "https://www.proshop.pl"
BOT_NAME = "Proshop Bot"
REFRESH_STATE_KEY = "proshop_categories_refreshed"
STORE_REFUSAL_EXIT_CODE = 4

# These are the electronics roots exposed in Proshop's main menu. Smart Home
# is nested under networking and has no `subCategoryList`, so it is admitted as
# a standalone listing below. Broad root pagination is deliberately not queued:
# it overlaps the leaf pages and would spend most of the budget on duplicates.
ROOT_CATEGORY_URLS = (
    f"{BASE_URL}/Komputer",
    f"{BASE_URL}/Sprzet-komputerowy",
    f"{BASE_URL}/Telefon-i-Tablet",
    f"{BASE_URL}/Telewizor-i-RTV",
    f"{BASE_URL}/Gry-i-Gaming",
    f"{BASE_URL}/Elementy-Sieciowe",
    f"{BASE_URL}/Drukarka-i-Akcesoria",
    f"{BASE_URL}/Kable-i-Wtyczki",
    f"{BASE_URL}/Zdjecia-i-Wideo",
)
STANDALONE_CATEGORIES = ((f"{BASE_URL}/Inteligentny-dom", "Inteligentny dom"),)
FOCUS_CATEGORIES = (
    (f"{BASE_URL}/RAM", "RAM"),
    (f"{BASE_URL}/Karta-graficzna", "Karta graficzna"),
)


@dataclass
class ScanResult:
    listing_pages: int = 0
    listing_fetch_errors: int = 0
    empty_listings: int = 0
    products_seen: int = 0
    duplicate_products: int = 0
    unavailable: int = 0
    first_seen: int = 0
    unchanged: int = 0
    increased: int = 0
    drops: int = 0
    from_ledger: int = 0
    from_tile: int = 0
    without_reference: int = 0
    with_code: int = 0
    focus_pages: int = 0
    qualified: int = 0
    already_reported: int = 0
    reported: int = 0
    circuit_breaker: bool = False
    shop_refusal: bool = False
    categories_added: int = 0
    listing_pages_added: int = 0
    listing_pages_removed: int = 0


@dataclass(frozen=True)
class PendingAlert:
    deal: Deal
    canonical: str
    producer: str
    image_url: str
    reason: str
    event_key: str


class ProshopSender:
    """Render shared deal data on canonical category routes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fallback = settings.webhook(STORE)

    async def __call__(self, alert: PendingAlert) -> bool:
        webhook = self._settings.webhook(alert.canonical) or self._fallback
        if not webhook:
            logger.error("missing_proshop_route", canonical=alert.canonical)
            return False
        deal = alert.deal
        unit = NotificationProduct(
            id=alert.event_key,
            name=deal.name,
            url=deal.url,
            price_current=deal.price,
            price_original=deal.reference_price or 0.0,
            discount_percent=deal.discount_percent,
            category="Proshop",
            image_url=alert.image_url,
            producer=alert.producer,
            condition=" • ".join(deal.conditions),
        )
        group = OutletGroup(
            key=alert.event_key,
            name=deal.name,
            price=deal.price,
            reference_price=deal.reference_price,
            discount_percent=deal.discount_percent,
            category="Proshop",
            image_url=alert.image_url,
            producer=alert.producer,
            units=[unit],
            source="scanner",
            reason=alert.reason,
            is_price_drop=True,
            previous_price=deal.reference_price,
        )
        await DiscordNotifier(
            webhook_url=webhook,
            bot_name=BOT_NAME,
            telegram_url=self._settings.telegram_webhook_url,
            telegram_token=self._settings.telegram_bot_token,
            telegram_chat=self._settings.telegram_chat_id,
            telegram_discount_threshold=self._settings.telegram_discount_threshold,
        ).notify_outlet_groups([group])
        return True


def should_refresh(
    last_refresh: str | None,
    interval_hours: int = 24,
    now: datetime | None = None,
) -> bool:
    if not last_refresh:
        return True
    try:
        last = datetime.fromisoformat(last_refresh)
    except ValueError:
        return True
    return (now or datetime.now()) - last >= timedelta(hours=interval_hours)


def should_refresh_catalog(last_refresh: str | None, dry_run: bool, interval_hours: int = 24) -> bool:
    """Dry-run discovery must never mutate the persistent listing queue."""
    return not dry_run and should_refresh(last_refresh, interval_hours)


# How long a refused category refresh waits before trying again.
#
# The refresh success timestamp is only written when the sweep COMPLETES, and
# a refusal forces `complete=False`, so on its own it can never delay a retry:
# measured live 2026-09-21, the scanner re-ran the refresh on every one of 24
# consecutive cycles, firing up to 12 root requests each time into an address
# Cloudflare was already refusing. The scanner was sustaining the very penalty
# it was waiting out.
#
# Sized above the 17-minute timer cadence so the retry genuinely skips cycles,
# and well under the 24h refresh interval so a transient block does not cost a
# day of category discovery.
REFRESH_BACKOFF_MINUTES = 60
REFUSED_REFRESH_STATE_KEY = "proshop_categories_refused"


def refresh_backoff_until(
    refused_at: str | None,
    now: datetime | None = None,
    minutes: int = REFRESH_BACKOFF_MINUTES,
) -> bool:
    """True while a refused refresh must NOT be retried.

    Unparseable or absent state means "never refused", so a corrupt value can
    only cost one extra refresh attempt - it can never wedge discovery off.
    """
    if not refused_at:
        return False
    try:
        refused = datetime.fromisoformat(refused_at)
    except ValueError:
        return False
    return (now or datetime.now()) - refused < timedelta(minutes=minutes)


# Two independent limits were measured. The binding one is the short burst:
# after 32 minutes of complete quiet, contacts 1..12 returned 200 and contact
# 13 returned 429 at the production 2.5s cadence. It must cover every real
# network contact in a cycle, including category refreshes and retries.
#
# The older hourly guard remains as a second line of defence. On 2026-09-21,
# refused passes followed 193, 222, 222 and 313 listing pages in the preceding
# hour, and the penalty survived >=10.5 minutes of silence. With the new
# 12-contact cycle cap and 15-minute timer it is normally inert (~48/h), but it
# still prevents a config or explicit operator limit from recreating the old
# runaway backlog.
MAX_CONTACTS_PER_CYCLE = 12
HOURLY_PAGE_CAP = 160
HOURLY_WINDOW = timedelta(hours=1)


def pages_allowed_now(
    recent_cycles: list[dict],
    pass_budget: int,
    hourly_cap: int = HOURLY_PAGE_CAP,
    now: datetime | None = None,
) -> int:
    """How many listing pages this pass may still fetch.

    Fails OPEN: unreadable or absent history means no evidence of a breach, so
    the pass keeps its normal budget. A guard that silences the scanner
    whenever it cannot read the past is the silent-starvation shape and is
    worse than the burst it prevents.
    """
    moment = now or datetime.now()
    spent = 0
    for cycle in recent_cycles or ():
        stamp = cycle.get("ts")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp))
        except (ValueError, TypeError):
            continue
        if moment - when >= HOURLY_WINDOW:
            continue
        try:
            spent += int(cycle.get("listing_pages") or 0)
        except (TypeError, ValueError):
            continue
    return max(0, min(pass_budget, hourly_cap - spent))


def _base_listing_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.proshop.pl", parts.path.rstrip("/"), "", ""))


def listing_page_urls(url: str, page_count: int, cap: int = 400) -> list[str]:
    """Canonical page addresses respecting Proshop's visible 400-page cap."""
    base = _base_listing_url(url)
    final_page = max(1, min(int(page_count), int(cap)))
    return [base, *[f"{base}?pn={page}" for page in range(2, final_page + 1)]]


def prune_listing_pages(conn, url: str, final_page: int) -> int:
    """Remove stale pagination addresses beyond a listing's current end.

    The shop's range can shrink (measured live: GPU 17 -> 15, RAM 54 -> 53).
    Discovery used to append the new range without retiring the old tail, so
    the scanner kept spending contacts on pages the shop no longer offered.
    Delete exact URLs only; a broad prefix delete could touch another category.
    """
    base = _base_listing_url(url)
    rows = conn.execute(
        "SELECT url FROM strony WHERE sklep = ? AND url LIKE ?",
        (LISTING_STORE, f"{base}?pn=%"),
    ).fetchall()
    stale: list[str] = []
    for row in rows:
        candidate = str(row["url"] if hasattr(row, "keys") else row[0])
        values = parse_qs(urlsplit(candidate).query).get("pn", [])
        if len(values) != 1:
            continue
        try:
            page_number = int(values[0])
        except (TypeError, ValueError):
            continue
        if page_number > max(1, int(final_page)):
            stale.append(candidate)
    if stale:
        conn.executemany(
            "DELETE FROM strony WHERE sklep = ? AND url = ?",
            [(LISTING_STORE, stale_url) for stale_url in stale],
        )
        conn.commit()
    return len(stale)


def sync_listing_page_range(
    catalog: Catalog,
    conn,
    page: Page,
    *,
    page_count: int,
    cap: int,
) -> tuple[int, int]:
    """Append the live range, stamp the visit, then retire its stale tail."""
    final_page = min(max(1, int(page_count)), max(1, int(cap)))
    expanded = [
        (listing_url, page.name)
        for listing_url in listing_page_urls(page.url, final_page, cap)
    ]
    added = _append_named(catalog, conn, expanded)
    # A stale URL can itself be the page currently being visited. Stamp it
    # before pruning; the opposite order silently resurrects that stale row.
    catalog.save_record(page)
    removed = prune_listing_pages(conn, page.url, final_page)
    return added, removed


def source_for(is_outlet: bool) -> str:
    return OUTLET_SOURCE if is_outlet else NEW_SOURCE


def condition_for(is_outlet: bool) -> str:
    return "used" if is_outlet else "new"


def _append_named(catalog: Catalog, conn, rows: list[tuple[str, str]]) -> int:
    """Append queue addresses and store the shop category used for ranking."""
    unique = sorted(dict(rows).items())
    added = catalog.append([url for url, _name in unique])
    conn.executemany(
        "UPDATE strony SET nazwa = ? WHERE sklep = ? AND url = ?",
        [(name, LISTING_STORE, url) for url, name in unique],
    )
    conn.commit()
    return added


async def refresh_categories(fetcher: CurlCffiFetcher, catalog: Catalog, conn) -> tuple[int, bool]:
    """Discover all electronics leaves from the store's own current menu."""
    discovered = list(FOCUS_CATEGORIES) + list(STANDALONE_CATEGORIES)
    complete = True
    for root in ROOT_CATEGORY_URLS:
        status, html = await fetcher(root)
        if fetcher.shop_refusal:
            return _append_named(catalog, conn, discovered), False
        if status != 200 or not html:
            complete = False
            logger.warning("category_root_unavailable", status=status, url=root)
            continue
        leaves = category_links(html)
        if not leaves:
            complete = False
            logger.warning("category_root_empty", url=root)
            continue
        discovered.extend(leaves)
    added = _append_named(catalog, conn, discovered)
    return added, complete


def _previous_price(conn, url: str) -> float | None:
    row = conn.execute(
        "SELECT cena FROM strony WHERE sklep = ? AND url = ?",
        (PRODUCT_STORE, url),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _dry_run_pages(limit: int) -> list[QueuedPage]:
    rows = list(FOCUS_CATEGORIES)[: max(1, limit)]
    return [QueuedPage(url=url, key="", name=name, price=None, checked_at=None) for url, name in rows]


def alert_cap(products_seen: int, settings: Settings) -> int:
    """How many alerts one pass may deliver before it looks like a fault.

    Scaled by the work actually done: an anomaly is a RATE, not a count, so a
    pass reading five times as much may legitimately report more. The
    configured absolute value stays as a floor so a tiny pass cannot spam.
    """
    ceiling = getattr(settings, "alert_rate_ceiling", 0.0)
    return max(settings.max_alerts_per_cycle, int(products_seen * ceiling))


async def run(
    settings: Settings,
    limit: int | None = None,
    dry_run: bool = False,
    notify: bool | None = None,
) -> tuple[ScanResult, dict]:
    """Run one focus-reserved pass and optionally deliver bounded alerts."""
    send_alerts = settings.proshop_notify if notify is None else notify
    outcome = ScanResult()
    rejected = RejectedDrops()
    database = Database(url=settings.database_url)
    fetcher = CurlCffiFetcher(
        settings.request_delay_s,
        max_requests=MAX_CONTACTS_PER_CYCLE,
    )
    pending: list[PendingAlert] = []
    try:
        listing_catalog = Catalog(database.connection, LISTING_STORE)
        product_catalog = Catalog(database.connection, PRODUCT_STORE)
        sender = ProshopSender(settings)

        if should_refresh_catalog(
            database.get_state(REFRESH_STATE_KEY),
            dry_run,
            settings.category_refresh_hours,
        ) and not refresh_backoff_until(database.get_state(REFUSED_REFRESH_STATE_KEY)):
            added, complete = await refresh_categories(fetcher, listing_catalog, database.connection)
            outcome.categories_added = added
            if complete:
                database.set_state(REFRESH_STATE_KEY, datetime.now().isoformat())
                database.set_state(REFUSED_REFRESH_STATE_KEY, "")
            elif fetcher.shop_refusal:
                # Record the refusal itself, not just the missing success, so
                # the next cycle can space the retry instead of knocking again.
                database.set_state(REFUSED_REFRESH_STATE_KEY, datetime.now().isoformat())
            logger.info(
                "proshop_categories_refreshed",
                added=added,
                total=listing_catalog.count(),
                complete=complete,
            )

        pass_limit = limit or settings.pages_per_pass
        if not dry_run:
            # Cap the HOUR, not just the pass. A per-pass budget is safe on
            # average and still breaches whenever the backlog is deep enough
            # to fill it - which is how a healthy 80-page budget produced
            # 222 pages in one hour on 2026-09-21 and cost six hours of
            # refusals. An explicit --limit is a deliberate operator choice
            # and is capped too, since the shop meters us either way.
            try:
                history = database.recent_cycles(STORE, limit=12)
            except Exception:  # noqa: BLE001 - fail open, never block the lane
                history = []
            granted = pages_allowed_now(history, pass_limit)
            if granted < pass_limit:
                logger.info(
                    "hourly_budget_applied",
                    requested=pass_limit,
                    granted=granted,
                    cap=HOURLY_PAGE_CAP,
                )
            pass_limit = granted
        remaining_contacts = getattr(fetcher, "remaining_requests", None)
        if remaining_contacts is not None and remaining_contacts < pass_limit:
            logger.info(
                "contact_budget_applied",
                requested=pass_limit,
                granted=remaining_contacts,
                contacts_used=getattr(fetcher, "requests_made", 0),
                cap=MAX_CONTACTS_PER_CYCLE,
            )
            pass_limit = remaining_contacts
        pages = (
            _dry_run_pages(pass_limit)
            if dry_run
            else due_for_pass(database.connection, LISTING_STORE, pass_limit, settings.focus_share)
        )
        seen_ids: set[str] = set()

        for page in pages:
            status, html = await fetcher(page.url)
            if getattr(fetcher, "budget_exhausted", False):
                logger.info(
                    "contact_budget_exhausted",
                    contacts_used=getattr(fetcher, "requests_made", 0),
                    cap=MAX_CONTACTS_PER_CYCLE,
                )
                break
            if fetcher.shop_refusal:
                outcome.shop_refusal = True
                # Report the request that was actually refused. A latched
                # refusal short-circuits without issuing a request, so
                # `page.url` here is merely the next queue entry and blaming
                # it sends the next reader after an innocent page.
                logger.warning(
                    "store_refused",
                    status=status,
                    url=fetcher.refused_url or page.url,
                    reached_listing_loop=fetcher.refused_url == page.url,
                )
                # A refusal anywhere in the pass means the address is
                # penalised, so the next cycle's refresh must back off too -
                # otherwise a refusal met in the listing loop still leaves 12
                # root requests queued up for 17 minutes' time.
                if not dry_run:
                    database.set_state(
                        REFUSED_REFRESH_STATE_KEY, datetime.now().isoformat()
                    )
                break
            if status == 0 or status >= 500 or not html:
                outcome.listing_fetch_errors += 1
                if not dry_run:
                    listing_catalog.save_record(Page(url=page.url, name=page.name, price=None))
                continue

            products = parse_listing(html, page.url)
            if not products:
                outcome.empty_listings += 1
                if not dry_run:
                    listing_catalog.save_record(Page(url=page.url, name=page.name, price=None))
                continue

            outcome.listing_pages += 1
            if is_focus(page.url):
                outcome.focus_pages += 1

            if not dry_run:
                added, removed = sync_listing_page_range(
                    listing_catalog,
                    database.connection,
                    Page(url=page.url, name=page.name, price=None),
                    page_count=listing_page_count(html),
                    cap=settings.max_listing_pages,
                )
                outcome.listing_pages_added += added
                outcome.listing_pages_removed += removed

            for product in products:
                if product.product_id in seen_ids:
                    outcome.duplicate_products += 1
                    continue
                seen_ids.add(product.product_id)
                outcome.products_seen += 1
                previous = _previous_price(database.connection, product.url)
                canonical = canonical_category(product.category, product.name, product.url)
                ledger_key = product_key(product.name, product.brand, product.mpn)
                if product.mpn:
                    outcome.with_code += 1
                source = source_for(product.is_outlet)
                condition = condition_for(product.is_outlet)
                observation_key = f"proshop:{product.product_id}"

                if not dry_run:
                    # Catalog state is recorded either way: the address was
                    # visited and must not come straight back to the queue.
                    product_catalog.append([product.url])
                    product_catalog.save_record(
                        Page(
                            url=product.url,
                            key=observation_key,
                            name=product.name,
                            price=product.price,
                        )
                    )

                if not product.purchasable:
                    # An amount nobody can order is not a price any shop may be
                    # scored against. Measured 2026-09-06: HP 669324-B21 sat at
                    # 1,015 PLN while unbuyable and was reused 8 times as a
                    # cross-shop reference.
                    #
                    # Only this product's own lane is withdrawn -- outlet and
                    # retail are separate offers for the same part -- and
                    # historia_cen keeps every observation, because what a shop
                    # asked is still evidence about the market.
                    outcome.unavailable += 1
                    if not dry_run:
                        database.prices.retire_current(
                            ledger_key,
                            SELLER,
                            source,
                            name=product.name,
                            url=product.url,
                            mpn=product.mpn,
                        )
                    continue

                if not dry_run:
                    database.prices.save_record(
                        ledger_key,
                        SELLER,
                        source,
                        product.price,
                        name=product.name,
                        url=product.url,
                        mpn=product.mpn,
                    )
                    database.events.save_record(
                        key=observation_key,
                        source=SOURCE_OWN,
                        name=product.name[:120],
                        price=product.price,
                        url=product.url,
                        channel=STORE,
                        product_identity_key=ledger_key,
                    )

                reference = reference_from_ledger(
                    database.connection,
                    key=ledger_key,
                    name=product.name,
                    seller=SELLER,
                    price=product.price,
                    condition=condition,
                )
                reference_price = None
                reference_kind = ""
                conditions: list[str] = []
                ledger_reference: float | None = None
                ledger_kind = ""
                ledger_condition = ""
                if reference and reference.price > product.price:
                    ledger_reference = reference.price
                    ledger_kind = reference.basis
                    ledger_condition = (
                        f"reference: {reference.seller} {reference.price:.2f} PLN"
                        f" ({reference.condition}"
                        + (f", {reference.age_hours:.0f}h old" if reference.age_hours >= 1 else "")
                        + ")"
                    )
                # Tile original_price (rendered .presales-price) is the
                # only market reference available for a first sighting. A price
                # that is not materially below either the ledger or the tile is
                # scored as  rather than as ;
                # the bucket exists for "nothing to compare against", not for
                # "compared and not worth reporting".
                tile_reference: float | None = (
                    product.original_price
                    if product.original_price and product.original_price > product.price
                    else None
                )
                candidates: list[tuple[float, str, str]] = []
                if ledger_reference is not None:
                    candidates.append((ledger_reference, ledger_kind, ledger_condition))
                if tile_reference is not None:
                    candidates.append((tile_reference, "tile-original-price", "reference: tile original price"))
                if candidates:
                    reference_price, reference_kind, cond = min(candidates, key=lambda c: c[0])
                    conditions.append(cond)
                    if reference_kind.startswith("ledger") or reference_kind in ("code", "ledger-code"):
                        outcome.from_ledger += 1
                    elif reference_kind == "tile-original-price":
                        outcome.from_tile += 1
                else:
                    if previous is None:
                        outcome.without_reference = getattr(outcome, "without_reference", 0) + 1  # type: ignore[attr-defined]
                    elif product.price > previous + 0.01:
                        outcome.increased += 1
                    elif product.price >= previous - 0.01:
                        outcome.unchanged += 1
                    else:
                        # No market price exists, so scoring against our own
                        # past observation is not a price drop to report. The
                        # Morele outlet ran for weeks as  while the
                        # listing held real deals precisely because this
                        # fallback treated "changed vs self" as "good vs
                        # market".
                        outcome.without_reference = getattr(outcome, "without_reference", 0) + 1  # type: ignore[attr-defined]
                    continue

                outcome.drops += 1
                event_key = f"{observation_key}:{int(round(product.price * 100))}"
                deal = Deal(
                    key=event_key,
                    name=product.name,
                    price=product.price,
                    reference_price=reference_price,
                    url=product.url,
                    store=STORE,
                    source=SOURCE_OWN,
                    conditions=conditions,
                    photo_url=product.image_url,
                    product_identity_key=ledger_key,
                    reference_kind=reference_kind,
                    category=canonical,
                    # Outlet stock is opened, scratched or ex-display, so it is
                    # judged against the used-stock bar. Derived above from
                    # is_outlet and already used to pick the reference lane;
                    # hard-coding "new" here scored outlet units against the
                    # new-stock threshold.
                    condition=condition,
                )
                reason = report_reason(deal)
                if not reason:
                    rejected.record(deal)
                    continue
                if not dry_run and database.events.is_reported(event_key):
                    outcome.already_reported += 1
                    continue
                pending.append(
                    PendingAlert(
                        deal=deal,
                        canonical=canonical,
                        producer=product.brand,
                        image_url=product.image_url,
                        reason=reason,
                        event_key=event_key,
                    )
                )

        outcome.qualified = len(pending)
        cap = alert_cap(outcome.products_seen, settings)
        if send_alerts and not dry_run and len(pending) > cap:
            outcome.circuit_breaker = True
            logger.error(
                "proshop_alert_circuit_breaker",
                qualified=len(pending),
                cap=cap,
                products_seen=outcome.products_seen,
            )
        elif send_alerts and not dry_run:
            for alert in pending:
                if await sender(alert):
                    database.events.mark_reported(alert.event_key)
                    outcome.reported += 1
        if not dry_run:
            database.record_cycle(
                scanner_name=STORE,
                checked_count=outcome.products_seen,
                available_count=outcome.products_seen - outcome.unavailable,
                report_count=outcome.reported,
                extra={**asdict(outcome), **rejected.as_dict(), "notify": send_alerts},
            )
        logger.info("proshop_scan", **asdict(outcome), **rejected.as_dict(), notify=send_alerts)
        return outcome, listing_catalog.statistics()
    finally:
        fetcher.close()
        database.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Proshop Poland electronics scanner")
    parser.add_argument("--limit", "--ile", dest="limit", type=int, default=None)
    parser.add_argument("--dry-run", "--probny", dest="dry_run", action="store_true")
    parser.add_argument("--notify", dest="notify", action="store_true", default=None)
    arguments = parser.parse_args()

    settings = get_settings()
    if not settings.proshop_enabled and not arguments.dry_run:
        print("proshop_enabled=false - scanner disabled, doing nothing")
        return

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    result, statistics = asyncio.run(
        run(settings, limit=arguments.limit, dry_run=arguments.dry_run, notify=arguments.notify)
    )
    print(
        f"listing_pages={result.listing_pages} focus_pages={result.focus_pages} "
        f"products={result.products_seen} with_code={result.with_code} "
        f"fetch_errors={result.listing_fetch_errors} empty={result.empty_listings} "
        f"drops={result.drops} from_ledger={result.from_ledger} "
        f"qualified={result.qualified} reported={result.reported}"
    )
    print(
        f"catalog: {statistics['wszystkie']} listing pages, "
        f"{statistics['nietkniete']} not checked yet"
    )
    if result.shop_refusal:
        raise SystemExit(STORE_REFUSAL_EXIT_CODE)


if __name__ == "__main__":
    main()
