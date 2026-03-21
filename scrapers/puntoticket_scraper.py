"""PuntoTicket scraper — fetches events from puntoticket.com/todos.

Only Santiago events are kept.  Uses requests + BeautifulSoup4.
A 2-second delay is inserted between paginated requests.

Run for a dry-run (fetch only, no DB writes):
    python scrapers/puntoticket_scraper.py --debug
"""
from __future__ import annotations

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

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://www.puntoticket.com"
LISTING_URL = f"{BASE_URL}/todos"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_DELAY = 2  # seconds between paginated requests

# Santiago identifiers — keep only events whose location text includes one of these
SANTIAGO_TOKENS = {
    # City / generic
    "santiago", "stgo", "rm",
    # Communes (all lowercase)
    "providencia", "las condes", "vitacura",
    "ñuñoa", "nunoa", "miraflores", "bellavista", "lastarria",
    "san miguel", "lo barnechea", "maipú", "maipu", "cerrillos",
    "la florida", "pudahuel", "quilicura", "el bosque", "la cisterna",
    "estación central", "estacion central", "quinta normal",
    "recoleta", "independencia", "conchalí", "conchali",
    "huechuraba", "renca", "cerro navia", "lo prado", "pudahuel",
    "san joaquín", "san joaquin", "la granja", "la pintana",
    "san ramón", "san ramon", "lo espejo", "pedro aguirre",
    "macul", "peñalolén", "penalolen", "la reina", "peñaflor",
    # Well-known venues that imply Santiago
    "parque padre hurtado", "movistar arena", "estadio nacional",
    "teatro caupolicán", "caupolican", "centro gam", "matucana",
    "espacio riesco", "club chocolate", "blondie",
}

# ── Spanish month lookup ──────────────────────────────────────────────────────

_MONTHS_ES: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # abbreviated
    "ene": 1, "feb": 2, "mar": 3, "abr": 4,
    "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _parse_date_es(raw: str) -> str | None:
    """Convert a Spanish date string to YYYY-MM-DD.

    Handles patterns like:
      - "sábado 22 de marzo de 2025"
      - "22 de marzo"            (year defaults to current)
      - "22/03/2025"
      - "2025-03-22"
    """
    raw = raw.strip().lower()

    # Already ISO: "2025-03-22"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2):0>2}-{m.group(3):0>2}"

    # Slash format: "22/03/2025" or "22/03/25"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"

    # Spanish prose: "22 de marzo de 2025" / "sábado, 22 de marzo"
    m = re.search(
        r"(\d{1,2})\s+de\s+([a-záéíóúüñ]+)(?:\s+de\s+(\d{4}))?", raw
    )
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        year_str = m.group(3)
        month_num = _MONTHS_ES.get(month_name[:3])
        if month_num:
            year = int(year_str) if year_str else datetime.now().year
            return f"{year:04d}-{month_num:02d}-{day:02d}"

    return None


def _parse_price(raw: str) -> list[float] | None:
    """Extract the lowest numeric price as [min, min].

    Examples: "$12.500", "Desde $5.000", "Gratis" → [0.0, 0.0]
    Returns None if no price info found.
    """
    if not raw:
        return None
    lower = raw.lower()
    if "gratis" in lower or "gratuito" in lower or "libre" in lower:
        return [0.0, 0.0]
    numbers = re.findall(r"[\d.]+", raw.replace(",", "."))
    prices: list[float] = []
    for n in numbers:
        try:
            val = float(n.replace(".", ""))
            if val >= 100:  # ignore stray small numbers
                prices.append(val)
        except ValueError:
            pass
    if prices:
        mn = min(prices)
        return [mn, max(prices)]
    return None


def _is_santiago(location_text: str) -> bool:
    """Return True if the location text suggests a Santiago event."""
    lower = location_text.lower()
    return any(tok in lower for tok in SANTIAGO_TOKENS)


def _absolute_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + href if href.startswith("/") else href


# ── Main scraper class ────────────────────────────────────────────────────────

class PuntoTicketScraper(BaseScraper):
    """Scrapes event listings from puntoticket.com/todos."""

    name = "puntoticket"

    def __init__(self, max_pages: int = 10, debug: bool = False) -> None:
        super().__init__()
        self.max_pages = max_pages
        self.debug = debug
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_soup(self, url: str) -> BeautifulSoup | None:
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.error("Failed to fetch %s: %s", url, exc)
            return None

    # ── Card parsing ──────────────────────────────────────────────────────────

    def _find_cards(self, soup: BeautifulSoup) -> list[Any]:
        """Return all event card elements on a listing page.

        PuntoTicket structure (confirmed from live HTML):
            <article class="col-4 col-sm-4 col-md-4" id="event_N">
        """
        # Primary: <article id="event_*"> — confirmed selector
        cards = soup.find_all("article", id=re.compile(r"^event_\d+$"))
        if cards:
            logger.debug("Found %d cards via article[id^=event_]", len(cards))
            return cards

        # Fallback 1: any <article> inside a .listado--eventos container
        container = soup.find(class_=re.compile(r"listado--eventos", re.I))
        if container:
            cards = container.find_all("article")
            if cards:
                logger.debug("Found %d cards inside .listado--eventos", len(cards))
                return cards

        # Fallback 2: generic article with event-like classes
        for cls_pat in (
            re.compile(r"event[-_]?card|card[-_]?event", re.I),
            re.compile(r"evento--box", re.I),
        ):
            cards = soup.find_all(["article", "div", "li"], class_=cls_pat)
            if cards:
                logger.debug("Found %d cards via class pattern %s", len(cards), cls_pat)
                return cards

        # Last-resort: any <a> whose href looks like a ticket-page slug,
        # grouped by their nearest block ancestor
        cards = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # PuntoTicket detail URLs: "/" + slug (no subfolder path like /eventos/)
            if not re.match(r"^/[a-z0-9][a-z0-9\-]+$", href):
                continue
            if href in seen:
                continue
            seen.add(href)
            parent = a
            for _ in range(5):
                if parent.parent and parent.parent.name in ("div", "article", "li", "section"):
                    parent = parent.parent
                else:
                    break
            cards.append(parent)

        logger.debug("Last-resort fallback: %d card ancestors", len(cards))
        return cards

    def _parse_card(self, card: Any) -> dict[str, Any] | None:
        """Extract an event dict from a single card element.

        PuntoTicket card structure (confirmed from live HTML):

            <article id="event_N">
              <a href="/event-slug">
                <div class="gallery-inner">
                  <img class="img--evento" src="...">
                </div>
                <div class="evento--box">
                  <p class="descripcion">
                    <strong>Venue Name</strong> / Category text
                  </p>
                  <h3>Event Title</h3>
                  <p class="fecha">21 de marzo 2026 - 22 de marzo 2026</p>
                </div>
              </a>
            </article>
        """
        event: dict[str, Any] = {}

        # ── URL ───────────────────────────────────────────────────────────────
        link = card.find("a", href=True)
        if not link:
            return None
        href = link["href"]
        full_url = _absolute_url(href)
        event["url"] = full_url
        event["source_url"] = full_url

        # ── Title — <h3> inside card ──────────────────────────────────────────
        for tag in ("h3", "h2", "h1", "h4"):
            el = card.find(tag)
            if el:
                text = el.get_text(strip=True)
                if text:
                    event["name"] = text
                    break
        if not event.get("name"):
            # Fallback: visible link text (excluding img alt)
            name_text = link.get_text(" ", strip=True)
            if name_text:
                event["name"] = name_text
        if not event.get("name"):
            return None

        # ── Image — <img class="img--evento"> ────────────────────────────────
        img = card.find("img", class_=re.compile(r"img--evento|event[-_]?img", re.I))
        if not img:
            img = card.find("img")
        if img:
            src = (
                img.get("data-src")
                or img.get("src")
                or img.get("data-lazy-src")
                or img.get("data-original")
            )
            if src and src.startswith("http"):
                event["image_url"] = src

        # ── Venue — <p class="descripcion"><strong>Name</strong> / …</p> ─────
        desc_el = card.find("p", class_=re.compile(r"descripcion|description|lugar|venue", re.I))
        if desc_el:
            strong = desc_el.find("strong")
            if strong:
                event["venue_name"] = strong.get_text(strip=True)
            else:
                # Take text before the "/" separator if present
                raw_desc = desc_el.get_text(strip=True)
                venue_part = raw_desc.split("/")[0].strip()
                if venue_part:
                    event["venue_name"] = venue_part
        # Fallback: any text that looks like a Santiago location
        if not event.get("venue_name"):
            for el in card.find_all(["p", "span", "div"]):
                text = el.get_text(strip=True)
                if _is_santiago(text) and 4 < len(text) < 120:
                    event["venue_name"] = text
                    break

        # ── Date — <p class="fecha">21 de marzo 2026 - 22 de marzo 2026</p> ──
        fecha_el = card.find(class_=re.compile(r"fecha|date|when|dia", re.I))
        if not fecha_el:
            fecha_el = card.find("time")

        if fecha_el:
            raw_date = (
                fecha_el.get("datetime")
                or fecha_el.get_text(strip=True)
            )
            # For date ranges like "21 de marzo 2026 - 22 de marzo 2026"
            # take the FIRST date (start date)
            first_part = raw_date.split("-")[0].split("al")[0].strip()
            parsed = _parse_date_es(first_part)
            if parsed:
                event["date"] = parsed
                # Extract time if present: "21 de marzo 2026 - 20:00 hs"
                t_match = re.search(r"(\d{1,2}):(\d{2})", raw_date)
                if t_match:
                    event["time_start"] = f"{int(t_match.group(1)):02d}:{t_match.group(2)}"

        # ── Price — look for price-related element ────────────────────────────
        price_el = card.find(class_=re.compile(r"precio|price|valor|costo|tarifa", re.I))
        if price_el:
            price_range = _parse_price(price_el.get_text(strip=True))
            if price_range:
                event["price_range"] = price_range

        return event

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page_url(self, soup: BeautifulSoup, current_url: str) -> str | None:
        """Return the URL of the next listing page, or None if last page."""
        next_link = (
            soup.find("a", rel=lambda v: v and "next" in v)
            or soup.find("a", class_=re.compile(r"next|siguiente", re.I))
            or soup.find("a", string=re.compile(r"siguiente|next|›|»", re.I))
        )
        if next_link and next_link.get("href"):
            return _absolute_url(next_link["href"])
        return None

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all Santiago events from puntoticket.com/todos.

        Returns a list of event dicts ready for classifier + enricher.
        """
        all_events: list[dict[str, Any]] = []
        url: str | None = LISTING_URL
        page = 1

        while url and page <= self.max_pages:
            logger.info("Fetching page %d: %s", page, url)
            soup = self._get_soup(url)
            if soup is None:
                break

            # Debug mode: dump raw HTML of the first card and stop
            if self.debug and page == 1:
                self._print_debug(soup)
                break

            cards = self._find_cards(soup)
            if not cards:
                logger.warning("No event cards found on page %d — stopping", page)
                break

            page_events = 0
            for card in cards:
                ev = self._parse_card(card)
                if ev is None:
                    continue
                # Santiago filter
                location_hint = ev.get("venue_name", "") + " " + ev.get("name", "")
                if not _is_santiago(location_hint):
                    continue
                all_events.append(ev)
                page_events += 1

            logger.info("Page %d: %d Santiago events collected", page, page_events)

            next_url = self._next_page_url(soup, url)
            if next_url == url:
                break  # guard against infinite loop
            url = next_url
            page += 1
            if url:
                time.sleep(REQUEST_DELAY)

        logger.info("Total events collected: %d", len(all_events))
        return all_events

    # ── Debug helper ─────────────────────────────────────────────────────────

    def _print_debug(self, soup: BeautifulSoup) -> None:
        """Print diagnostic info: card count, first card HTML, page title."""
        print("\n" + "=" * 70)
        print("DEBUG — PuntoTicket HTML inspection")
        print("=" * 70)

        title = soup.find("title")
        print(f"\nPage <title>: {title.get_text(strip=True) if title else '(none)'}")

        # Show counts for common structural elements
        for tag in ("article", "section", "div"):
            count = len(soup.find_all(tag))
            print(f"  <{tag}> elements: {count}")

        # Show all unique class names that appear on divs/articles
        print("\nTop-20 div/article class names on the page:")
        from collections import Counter
        class_counts: Counter = Counter()
        for el in soup.find_all(["div", "article", "li"]):
            for cls in el.get("class", []):
                class_counts[cls] += 1
        for cls, cnt in class_counts.most_common(20):
            print(f"  {cnt:4d}×  .{cls}")

        # Show the raw HTML of the first card we find (any selector)
        cards = self._find_cards(soup)
        print(f"\nCards found by _find_cards(): {len(cards)}")

        if cards:
            first_card = cards[0]
            print("\n── First card raw HTML (truncated to 2000 chars) ──────────────")
            raw = str(first_card)
            print(raw[:2000])
            if len(raw) > 2000:
                print(f"\n... ({len(raw) - 2000} more chars truncated)")

            print("\n── Parsed fields from first card ──────────────────────────────")
            parsed = self._parse_card(first_card)
            if parsed:
                for k, v in parsed.items():
                    print(f"  {k}: {v!r}")
            else:
                print("  (parse returned None)")
        else:
            # No cards found — show the first 3000 chars of the body for inspection
            print("\nNo cards found. First 3000 chars of <body>:")
            body = soup.find("body")
            if body:
                print(str(body)[:3000])

        print("\n" + "=" * 70)


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="PuntoTicket scraper")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Fetch first page only and print raw HTML structure (no DB writes)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=10, help="Maximum listing pages to scrape"
    )
    args = parser.parse_args()

    scraper = PuntoTicketScraper(max_pages=args.max_pages, debug=args.debug)
    events = scraper.fetch_events()
    if not args.debug:
        print(f"\nFetched {len(events)} events.")
        for ev in events[:5]:
            print(f"  • {ev.get('name')!r}  date={ev.get('date')}  venue={ev.get('venue_name')!r}")
