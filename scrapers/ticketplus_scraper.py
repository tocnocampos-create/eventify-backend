"""TicketPlus Chile scraper — fetches events from category taxon listing pages.

TicketPlus (ticketplus.cl) is built on Spree Commerce (Ruby on Rails).
Category listing pages use the /taxons/ path; pagination is ?page=N.
Product detail pages carry event-specific metadata in Spree "properties"
(a table of key/value rows: Fecha, Hora, Lugar, Dirección, etc.).

Scraped categories:
    /taxons/teatros   → category hint "Teatro"
    /taxons/musica    → category hint "Música"
    /taxons/fiestas   → left to classifier (Vida Nocturna / Arte / etc.)
    /taxons/familiar  → category hint "Teatro", kids_friendly=True

Only Región Metropolitana events are kept (SANTIAGO_TOKENS filter applied
to venue_name + location fields scraped from the detail page).

source_url is the canonical product detail URL — stable across re-scrapes.

Run:
    python scrapers/ticketplus_scraper.py --dry-run
    python scrapers/ticketplus_scraper.py --dry-run --verbose
    python scrapers/ticketplus_scraper.py --category teatros --dry-run
    python scrapers/ticketplus_scraper.py --debug --category musica
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper
from scrapers.puntoticket_scraper import (
    SANTIAGO_TOKENS,
    _MONTHS_ES,
    _is_santiago,
    _parse_date_es,
    _parse_price,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://ticketplus.cl"

# Category taxon pages to scrape.
# Each tuple: (slug, category_hint, kids_friendly, lock_category)
# lock_category=True means _locked_category sentinel is set (classifier cannot override).
CATEGORY_CONFIG: list[tuple[str, str | None, bool, bool]] = [
    ("teatros",  "Teatro",  False, True),
    ("musica",   "Música",  False, True),
    ("fiestas",  None,      False, False),   # classifier decides freely
    ("familiar", "Teatro",  True,  True),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 2  # seconds between requests

# Spree property key aliases (all lowercased) for each field we care about.
_PROP_DATE  = {"fecha", "date", "fecha del evento", "fecha evento", "dia", "día"}
_PROP_TIME  = {"hora", "hora de inicio", "time", "horario", "hora inicio"}
_PROP_VENUE = {
    "lugar", "recinto", "venue", "sala", "teatro", "lugar del evento",
    "dirección del evento", "location",
}
_PROP_ADDR  = {"dirección", "direccion", "address", "domicilio", "ubicación", "ubicacion"}


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _absolute_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE_URL + href if href.startswith("/") else href


def _parse_time_str(raw: str) -> str | None:
    """Extract HH:MM from any time-like string."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", raw.strip())
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _read_spree_properties(soup: BeautifulSoup) -> dict[str, str]:
    """Extract Spree product properties table into a lowercase-key dict.

    Spree renders properties as:
        <table class="product-properties">
          <tr><th class="property-name">Fecha</th>
              <td class="property-value">21 de marzo 2026</td></tr>
          …
        </table>

    Falls back to <dl> definition lists and generic <li> "Key: Value" patterns.
    """
    props: dict[str, str] = {}

    # Primary: table.product-properties or table[id*=product-properties]
    for table in soup.find_all("table", class_=re.compile(r"prop(?:ert(?:ies|y))?", re.I)):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True).lower().rstrip(":")
                val = cells[1].get_text(strip=True)
                if key and val:
                    props[key] = val

    # Fallback 1: <dl> definition lists
    if not props:
        for dl in soup.find_all("dl"):
            terms = dl.find_all("dt")
            defs  = dl.find_all("dd")
            for dt, dd in zip(terms, defs):
                key = dt.get_text(strip=True).lower().rstrip(":")
                val = dd.get_text(strip=True)
                if key and val:
                    props[key] = val

    # Fallback 2: spans/divs labelled with property-like classes
    if not props:
        for el in soup.find_all(class_=re.compile(r"property|prop-", re.I)):
            text = el.get_text(" ", strip=True)
            if ":" in text:
                k, _, v = text.partition(":")
                key = k.strip().lower()
                val = v.strip()
                if key and val:
                    props[key] = val

    return props


def _prop_get(props: dict[str, str], aliases: set[str]) -> str | None:
    """Return the first value in props whose key matches any alias."""
    for key, val in props.items():
        if key in aliases:
            return val
    return None


# ── Main scraper class ────────────────────────────────────────────────────────

class TicketPlusScraper(BaseScraper):
    """Scrapes event listings from TicketPlus Chile category (taxon) pages."""

    name = "ticketplus"

    def __init__(
        self,
        max_pages:  int  = 10,
        max_events: int  = 0,
        debug:      bool = False,
    ) -> None:
        super().__init__()
        self.max_pages  = max_pages
        self.max_events = max_events
        self.debug      = debug
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get_soup(self, url: str) -> BeautifulSoup | None:
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.error("[ticketplus] GET %s failed: %s", url, exc)
            return None

    # ── Card detection ────────────────────────────────────────────────────────

    def _find_cards(self, soup: BeautifulSoup) -> list[Any]:
        """Return product card elements from a Spree listing page.

        Spree listing page DOM variants (in priority order):
          1. <div id="products"> wrapping <div class="product">
          2. <ul class="products-list"> / <ul class="product-list"> with <li>
          3. <article class="product-card"> or <div class="product-card">
          4. Generic: any block element with a child <a> pointing to /products/
        """
        # 1. Spree default: id="products" container
        container = soup.find(id="products")
        if container:
            cards = container.find_all(
                ["div", "article", "li"],
                class_=re.compile(r"product(?!-price|-title|-name|-prop)", re.I),
            )
            if cards:
                return cards

        # 2. ul.products-list / ul.product-list
        for ul_cls in (
            re.compile(r"products?[_-](?:list|grid|container)", re.I),
        ):
            ul = soup.find(["ul", "div"], class_=ul_cls)
            if ul:
                cards = ul.find_all(["li", "div", "article"])
                if cards:
                    return cards

        # 3. Any element with class containing "product-card" or "event-card"
        cards = soup.find_all(
            ["div", "article", "li"],
            class_=re.compile(r"(?:product|event)[_-]card|card[_-](?:product|event)", re.I),
        )
        if cards:
            return cards

        # 4. Last resort: collect block ancestors of /products/ links
        cards = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.search(r"/products?/", href, re.I):
                continue
            if href in seen:
                continue
            seen.add(href)
            parent = a
            for _ in range(5):
                p = parent.parent
                if p and p.name in ("div", "article", "li", "section"):
                    parent = p
                else:
                    break
            cards.append(parent)

        return cards

    def _parse_card(self, card: Any) -> dict[str, Any] | None:
        """Extract stub event fields from a listing-page card.

        Returns a partial dict; detail page fills in date, time, venue, price.
        """
        event: dict[str, Any] = {}

        # ── URL ───────────────────────────────────────────────────────────────
        link = card.find("a", href=re.compile(r"/products?/", re.I))
        if not link:
            link = card.find("a", href=True)
        if not link:
            return None
        href = link.get("href", "")
        if not href or href in ("#", "javascript:void(0)"):
            return None
        full_url = _absolute_url(href)
        event["url"] = full_url
        event["source_url"] = full_url

        # ── Title ─────────────────────────────────────────────────────────────
        for tag in ("h1", "h2", "h3", "h4"):
            el = card.find(tag)
            if el:
                text = el.get_text(strip=True)
                if text:
                    event["name"] = text
                    break
        if not event.get("name"):
            # Spree sometimes puts the name in a .product-name / .product-title span
            for cls in (
                re.compile(r"product[_-](?:name|title)", re.I),
                re.compile(r"event[_-](?:name|title)", re.I),
                re.compile(r"item[_-]name", re.I),
            ):
                el = card.find(class_=cls)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        event["name"] = text
                        break
        if not event.get("name"):
            return None

        # ── Image ─────────────────────────────────────────────────────────────
        img = card.find("img")
        if img:
            src = (
                img.get("data-src")
                or img.get("src")
                or img.get("data-lazy-src")
                or img.get("data-original")
            )
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http"):
                    event["image_url"] = src

        # ── Price (listing page only — detail page overrides if richer) ───────
        price_el = card.find(
            class_=re.compile(r"price|precio|valor|cost", re.I)
        )
        if price_el:
            pr = _parse_price(price_el.get_text(strip=True))
            if pr:
                event["price_range"] = pr

        # ── Date on card (sometimes shown on listing) ─────────────────────────
        date_el = card.find(class_=re.compile(r"fecha|date|when|dia|schedule", re.I))
        if not date_el:
            date_el = card.find("time")
        if date_el:
            raw_date = date_el.get("datetime") or date_el.get_text(strip=True)
            iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
            first_part = iso_m.group(1) if iso_m else raw_date.split(" - ")[0].split(" al ")[0].strip()
            parsed = _parse_date_es(first_part)
            if parsed:
                event["date"] = parsed
                t_m = re.search(r"(\d{1,2}):(\d{2})", raw_date)
                if t_m:
                    event["time_start"] = f"{int(t_m.group(1)):02d}:{t_m.group(2)}"

        return event

    # ── Detail page ───────────────────────────────────────────────────────────

    def fetch_event_detail(self, url: str) -> dict[str, Any]:
        """Fetch a product detail page and extract date, time, venue, price, description.

        Spree product pages carry event metadata in a properties table.
        Falls back to JSON-LD structured data, then meta tags.
        """
        time.sleep(REQUEST_DELAY)
        soup = self._get_soup(url)
        if soup is None:
            return {}

        result: dict[str, Any] = {}

        # ── 1. Spree product properties table ─────────────────────────────────
        props = _read_spree_properties(soup)

        raw_date = _prop_get(props, _PROP_DATE)
        if raw_date:
            iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
            first_part = iso_m.group(1) if iso_m else raw_date.split(" - ")[0].split(" al ")[0].strip()
            parsed = _parse_date_es(first_part)
            if parsed:
                result["date"] = parsed

        raw_time = _prop_get(props, _PROP_TIME)
        if raw_time:
            t = _parse_time_str(raw_time)
            if t:
                result["time_start"] = t

        venue_raw = _prop_get(props, _PROP_VENUE)
        if venue_raw:
            result["venue_name"] = venue_raw.strip()

        addr_raw = _prop_get(props, _PROP_ADDR)
        if addr_raw:
            result["address"] = addr_raw.strip()

        # ── 2. JSON-LD structured data ─────────────────────────────────────────
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") not in (
                        "Event", "MusicEvent", "TheaterEvent",
                        "SportsEvent", "ComedyEvent", "DanceEvent",
                    ):
                        continue
                    # Date + time
                    if "date" not in result:
                        start = item.get("startDate") or ""
                        iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", start)
                        if iso_m:
                            result["date"] = iso_m.group(1)
                        if "time_start" not in result:
                            t_m = re.search(r"T(\d{2}):(\d{2})", start)
                            if t_m:
                                result["time_start"] = f"{t_m.group(1)}:{t_m.group(2)}"
                    # Venue
                    if "venue_name" not in result:
                        loc = item.get("location") or {}
                        if isinstance(loc, dict):
                            vname = loc.get("name") or ""
                            if vname:
                                result["venue_name"] = vname.strip()
                            addr = loc.get("address") or {}
                            if isinstance(addr, dict):
                                street = addr.get("streetAddress", "")
                                locality = addr.get("addressLocality", "")
                                result["address"] = f"{street} {locality}".strip()
                    # Price
                    if "price_range" not in result:
                        offers = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0]
                        low = offers.get("price") or offers.get("lowPrice")
                        high = offers.get("highPrice") or low
                        if low is not None:
                            try:
                                result["price_range"] = [float(low), float(high)]
                            except (TypeError, ValueError):
                                pass
                    # Description
                    if "description" not in result:
                        desc = item.get("description") or ""
                        if len(desc.strip()) > 10:
                            result["description"] = desc.strip()[:1500]
                    # Image
                    if "image_url" not in result:
                        img_ld = item.get("image")
                        if isinstance(img_ld, str) and img_ld.startswith("http"):
                            result["image_url"] = img_ld
                        elif isinstance(img_ld, list) and img_ld:
                            result["image_url"] = img_ld[0]
                    break
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        # ── 3. Spree price widget ─────────────────────────────────────────────
        if "price_range" not in result:
            for cls_pat in (
                re.compile(r"price[_-](?:selling|current|amount|value)", re.I),
                re.compile(r"selling[_-]price", re.I),
                re.compile(r"product[_-]price", re.I),
                re.compile(r"^price$", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    pr = _parse_price(el.get_text(strip=True))
                    if pr:
                        result["price_range"] = pr
                        break

        # ── 4. Description: Spree product-description div then meta ──────────
        if "description" not in result:
            for cls_pat in (
                re.compile(r"product[_-]description|event[_-]description", re.I),
                re.compile(r"product[_-]detail", re.I),
                re.compile(r"description[_-]content", re.I),
                re.compile(r"^description$", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    txt = el.get_text(" ", strip=True)
                    if len(txt) > 30:
                        result["description"] = txt[:1500]
                        break

        if "description" not in result:
            meta = soup.find("meta", {"name": "description"})
            if meta and meta.get("content"):
                result["description"] = meta["content"][:1500]

        # ── 5. Image fallback: og:image ───────────────────────────────────────
        if "image_url" not in result:
            og = soup.find("meta", {"property": "og:image"})
            if og and og.get("content", "").startswith("http"):
                result["image_url"] = og["content"]

        return result

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page_url(self, soup: BeautifulSoup, current_url: str) -> str | None:
        """Return the next listing page URL, or None if on the last page.

        Spree paginates via ?page=N (Kaminari gem).
        """
        # rel="next" is the most reliable signal
        next_link = soup.find("a", rel=lambda v: v and "next" in v)
        if next_link and next_link.get("href"):
            return _absolute_url(next_link["href"])

        # Class-based "Next" / "Siguiente" link
        next_link = (
            soup.find("a", class_=re.compile(r"next|siguiente", re.I))
            or soup.find("a", string=re.compile(r"siguiente|next|›|»", re.I))
        )
        if next_link and next_link.get("href"):
            href = next_link["href"]
            if href not in ("#", "javascript:void(0)"):
                return _absolute_url(href)

        return None

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch Región Metropolitana events from all configured taxon pages.

        For each category:
          1. Iterate listing pages (pagination)
          2. Parse each event card (stub: name, image, listing-page price/date)
          3. Fetch the detail page (date, time, venue, description, price)
          4. Apply Santiago filter on venue + address
          5. Apply category hints / _locked_category sentinel

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        all_events: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for slug, cat_hint, kids, lock in CATEGORY_CONFIG:
            cat_url = f"{BASE_URL}/taxons/{slug}"
            logger.info("[ticketplus] Scraping category %r — %s", slug, cat_url)

            url: str | None = cat_url
            page = 1

            while url and page <= self.max_pages:
                logger.info("[ticketplus] [%s] page %d: %s", slug, page, url)
                soup = self._get_soup(url)
                if soup is None:
                    break

                if self.debug and page == 1:
                    self._print_debug(soup, slug)
                    break

                cards = self._find_cards(soup)
                if not cards:
                    logger.warning(
                        "[ticketplus] [%s] No cards on page %d — stopping category",
                        slug, page,
                    )
                    break

                page_new = 0
                for card in cards:
                    ev = self._parse_card(card)
                    if ev is None:
                        continue

                    detail_url = ev.get("url", "")
                    if not detail_url or detail_url in seen_urls:
                        continue

                    # Fetch detail page for date, time, venue, description, price
                    detail = self.fetch_event_detail(detail_url)
                    for key, val in detail.items():
                        ev.setdefault(key, val)

                    # Mandatory: skip if no date could be extracted at all
                    if not ev.get("date"):
                        logger.debug(
                            "[ticketplus] No date for %r — skipping", ev.get("name")
                        )
                        continue

                    # Región Metropolitana filter
                    location_hint = (
                        ev.get("venue_name", "")
                        + " "
                        + ev.get("address", "")
                        + " "
                        + ev.get("name", "")
                    )
                    if not _is_santiago(location_hint):
                        logger.debug(
                            "[ticketplus] Non-Santiago event %r — skipping", ev.get("name")
                        )
                        continue

                    seen_urls.add(detail_url)

                    # Apply category hints
                    if cat_hint:
                        ev.setdefault("category", cat_hint)
                        if lock:
                            ev["_locked_category"] = cat_hint
                    if kids:
                        ev["kids_friendly"] = True

                    all_events.append(ev)
                    page_new += 1

                    if self.max_events and len(all_events) >= self.max_events:
                        logger.info(
                            "[ticketplus] Reached max_events=%d", self.max_events
                        )
                        break

                logger.info(
                    "[ticketplus] [%s] page %d: %d new RM events (total: %d)",
                    slug, page, page_new, len(all_events),
                )

                if self.max_events and len(all_events) >= self.max_events:
                    break

                next_url = self._next_page_url(soup, url)
                if not next_url or next_url == url:
                    break
                url = next_url
                page += 1
                time.sleep(REQUEST_DELAY)

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[ticketplus] Total events collected: %d", len(all_events))
        return all_events

    # ── Debug helper ──────────────────────────────────────────────────────────

    def _print_debug(self, soup: BeautifulSoup, category: str = "") -> None:
        """Print structural diagnostics for the first listing page."""
        print("\n" + "=" * 70)
        print(f"DEBUG — TicketPlusScraper  [{category}]")
        print("=" * 70)

        title = soup.find("title")
        print(f"\nPage <title>: {title.get_text(strip=True) if title else '(none)'}")

        from collections import Counter
        class_counts: Counter = Counter()
        for el in soup.find_all(["div", "article", "li", "ul"]):
            for cls in el.get("class", []):
                class_counts[cls] += 1
        print("\nTop-20 class names:")
        for cls, cnt in class_counts.most_common(20):
            print(f"  {cnt:4d}×  .{cls}")

        cards = self._find_cards(soup)
        print(f"\nCards found by _find_cards(): {len(cards)}")
        if cards:
            print("\n── First card raw HTML (first 2000 chars) ─────────────────────")
            raw = str(cards[0])
            print(raw[:2000])
            print("\n── Parsed stub from first card ─────────────────────────────────")
            parsed = self._parse_card(cards[0])
            if parsed:
                for k, v in parsed.items():
                    print(f"  {k}: {v!r}")
            else:
                print("  (parse returned None)")
        else:
            body = soup.find("body")
            if body:
                print("\nNo cards found. First 3000 chars of <body>:")
                print(str(body)[:3000])
        print("\n" + "=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="TicketPlus Chile scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print sample events — no DB writes",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print first-page HTML structure and exit (no detail fetches)",
    )
    parser.add_argument(
        "--category",
        choices=[s for s, *_ in CATEGORY_CONFIG],
        help="Scrape only this category (default: all 4)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=10,
        help="Maximum listing pages per category",
    )
    parser.add_argument(
        "--max-events", type=int, default=0,
        help="Stop after this many events (0 = unlimited)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print price and description for each sample event",
    )
    args = parser.parse_args()

    # Filter to one category if requested
    if args.category:
        import scrapers.ticketplus_scraper as _self
        _self.CATEGORY_CONFIG = [
            c for c in CATEGORY_CONFIG if c[0] == args.category
        ]

    scraper = TicketPlusScraper(
        max_pages=args.max_pages,
        max_events=args.max_events,
        debug=args.debug,
    )
    events = scraper.fetch_events()

    if args.dry_run or args.debug:
        print(f"\n── TicketPlus dry-run: {len(events)} RM events ────────────────")
        for ev in events[:10]:
            print(
                f"\n  name      : {ev.get('name')!r}\n"
                f"  date      : {ev.get('date')}\n"
                f"  time_start: {ev.get('time_start')}\n"
                f"  venue_name: {ev.get('venue_name')!r}\n"
                f"  source_url: {ev.get('source_url')}"
            )
            if args.verbose:
                print(
                    f"  price     : {ev.get('price_range')}\n"
                    f"  image_url : {ev.get('image_url')}\n"
                    f"  desc      : {str(ev.get('description',''))[:120]!r}"
                )
    else:
        from scrapers.base_scraper import make_scraper_session
        from scrapers import classifier, enricher, deduplicator
        from datetime import datetime, timezone

        engine, db = make_scraper_session()
        now = datetime.now(timezone.utc)
        stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}

        for ev in events:
            try:
                ev = classifier.classify(ev)
                ev = enricher.enrich(ev, db)
                ev.setdefault("scraped_at", now)
                ev.setdefault("is_verified", False)
                result = deduplicator.save_or_update(ev, db)
                stats[result] += 1
            except Exception as exc:
                logger.warning("Failed to save %r: %s", ev.get("name"), exc)
                db.rollback()
                stats["failed"] += 1

        db.commit()
        db.close()
        engine.dispose()

        print(
            f"\nDone — created={stats['created']}  updated={stats['updated']}  "
            f"skipped={stats['skipped']}  failed={stats['failed']}"
        )
