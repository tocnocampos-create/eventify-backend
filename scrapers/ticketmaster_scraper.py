"""Ticketmaster Chile scraper — fetches events from static HTML listing pages.

Ticketmaster Chile (ticketmaster.cl) serves server-rendered HTML.
Events appear on the homepage and on paginated listing pages.
Each event card links to a detail page that carries full metadata
(date, venue, description) often also embedded as JSON-LD.

Listing entry points scraped (in order):
    https://www.ticketmaster.cl/               (homepage)
    https://www.ticketmaster.cl/listing        (main catalogue, if present)

Pagination follows rel="next" / class="next" / ?page=N patterns.

Only Santiago / Región Metropolitana events are kept.

source_url: canonical event detail URL (stable across re-scrapes).

Run:
    python scrapers/ticketmaster_scraper.py --dry-run
    python scrapers/ticketmaster_scraper.py --dry-run --verbose
    python scrapers/ticketmaster_scraper.py --debug
    python scrapers/ticketmaster_scraper.py --max-pages 3 --dry-run
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
from urllib.parse import urljoin

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

BASE_URL = "https://www.ticketmaster.cl"

# Listing pages to seed the scraper.
# The scraper will follow pagination from each seed URL.
LISTING_SEEDS: list[str] = [
    f"{BASE_URL}/",
    f"{BASE_URL}/es/tmus/home",
    f"{BASE_URL}/listing",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL,
}

REQUEST_DELAY = 2  # seconds between requests


# ── URL helpers ───────────────────────────────────────────────────────────────

def _absolute_url(href: str, page_url: str = BASE_URL) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return urljoin(page_url, href)


def _is_event_url(href: str) -> bool:
    """Return True if href looks like a Ticketmaster Chile event detail page.

    Known Ticketmaster CL URL shapes (in order of confidence):
      /evento/some-event-name-tickets-12345
      /event/12345
      /ev/12345
      /boletos/some-event
      /tickets/12345/
    """
    return bool(
        re.search(
            r"/(evento|event|ev|boletos|tickets|comprar)/",
            href,
            re.IGNORECASE,
        )
    )


# ── Date / time helpers ───────────────────────────────────────────────────────

def _parse_time_str(raw: str) -> str | None:
    """Extract HH:MM from any time-like string."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", raw.strip())
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _parse_date_safe(raw: str) -> str | None:
    """Parse a date field safely, handling ISO datetime attributes."""
    if not raw:
        return None
    raw = raw.strip()
    # ISO datetime attribute: "2026-04-23" or "2026-04-23T20:00:00-03:00"
    iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if iso_m:
        return iso_m.group(1)
    # Prose date range — take first part only
    first_part = raw.split(" - ")[0].split(" al ")[0].strip()
    return _parse_date_es(first_part)


# ── Main scraper class ────────────────────────────────────────────────────────

class TicketmasterScraper(BaseScraper):
    """Scrapes events from Ticketmaster Chile static HTML listing pages."""

    name = "ticketmaster"

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
            logger.error("[ticketmaster] GET %s failed: %s", url, exc)
            return None

    # ── Card detection ────────────────────────────────────────────────────────

    def _find_cards(self, soup: BeautifulSoup) -> list[Any]:
        """Return event card elements from a listing page.

        Ticketmaster Chile HTML variants tried (highest confidence first):

        1. Elements with class matching "event-card", "event-tile",
           "event-listing-item", "tm-event", "event-item" — their
           own component names from past site versions.
        2. <article> elements that contain an event-detail link.
        3. <li> elements inside an events list container.
        4. Last resort: block ancestors of all event-detail <a> hrefs.
        """
        # 1. Named card components — including current TM Chile layout (.grid_element)
        for cls_pat in (
            re.compile(r"^grid[_-]element$", re.I),               # ticketmaster.cl current
            re.compile(r"event[_-](?:card|tile|item|listing|row|block)", re.I),
            re.compile(r"tm[_-]event|eventCard|eventTile", re.I),
            re.compile(r"search[_-]result[_-](?:card|item)", re.I),
            re.compile(r"product[_-]card|card[_-]event", re.I),
        ):
            cards = soup.find_all(["div", "article", "li", "section"], class_=cls_pat)
            if cards:
                logger.debug("[ticketmaster] Found %d cards via %s", len(cards), cls_pat)
                return cards

        # 2. <article> elements that link to event pages
        articles = [
            a for a in soup.find_all("article")
            if a.find("a", href=_is_event_url)
        ]
        if articles:
            logger.debug("[ticketmaster] Found %d <article> cards", len(articles))
            return articles

        # 3. <ul>/<ol> event list containers
        for ul_cls in (
            re.compile(r"events?[_-](?:list|grid|container|results?)", re.I),
            re.compile(r"listing[_-](?:events?|results?)", re.I),
        ):
            ul = soup.find(["ul", "div", "section"], class_=ul_cls)
            if ul:
                items = ul.find_all(["li", "div", "article"])
                if items:
                    logger.debug(
                        "[ticketmaster] Found %d items inside %s", len(items), ul_cls
                    )
                    return items

        # 4. Last resort: block ancestors of all event-detail links
        cards: list[Any] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not _is_event_url(href):
                continue
            if href in seen:
                continue
            seen.add(href)
            parent = a
            for _ in range(6):
                p = parent.parent
                if p and p.name in ("div", "article", "li", "section"):
                    parent = p
                else:
                    break
            cards.append(parent)

        logger.debug("[ticketmaster] Last-resort: %d card ancestors", len(cards))
        return cards

    def _parse_card(self, card: Any) -> dict[str, Any] | None:
        """Extract stub event fields from a listing-page card.

        Ticketmaster CL card structure (historical variants):

          Variant A — component-style:
            <div class="event-card">
              <a href="/evento/...">
                <img src="...poster.jpg">
                <div class="event-name">Title</div>
                <div class="event-date">Sábado 23 de Abril</div>
                <div class="event-venue">Teatro Caupolicán · Santiago</div>
              </a>
            </div>

          Variant B — schema.org microdata:
            <article itemtype="https://schema.org/Event">
              <a itemprop="url" href="/evento/...">
              <span itemprop="name">Title</span>
              <time itemprop="startDate" datetime="2026-04-23T20:00">...</time>
              <span itemprop="location">Venue Name</span>
            </article>
        """
        event: dict[str, Any] = {}

        # ── URL ───────────────────────────────────────────────────────────────
        link = card.find("a", href=_is_event_url)
        if not link:
            link = card.find("a", href=True)
        if not link:
            return None
        href = link.get("href", "")
        if not href or href in ("#", "javascript:void(0)"):
            return None
        full_url = _absolute_url(href)
        # If still relative (no scheme), can't use — skip
        if not full_url.startswith("http"):
            return None
        event["url"] = full_url
        event["source_url"] = full_url

        # ── Title ─────────────────────────────────────────────────────────────
        # schema.org itemprop="name" is most reliable
        name_el = card.find(itemprop="name")
        if name_el:
            event["name"] = name_el.get_text(strip=True)

        if not event.get("name"):
            # ticketmaster.cl current layout: <div class="item_title">
            it = card.find(class_="item_title")
            if it:
                event["name"] = it.get_text(strip=True)

        if not event.get("name"):
            for tag in ("h1", "h2", "h3", "h4"):
                el = card.find(tag)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        event["name"] = text
                        break

        if not event.get("name"):
            for cls_pat in (
                re.compile(r"event[_-](?:name|title)|title[_-]event", re.I),
                re.compile(r"tm[_-]title|cardTitle|card[_-]title", re.I),
            ):
                el = card.find(class_=cls_pat)
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

        # ── Date (listing card may carry a truncated date) ────────────────────
        # schema.org itemprop="startDate"
        time_el = card.find("time", itemprop="startDate")
        if not time_el:
            time_el = card.find("time")
        if time_el:
            raw = time_el.get("datetime") or time_el.get_text(strip=True)
            d = _parse_date_safe(raw)
            if d:
                event["date"] = d
            t = _parse_time_str(time_el.get("datetime") or "")
            if t:
                event["time_start"] = t

        if not event.get("date"):
            # ticketmaster.cl current layout: <p>Mayo 2026</p> inside .details
            details_el = card.find(class_="details")
            if details_el:
                p = details_el.find("p")
                if p:
                    d = _parse_date_safe(p.get_text(strip=True))
                    if d:
                        event["date"] = d

        if not event.get("date"):
            for cls_pat in (
                re.compile(r"event[_-]date|date[_-]event|fecha|when", re.I),
                re.compile(r"tm[_-]date|cardDate", re.I),
            ):
                el = card.find(class_=cls_pat)
                if el:
                    d = _parse_date_safe(el.get("datetime") or el.get_text(strip=True))
                    if d:
                        event["date"] = d
                        break

        # ── Venue (listing card) ──────────────────────────────────────────────
        loc_el = card.find(itemprop="location")
        if loc_el:
            event["venue_name"] = loc_el.get_text(strip=True)

        if not event.get("venue_name"):
            # ticketmaster.cl current layout: <span class="grid-label">Venue name</span>
            gl = card.find("span", class_="grid-label")
            if gl:
                # grid-label sometimes contains nested <span class="hide"> with city
                # Keep only the top-level text (venue name, not city)
                text = gl.get_text(strip=True)
                if text:
                    event["venue_name"] = text

        if not event.get("venue_name"):
            for cls_pat in (
                re.compile(r"event[_-]venue|venue[_-]name|lugar|recinto", re.I),
                re.compile(r"tm[_-]venue|cardVenue|location[_-]name", re.I),
            ):
                el = card.find(class_=cls_pat)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        # Ticketmaster often formats as "Venue · City"
                        event["venue_name"] = text.split("·")[0].split("|")[0].strip()
                        break

        # ── Price (listing card) ──────────────────────────────────────────────
        for cls_pat in (
            re.compile(r"price|precio|valor|cost|ticket[_-]price", re.I),
        ):
            el = card.find(class_=cls_pat)
            if el:
                pr = _parse_price(el.get_text(strip=True))
                if pr:
                    event["price_range"] = pr
                    break

        return event

    # ── Detail page ───────────────────────────────────────────────────────────

    def fetch_event_detail(self, url: str) -> dict[str, Any]:
        """Fetch an event detail page and extract full metadata.

        Priority order:
          1. JSON-LD  (schema.org Event — most reliable, widely used by TM)
          2. schema.org microdata  (itemprop attributes)
          3. OpenGraph  (og:title / og:description / og:image)
          4. DOM class selectors  (site-specific fallback)
        """
        time.sleep(REQUEST_DELAY)
        soup = self._get_soup(url)
        if soup is None:
            return {}

        result: dict[str, Any] = {}

        # ── 1. JSON-LD structured data ─────────────────────────────────────────
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") not in (
                        "Event", "MusicEvent", "TheaterEvent",
                        "SportsEvent", "ComedyEvent", "DanceEvent",
                        "VisualArtsEvent", "Festival",
                    ):
                        continue

                    # Date + time
                    start = item.get("startDate") or ""
                    d = _parse_date_safe(start)
                    if d:
                        result["date"] = d
                    t = _parse_time_str(re.sub(r"^\d{4}-\d{2}-\d{2}", "", start))
                    if t:
                        result["time_start"] = t

                    # End time
                    end = item.get("endDate") or ""
                    t_end = _parse_time_str(re.sub(r"^\d{4}-\d{2}-\d{2}", "", end))
                    if t_end:
                        result["time_end"] = t_end

                    # Venue
                    loc = item.get("location") or {}
                    if isinstance(loc, dict):
                        vname = loc.get("name") or ""
                        if vname:
                            result["venue_name"] = vname.strip()
                        addr = loc.get("address") or {}
                        if isinstance(addr, dict):
                            parts = [
                                addr.get("streetAddress", ""),
                                addr.get("addressLocality", ""),
                                addr.get("addressRegion", ""),
                            ]
                            result["address"] = " ".join(p for p in parts if p).strip()
                    elif isinstance(loc, str):
                        result["venue_name"] = loc.strip()

                    # Price
                    offers = item.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0]
                    if isinstance(offers, dict):
                        low  = offers.get("price") or offers.get("lowPrice")
                        high = offers.get("highPrice") or low
                        if low is not None:
                            try:
                                result["price_range"] = [float(low), float(high)]
                            except (TypeError, ValueError):
                                pass

                    # Description
                    desc = (item.get("description") or "").strip()
                    if len(desc) > 10:
                        result["description"] = desc[:1500]

                    # Image
                    img_ld = item.get("image")
                    if isinstance(img_ld, str) and img_ld.startswith("http"):
                        result["image_url"] = img_ld
                    elif isinstance(img_ld, list) and img_ld:
                        result["image_url"] = str(img_ld[0])

                    # Performer (useful for classifier keywords)
                    performer = item.get("performer")
                    if isinstance(performer, dict):
                        performer = performer.get("name", "")
                    if isinstance(performer, str) and performer:
                        result["_performer"] = performer.strip()

                    break  # first Event item is enough
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        # ── 2. schema.org microdata fallback ──────────────────────────────────
        if "date" not in result:
            time_el = soup.find("time", itemprop="startDate")
            if not time_el:
                time_el = soup.find(itemprop="startDate")
            if time_el:
                raw = time_el.get("datetime") or time_el.get_text(strip=True)
                d = _parse_date_safe(raw)
                if d:
                    result["date"] = d
                t = _parse_time_str(raw)
                if t:
                    result["time_start"] = t

        if "venue_name" not in result:
            loc_el = soup.find(itemprop="location")
            if loc_el:
                name_el = loc_el.find(itemprop="name")
                result["venue_name"] = (
                    name_el.get_text(strip=True)
                    if name_el
                    else loc_el.get_text(strip=True)
                )

        if "description" not in result:
            desc_el = soup.find(itemprop="description")
            if desc_el:
                txt = desc_el.get_text(" ", strip=True)
                if len(txt) > 10:
                    result["description"] = txt[:1500]

        # ── 3. OpenGraph tags ─────────────────────────────────────────────────
        if "image_url" not in result:
            og_img = soup.find("meta", {"property": "og:image"})
            if og_img and og_img.get("content", "").startswith("http"):
                result["image_url"] = og_img["content"]

        if "description" not in result:
            og_desc = soup.find("meta", {"property": "og:description"})
            if not og_desc:
                og_desc = soup.find("meta", {"name": "description"})
            if og_desc and og_desc.get("content"):
                txt = og_desc["content"].strip()
                if len(txt) > 10:
                    result["description"] = txt[:1500]

        # ── 4. DOM class selectors ────────────────────────────────────────────
        if "date" not in result:
            for cls_pat in (
                re.compile(r"event[_-]date|fecha[_-]evento|start[_-]date", re.I),
                re.compile(r"tm[_-]date|event-info[_-]date", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    d = _parse_date_safe(el.get("datetime") or el.get_text(strip=True))
                    if d:
                        result["date"] = d
                        break

        if "time_start" not in result:
            for cls_pat in (
                re.compile(r"event[_-]time|hora[_-]evento|start[_-]time", re.I),
                re.compile(r"tm[_-]time|event-info[_-]time", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    t = _parse_time_str(el.get("datetime") or el.get_text(strip=True))
                    if t:
                        result["time_start"] = t
                        break

        if "venue_name" not in result:
            for cls_pat in (
                re.compile(r"event[_-]venue|venue[_-]name|lugar[_-]evento|recinto", re.I),
                re.compile(r"tm[_-]venue|location[_-]name|place[_-]name", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    text = el.get_text(strip=True).split("·")[0].split("|")[0].strip()
                    if text:
                        result["venue_name"] = text
                        break

        if "description" not in result:
            for cls_pat in (
                re.compile(r"event[_-]description|descripcion|detail[_-]content", re.I),
                re.compile(r"tm[_-]description|event[_-]info[_-]desc", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    txt = el.get_text(" ", strip=True)
                    if len(txt) > 30:
                        result["description"] = txt[:1500]
                        break

        if "price_range" not in result:
            for cls_pat in (
                re.compile(r"price|precio|ticket[_-]price|desde", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    pr = _parse_price(el.get_text(strip=True))
                    if pr:
                        result["price_range"] = pr
                        break

        return result

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page_url(self, soup: BeautifulSoup, current_url: str) -> str | None:
        """Return the next listing page URL or None."""
        # rel="next" — most reliable
        link = soup.find("a", rel=lambda v: v and "next" in v)
        if link and link.get("href"):
            return _absolute_url(link["href"], current_url)

        # class-based "Next" / "Siguiente" link
        link = (
            soup.find("a", class_=re.compile(r"\bnext\b|\bsiguiente\b", re.I))
            or soup.find("a", string=re.compile(r"siguiente|next|›|»", re.I))
        )
        if link and link.get("href"):
            href = link["href"]
            if href not in ("#", "javascript:void(0)"):
                return _absolute_url(href, current_url)

        # ?page=N increment: find current page number and bump it
        m = re.search(r"[?&]page=(\d+)", current_url)
        if m:
            cur_page = int(m.group(1))
            # Only auto-increment if there are still event links on this page
            # (the caller checks for empty cards — we just build the URL)
            next_url = re.sub(
                r"([?&]page=)\d+",
                lambda mm: mm.group(1) + str(cur_page + 1),
                current_url,
            )
            return next_url

        return None

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch Santiago events from all listing seed URLs, following pagination.

        For each seed URL:
          1. Detect event cards on the listing page
          2. Parse stub data from each card (name, image, listing-level date)
          3. Fetch each event's detail page for full date, venue, description
          4. Apply Santiago/RM filter
          5. Deduplicate by source_url across all seeds

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        all_events: list[dict[str, Any]] = []
        seen_urls:  set[str] = set()

        for seed_url in LISTING_SEEDS:
            logger.info("[ticketmaster] Seeding from %s", seed_url)

            url: str | None = seed_url
            page = 1

            while url and page <= self.max_pages:
                logger.info("[ticketmaster] page %d: %s", page, url)
                soup = self._get_soup(url)
                if soup is None:
                    break

                if self.debug and page == 1:
                    self._print_debug(soup)
                    break

                cards = self._find_cards(soup)
                if not cards:
                    logger.warning(
                        "[ticketmaster] No cards on page %d — stopping this seed", page
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

                    # Fetch detail page
                    detail = self.fetch_event_detail(detail_url)
                    for key, val in detail.items():
                        ev.setdefault(key, val)

                    # Must have a date
                    if not ev.get("date"):
                        logger.debug(
                            "[ticketmaster] No date for %r — skipping", ev.get("name")
                        )
                        continue

                    # Santiago / RM filter
                    location_hint = " ".join(
                        str(ev.get(f, "") or "")
                        for f in ("venue_name", "address", "name")
                    )
                    if not _is_santiago(location_hint):
                        logger.debug(
                            "[ticketmaster] Non-Santiago %r — skipping", ev.get("name")
                        )
                        continue

                    seen_urls.add(detail_url)
                    all_events.append(ev)
                    page_new += 1

                    if self.max_events and len(all_events) >= self.max_events:
                        logger.info(
                            "[ticketmaster] Reached max_events=%d", self.max_events
                        )
                        break

                logger.info(
                    "[ticketmaster] page %d: %d new RM events (total: %d)",
                    page, page_new, len(all_events),
                )

                if self.max_events and len(all_events) >= self.max_events:
                    break

                # Stop paginating if the page yielded nothing new
                if page_new == 0 and page > 1:
                    break

                next_url = self._next_page_url(soup, url)
                if not next_url or next_url == url:
                    break
                url = next_url
                page += 1
                time.sleep(REQUEST_DELAY)

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[ticketmaster] Total events collected: %d", len(all_events))
        return all_events

    # ── Debug helper ──────────────────────────────────────────────────────────

    def _print_debug(self, soup: BeautifulSoup) -> None:
        """Print first-page structural diagnostics."""
        print("\n" + "=" * 70)
        print("DEBUG — TicketmasterScraper")
        print("=" * 70)

        title = soup.find("title")
        print(f"\nPage <title>: {title.get_text(strip=True) if title else '(none)'}")

        # Count structural elements
        for tag in ("article", "section", "div", "li"):
            print(f"  <{tag}> count: {len(soup.find_all(tag))}")

        # Top-20 class names
        from collections import Counter
        cls_counts: Counter = Counter()
        for el in soup.find_all(["div", "article", "li", "section"]):
            for cls in el.get("class", []):
                cls_counts[cls] += 1
        print("\nTop-20 class names:")
        for cls, cnt in cls_counts.most_common(20):
            print(f"  {cnt:4d}×  .{cls}")

        # JSON-LD types present
        print("\nJSON-LD @types found:")
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    print(f"  @type={item.get('@type')!r}")
            except (json.JSONDecodeError, TypeError):
                pass

        # Event links found
        event_links = [
            a["href"] for a in soup.find_all("a", href=True) if _is_event_url(a["href"])
        ]
        print(f"\nEvent-detail links on page: {len(set(event_links))}")
        for href in list(set(event_links))[:5]:
            print(f"  {href}")

        # Cards
        cards = self._find_cards(soup)
        print(f"\nCards found by _find_cards(): {len(cards)}")
        if cards:
            print("\n── First card raw HTML (2000 chars) ───────────────────────────")
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

    parser = argparse.ArgumentParser(description="Ticketmaster Chile scraper")
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
        "--max-pages", type=int, default=10,
        help="Maximum listing pages to scrape",
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

    scraper = TicketmasterScraper(
        max_pages=args.max_pages,
        max_events=args.max_events,
        debug=args.debug,
    )
    events = scraper.fetch_events()

    if args.dry_run or args.debug:
        print(f"\n── Ticketmaster dry-run: {len(events)} RM events ───────────────")
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
                    f"  desc      : {str(ev.get('description', ''))[:120]!r}"
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
