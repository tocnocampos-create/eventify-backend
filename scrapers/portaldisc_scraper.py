"""PortalDisc / PortalTickets Chile scraper.

Two complementary scraping strategies are merged into one pipeline:

  Strategy A — lista_eventos (paginated national catalogue)
  ─────────────────────────────────────────────────────────
  URL:  https://www.portaldisc.com/lista_eventos?pagina=N
  Scrapes every paginated page of the national event list.
  Santiago/RM filter applied via SANTIAGO_TOKENS on the venue + address fields
  extracted from each card.  Pagination continues while cards are found.

  Strategy B — per-venue cartelera pages
  ───────────────────────────────────────
  Entry:  https://www.portaldisc.com/portaltickets
  Lists all partner venues; each entry links to /cartelera/{venue-slug}.
  Only Santiago venues are followed (SANTIAGO_TOKENS on venue address/name).
  Each cartelera page lists upcoming events for that venue.

Both strategies feed the same detail-fetch → classifier → enricher → deduplicator
pipeline.  Deduplication by source_url prevents double-processing events that
appear in both the global list and a venue cartelera.

source_url:
    portaldisc:cl:{event_id}     when the detail URL contains a numeric ID
    portaldisc:cl:{slug}         when only a slug is available
    <full detail url>            fallback

Run:
    python scrapers/portaldisc_scraper.py --dry-run
    python scrapers/portaldisc_scraper.py --dry-run --verbose
    python scrapers/portaldisc_scraper.py --strategy lista --dry-run
    python scrapers/portaldisc_scraper.py --strategy cartelera --dry-run
    python scrapers/portaldisc_scraper.py --debug --strategy lista
    python scrapers/portaldisc_scraper.py --max-pages 3 --dry-run
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper
from scrapers.puntoticket_scraper import (
    SANTIAGO_TOKENS,
    _is_santiago,
    _parse_date_es,
    _parse_price,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL             = "https://www.portaldisc.com"
LISTA_EVENTOS_URL    = f"{BASE_URL}/lista_eventos"
PORTALTICKETS_URL    = f"{BASE_URL}/portaltickets"

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

REQUEST_DELAY = 2   # seconds between requests

# PortalDisc uses several pagination param names across page types
_PAGE_PARAMS = ("pagina", "page", "p", "pg")


# ── URL helpers ───────────────────────────────────────────────────────────────

def _abs(href: str) -> str:
    """Return an absolute URL, handling relative and protocol-relative hrefs."""
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return BASE_URL + href if href.startswith("/") else href


def _is_event_href(href: str) -> bool:
    """Return True if href looks like a PortalDisc event detail URL."""
    if not href:
        return False
    return bool(re.search(
        r"/(evento|eventos|detalle_evento|ver_evento|ticket|event)/",
        href, re.IGNORECASE,
    ))


def _is_cartelera_href(href: str) -> bool:
    """Return True if href looks like a PortalDisc venue cartelera URL."""
    if not href:
        return False
    return bool(re.search(r"/cartelera/", href, re.IGNORECASE))


def _source_url_from_event_url(url: str) -> str:
    """Build a stable source_url from an event detail URL.

    Priority:
      1. Numeric ID in path:  /evento/1234  →  portaldisc:cl:1234
      2. Slug in path:        /evento/gran-concierto  →  portaldisc:cl:gran-concierto
      3. Fallback:            full URL
    """
    m = re.search(r"/(?:evento|eventos|detalle_evento|ver_evento|ticket|event)/([^/?#]+)", url, re.I)
    if m:
        return f"portaldisc:cl:{m.group(1)}"
    return url


def _pagination_url(base: str, page: int) -> str:
    """Build a paginated URL for lista_eventos.

    Tries ?pagina=N first (PortalDisc default), falling back to ?page=N.
    If the base URL already contains a page param, replaces it.
    """
    for param in _PAGE_PARAMS:
        pattern = rf"([?&]{param}=)\d+"
        if re.search(pattern, base):
            return re.sub(pattern, lambda m: m.group(1) + str(page), base)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}pagina={page}"


# ── Text normalisation helpers ────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _slug_norm(s: str) -> str:
    """Lowercase + strip accents + remove all non-alpha chars."""
    return re.sub(r"[^a-z]", "", _strip_accents(s.lower()))


# ── Date / time parsing ───────────────────────────────────────────────────────

def _parse_date_safe(raw: str) -> str | None:
    """Parse any date string to YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    iso = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if iso:
        return iso.group(1)
    # Slash format DD/MM/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # Prose "21 de marzo 2026" / "sábado 21 de marzo"
    first = raw.split(" - ")[0].split(" al ")[0].strip()
    return _parse_date_es(first)


def _parse_time_safe(raw: str) -> str | None:
    """Extract HH:MM from any string."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", raw.strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


# ── Shared card-level field extractors ───────────────────────────────────────

def _extract_image(card: Any) -> str | None:
    """Return the best image URL from a card element."""
    img = card.find("img")
    if img:
        src = (
            img.get("data-src")
            or img.get("src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )
        if src:
            src = "https:" + src if src.startswith("//") else src
            if src.startswith("http"):
                return src
    # CSS background-image
    for el in card.find_all(style=True):
        style = el["style"]
        m = re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", style)
        if m:
            return m.group(1)
    return None


def _extract_price(card: Any) -> list[float] | None:
    """Return [min, max] price from a card, or None."""
    for cls_pat in (
        re.compile(r"precio|price|valor|costo|tarifa|entrada", re.I),
    ):
        el = card.find(class_=cls_pat)
        if el:
            pr = _parse_price(el.get_text(strip=True))
            if pr is not None:
                return pr
    # Search all text nodes for price-like content
    full_text = card.get_text(" ", strip=True)
    pr = _parse_price(full_text)
    return pr


# ── Scraper class ─────────────────────────────────────────────────────────────

class PortalDiscScraper(BaseScraper):
    """Scrapes events from PortalDisc/PortalTickets Chile.

    Two strategies:
      - 'lista': paginates /lista_eventos, filters to RM, fetches detail pages
      - 'cartelera': discovers Santiago venues from /portaltickets, then
                     scrapes each venue's /cartelera/{slug} page
    Both are run by default; pass strategy='lista' or 'cartelera' to restrict.
    """

    name = "portaldisc"

    def __init__(
        self,
        max_pages:  int  = 15,
        max_events: int  = 0,
        debug:      bool = False,
        strategy:   str  = "both",   # "lista" | "cartelera" | "both"
    ) -> None:
        super().__init__()
        self.max_pages  = max_pages
        self.max_events = max_events
        self.debug      = debug
        self.strategy   = strategy
        self.session    = requests.Session()
        self.session.headers.update(HEADERS)
        # Normalised DB venue names for fuzzy slug matching (loaded at fetch time)
        self._db_venue_norms: list[str] = []

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get_soup(self, url: str) -> BeautifulSoup | None:
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.error("[portaldisc] GET %s failed: %s", url, exc)
            return None

    # ── DB venue fuzzy matching ───────────────────────────────────────────────

    def _load_db_venues(self) -> None:
        """Load normalised venue names from DB for slug fuzzy matching."""
        try:
            from sqlalchemy import create_engine, text
            from scrapers.base_scraper import _get_database_url
            engine = create_engine(_get_database_url(), pool_pre_ping=True)
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT name FROM venues")).fetchall()
            engine.dispose()
            self._db_venue_norms = [_slug_norm(r[0]) for r in rows if r[0]]
            logger.info(
                "[portaldisc] loaded %d DB venue names for fuzzy matching",
                len(self._db_venue_norms),
            )
        except Exception as exc:
            logger.warning("[portaldisc] could not load DB venues: %s", exc)

    def _matches_db_venue(self, name: str) -> bool:
        """Return True if *name* fuzzy-matches any known DB venue (all Santiago).

        Matching rule: after normalising both strings (lowercase, strip accents,
        remove non-alpha), one must be a substring of the other, with a minimum
        length of 6 chars to avoid false positives from short words.
        """
        if not self._db_venue_norms or not name:
            return False
        norm = _slug_norm(name)
        if len(norm) < 4:
            return False
        for db_norm in self._db_venue_norms:
            if len(db_norm) < 6:
                continue
            if db_norm in norm or norm in db_norm:
                return True
        return False

    # ══════════════════════════════════════════════════════════════════════════
    # Strategy A — lista_eventos
    # ══════════════════════════════════════════════════════════════════════════

    def _find_lista_cards(self, soup: BeautifulSoup) -> list[Any]:
        """Return event row/card elements from a lista_eventos page.

        PortalDisc lista_eventos DOM variants (priority order):

        1. <table> rows — the classic static HTML layout uses a table where
           each <tr> is one event (th/td with fecha, artista, venue, precio).
        2. Named card divs — if they redesigned to a card layout.
        3. List items with event links.
        4. Last resort: block ancestors of all /evento/ links.
        """
        # 1. Table rows (most likely for a static-HTML Chilean ticket site)
        for tbl in soup.find_all("table"):
            rows = tbl.find_all("tr")
            # Require at least one data row that has an event-like link or date cell
            data_rows = [
                r for r in rows
                if r.find("a", href=_is_event_href)
                or r.find("td", string=re.compile(r"\d{1,2}/\d{1,2}|\de\s+\d{4}", re.I))
            ]
            if len(data_rows) >= 1:
                logger.debug(
                    "[portaldisc] lista_cards: %d table rows", len(data_rows)
                )
                return data_rows

        # 1b. Current PortalDisc layout: div.album cards
        albums = soup.find_all("div", class_="album")
        if albums:
            logger.debug("[portaldisc] lista_cards: %d .album elements", len(albums))
            return albums

        # 2. Named card divs / articles
        for cls_pat in (
            re.compile(r"event[_-]?(?:card|item|row|listing)", re.I),
            re.compile(r"lista[_-]?evento|evento[_-]?item", re.I),
            re.compile(r"show[_-]?item|concert[_-]?item", re.I),
        ):
            cards = soup.find_all(["div", "article", "li"], class_=cls_pat)
            if cards:
                logger.debug(
                    "[portaldisc] lista_cards: %d cards via %s", len(cards), cls_pat
                )
                return cards

        # 3. <ul>/<ol> list with event links
        for ul_cls in (
            re.compile(r"lista[_-]?eventos?|events?[_-]?list", re.I),
        ):
            ul = soup.find(["ul", "div"], class_=ul_cls)
            if ul:
                items = ul.find_all(["li", "div"])
                if items:
                    return items

        # 4. Last resort
        cards: list[Any] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not _is_event_href(href) or href in seen:
                continue
            seen.add(href)
            parent = a
            for _ in range(5):
                p = parent.parent
                if p and p.name in ("div", "article", "li", "tr", "section"):
                    parent = p
                else:
                    break
            cards.append(parent)
        logger.debug("[portaldisc] lista_cards: %d (last-resort)", len(cards))
        return cards

    def _parse_lista_card(self, card: Any) -> dict[str, Any] | None:
        """Extract a stub event dict from a lista_eventos row/card.

        PortalDisc table row structure (typical):
          <tr>
            <td class="fecha">21/04/2026</td>
            <td class="artista"><a href="/evento/1234">Event Title</a></td>
            <td class="venue">Teatro Caupolicán</td>
            <td class="ciudad">Santiago</td>
            <td class="precio">$12.000</td>
          </tr>

        Also handles div/article card layouts.
        """
        ev: dict[str, Any] = {}

        # ── URL + title ───────────────────────────────────────────────────────
        link = card.find("a", href=_is_event_href)
        if not link:
            # May be a header row or non-event row
            return None
        href = _abs(link["href"])
        ev["url"]        = href
        ev["source_url"] = _source_url_from_event_url(href)
        ev["name"]       = link.get_text(strip=True)

        if not ev["name"]:
            # Link text may be empty — try surrounding heading or first td
            for tag in ("h2", "h3", "h4", "strong", "b"):
                el = card.find(tag)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        ev["name"] = text
                        break
        if not ev["name"]:
            return None

        # ── Date ──────────────────────────────────────────────────────────────
        # In table layout: <td class="fecha"> or first <td> with a date-like value
        date_el = card.find(class_=re.compile(r"\bfecha\b|\bdate\b|\bdia\b", re.I))
        if not date_el:
            # Try <time> or <td> whose text looks like a date
            date_el = card.find("time")
        if not date_el:
            for td in card.find_all("td"):
                txt = td.get_text(strip=True)
                if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", txt) or re.search(
                    r"\d{1,2}\s+de\s+[a-z]+", txt, re.I
                ):
                    date_el = td
                    break

        if date_el:
            raw = date_el.get("datetime") or date_el.get_text(strip=True)
            d = _parse_date_safe(raw)
            if d:
                ev["date"] = d
                t = _parse_time_safe(raw)
                if t:
                    ev["time_start"] = t

        # ── Venue ─────────────────────────────────────────────────────────────
        venue_el = card.find(class_=re.compile(r"\bvenue\b|\brecinto\b|\blugar\b|\bteatro\b", re.I))
        if not venue_el:
            # In table layout: the td after the title td
            tds = card.find_all("td")
            # title_td index + 1 is often venue
            for i, td in enumerate(tds):
                if td.find("a", href=_is_event_href) and i + 1 < len(tds):
                    venue_el = tds[i + 1]
                    break
        if venue_el:
            ev["venue_name"] = venue_el.get_text(strip=True)

        # ── City / comuna (for Santiago filter) ───────────────────────────────
        city_el = card.find(class_=re.compile(r"\bciudad\b|\bcity\b|\bcomuna\b|\bregion\b", re.I))
        if not city_el:
            # In tables: the td after venue is often ciudad
            tds = card.find_all("td")
            for i, td in enumerate(tds):
                if venue_el and td is venue_el and i + 1 < len(tds):
                    city_el = tds[i + 1]
                    break
        if city_el:
            ev["_city"] = city_el.get_text(strip=True)

        # ── Image ─────────────────────────────────────────────────────────────
        img_url = _extract_image(card)
        if img_url:
            ev["image_url"] = img_url

        # ── Price ─────────────────────────────────────────────────────────────
        pr = _extract_price(card)
        if pr is not None:
            ev["price_range"] = pr

        return ev

    def _lista_next_page(self, soup: BeautifulSoup, current_url: str) -> str | None:
        """Return the next page URL for lista_eventos, or None."""
        # rel="next"
        link = soup.find("a", rel=lambda v: v and "next" in v)
        if link and link.get("href"):
            return _abs(link["href"])
        # "Siguiente" / "Next" / "»" anchor
        link = (
            soup.find("a", string=re.compile(r"siguiente|next|›|»", re.I))
            or soup.find("a", class_=re.compile(r"\bnext\b|\bsiguiente\b", re.I))
        )
        if link and link.get("href") and link["href"] not in ("#", "javascript:void(0)"):
            return _abs(link["href"])
        # ?pagina=N auto-increment
        m = re.search(r"[?&]pagina=(\d+)", current_url)
        if m:
            return _pagination_url(current_url, int(m.group(1)) + 1)
        return None

    def _fetch_lista_eventos(
        self, seen: set[str]
    ) -> list[dict[str, Any]]:
        """Scrape all Santiago events from the paginated lista_eventos feed."""
        events: list[dict[str, Any]] = []
        url: str | None = LISTA_EVENTOS_URL
        page = 1

        while url and page <= self.max_pages:
            logger.info("[portaldisc/lista] page %d: %s", page, url)
            soup = self._get_soup(url)
            if soup is None:
                break

            if self.debug and page == 1:
                self._print_debug_lista(soup)
                break

            cards = self._find_lista_cards(soup)
            if not cards:
                logger.warning("[portaldisc/lista] No cards on page %d — stopping", page)
                break

            page_new = 0
            for card in cards:
                ev = self._parse_lista_card(card)
                if ev is None:
                    continue
                if ev["source_url"] in seen:
                    continue

                # ── Early reject: explicit non-Santiago city in stub ──────────
                # Only skip when we have a city field that is clearly NOT Santiago.
                # If city is absent (most cards) we proceed optimistically and
                # defer the full check to after the detail page is fetched.
                explicit_city = (ev.get("_city") or "").strip()
                stub_hint = " ".join(
                    str(ev.get(f, "") or "") for f in ("venue_name", "_city", "name")
                )
                if (
                    explicit_city
                    and not _is_santiago(explicit_city)
                    and not self._matches_db_venue(ev.get("venue_name") or "")
                ):
                    continue

                # ── Fetch detail page for full address + missing fields ────────
                # Always fetch when stub lacks location info so the deferred
                # Santiago check below has the address field available.
                needs_detail = (
                    not ev.get("date")
                    or not ev.get("description")
                    or not ev.get("image_url")
                    or not _is_santiago(stub_hint)
                )
                if needs_detail:
                    detail = self.fetch_event_detail(ev["url"])
                    for k, v in detail.items():
                        ev.setdefault(k, v)

                if not ev.get("date"):
                    logger.debug("[portaldisc/lista] No date for %r — skipping", ev.get("name"))
                    continue

                # ── Deferred Santiago check using full address ─────────────────
                full_hint = " ".join(
                    str(ev.get(f, "") or "") for f in ("venue_name", "_city", "name", "address")
                )
                if not _is_santiago(full_hint) and not self._matches_db_venue(ev.get("venue_name") or ""):
                    logger.debug(
                        "[portaldisc/lista] Non-Santiago after detail fetch %r — skipping",
                        ev.get("name"),
                    )
                    continue

                ev.pop("_city", None)
                seen.add(ev["source_url"])
                events.append(ev)
                page_new += 1

                if self.max_events and (len(events) >= self.max_events):
                    return events

            logger.info(
                "[portaldisc/lista] page %d: %d new RM events", page, page_new
            )
            if page_new == 0 and page > 1:
                break

            next_url = self._lista_next_page(soup, url)
            if not next_url or next_url == url:
                break
            url = next_url
            page += 1
            time.sleep(REQUEST_DELAY)

        return events

    # ══════════════════════════════════════════════════════════════════════════
    # Strategy B — per-venue cartelera pages
    # ══════════════════════════════════════════════════════════════════════════

    def _fetch_venue_slugs(self) -> list[tuple[str, str]]:
        """Return list of (slug, venue_name) for Santiago venues.

        Scrapes /portaltickets which lists partner venues with links to their
        /cartelera/{slug} pages.  Only venues whose name or address contains a
        SANTIAGO_TOKEN are included.

        PortalDisc venue listing HTML variants:
          - <ul class="venues"> / <div class="venue-list"> with <a> links
          - A table of venue rows
          - Generic anchors pointing to /cartelera/
        """
        logger.info("[portaldisc/cartelera] Fetching venue list from %s", PORTALTICKETS_URL)
        soup = self._get_soup(PORTALTICKETS_URL)
        if soup is None:
            return []

        results: list[tuple[str, str]] = []
        seen_slugs: set[str] = set()

        for a in soup.find_all("a", href=_is_cartelera_href):
            href = a["href"]
            slug_m = re.search(r"/cartelera/([^/?#]+)", href, re.I)
            if not slug_m:
                continue
            slug = slug_m.group(1).strip("/")
            if slug in seen_slugs:
                continue

            venue_name = a.get_text(strip=True) or slug.replace("-", " ").title()

            # Filter to Santiago/RM venues only
            venue_hint = f"{venue_name} {slug}"
            if not _is_santiago(venue_hint):
                logger.debug(
                    "[portaldisc/cartelera] Skipping non-Santiago venue: %r", venue_name
                )
                continue

            seen_slugs.add(slug)
            results.append((slug, venue_name))
            logger.debug(
                "[portaldisc/cartelera] Venue found: %r (slug=%r)", venue_name, slug
            )

        logger.info(
            "[portaldisc/cartelera] %d Santiago venues found", len(results)
        )
        return results

    def _find_cartelera_cards(self, soup: BeautifulSoup) -> list[Any]:
        """Return event card elements from a /cartelera/{slug} page.

        The cartelera page shows upcoming events for one venue.
        Current PortalDisc structure:
          <div class="album" style="display:flex;">
            <div class="cover img-container"><a href="/evento/..."><img .../></a></div>
            <div class="info_responsivo">
              <a href="/evento/..."><p><i>…</i> TITLE</p><p>Date string</p></a>
            </div>
          </div>
        """
        # 0. Current layout: div.album cards (highest priority)
        albums = soup.find_all("div", class_="album")
        if albums:
            logger.debug("[portaldisc] cartelera_cards: %d .album elements", len(albums))
            return albums

        # 1. Table rows with event links
        for tbl in soup.find_all("table"):
            rows = [r for r in tbl.find_all("tr") if r.find("a", href=_is_event_href)]
            if rows:
                return rows

        # 2. Named card elements
        for cls_pat in (
            re.compile(r"event[_-]?(?:card|item|row)|show[_-]?item|espectaculo", re.I),
            re.compile(r"cartelera[_-]?item|agenda[_-]?item", re.I),
        ):
            cards = soup.find_all(["div", "article", "li"], class_=cls_pat)
            if cards:
                return cards

        # 3. List items containing event links
        for ul in soup.find_all(["ul", "ol"]):
            items = [li for li in ul.find_all("li") if li.find("a", href=_is_event_href)]
            if items:
                return items

        # 4. Last resort: immediate block ancestors of event links (depth 1 only)
        cards: list[Any] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not _is_event_href(href) or href in seen:
                continue
            seen.add(href)
            parent = a.parent
            if parent and parent.name in ("div", "article", "li", "tr", "section"):
                cards.append(parent)
            else:
                cards.append(a)
        return cards

    def _parse_cartelera_card(
        self, card: Any, venue_name: str
    ) -> dict[str, Any] | None:
        """Extract a stub event dict from a cartelera event card.

        Handles current PortalDisc .album layout:
          <div class="album">
            <div class="cover img-container"><a href="/evento/..."><img/></a></div>
            <div class="info_responsivo">
              <a href="/evento/...">
                <p><i class="fa fa-ticket"></i> EVENT TITLE</p>
                <p style="font-weight: normal">Jueves 14 de mayo 2026, 19:00</p>
              </a>
            </div>
          </div>
        """
        ev: dict[str, Any] = {}

        # ── URL ───────────────────────────────────────────────────────────────
        # Prefer the link inside .info_responsivo (has title/date text).
        # Fall back to any /evento/ link in the card.
        info_div = card.find(class_="info_responsivo")
        link = None
        if info_div:
            link = info_div.find("a", href=_is_event_href)
        if not link:
            link = card.find("a", href=_is_event_href)
        if not link:
            return None

        href = _abs(link["href"])
        ev["url"]        = href
        ev["source_url"] = _source_url_from_event_url(href)
        ev["venue_name"] = venue_name

        # ── Title ─────────────────────────────────────────────────────────────
        # In .info_responsivo the first <p> contains icon + title text.
        # Stripping <i> children gives the clean title.
        paragraphs = link.find_all("p")
        if paragraphs:
            first_p = paragraphs[0]
            # Remove icon elements (<i>) before extracting text
            for icon in first_p.find_all("i"):
                icon.decompose()
            title_text = first_p.get_text(strip=True)
            if title_text:
                ev["name"] = title_text

        if not ev.get("name"):
            ev["name"] = link.get_text(strip=True)

        if not ev.get("name"):
            for tag in ("h2", "h3", "h4", "strong", "b"):
                el = card.find(tag)
                if el and el.get_text(strip=True):
                    ev["name"] = el.get_text(strip=True)
                    break
        if not ev.get("name"):
            return None

        # ── Date + time ───────────────────────────────────────────────────────
        # Second <p> in .info_responsivo link: "Jueves 14 de mayo 2026, 19:00"
        if len(paragraphs) >= 2:
            raw = paragraphs[1].get_text(strip=True)
            d = _parse_date_safe(raw)
            if d:
                ev["date"] = d
                t = _parse_time_safe(raw)
                if t:
                    ev["time_start"] = t

        if not ev.get("date"):
            date_el = (
                card.find("time")
                or card.find(class_=re.compile(r"\bfecha\b|\bdate\b|\bdia\b", re.I))
            )
            if not date_el:
                for td in card.find_all("td"):
                    txt = td.get_text(strip=True)
                    if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", txt) or re.search(
                        r"\d{1,2}\s+de\s+[a-z]+", txt, re.I
                    ):
                        date_el = td
                        break
            if date_el:
                raw = date_el.get("datetime") or date_el.get_text(strip=True)
                d = _parse_date_safe(raw)
                if d:
                    ev["date"] = d
                    t = _parse_time_safe(raw)
                    if t:
                        ev["time_start"] = t

        # ── Image ─────────────────────────────────────────────────────────────
        img_url = _extract_image(card)
        if img_url:
            ev["image_url"] = img_url

        # ── Price ─────────────────────────────────────────────────────────────
        pr = _extract_price(card)
        if pr is not None:
            ev["price_range"] = pr

        return ev

    def _fetch_venue_cartelera(
        self, slug: str, venue_name: str, seen: set[str]
    ) -> list[dict[str, Any]]:
        """Scrape all upcoming events from one venue's cartelera page."""
        url = f"{BASE_URL}/cartelera/{slug}"
        logger.info("[portaldisc/cartelera] %r — %s", venue_name, url)
        time.sleep(REQUEST_DELAY)
        soup = self._get_soup(url)
        if soup is None:
            return []

        cards = self._find_cartelera_cards(soup)
        if not cards:
            logger.warning(
                "[portaldisc/cartelera] No event cards for venue %r", venue_name
            )
            return []

        events: list[dict[str, Any]] = []
        for card in cards:
            ev = self._parse_cartelera_card(card, venue_name)
            if ev is None:
                continue
            if ev["source_url"] in seen:
                continue

            # Fetch detail page when key fields are absent
            if not ev.get("date") or not ev.get("description"):
                detail = self.fetch_event_detail(ev["url"])
                for k, v in detail.items():
                    ev.setdefault(k, v)

            if not ev.get("date"):
                logger.debug(
                    "[portaldisc/cartelera] No date for %r — skipping", ev.get("name")
                )
                continue

            seen.add(ev["source_url"])
            events.append(ev)

            if self.max_events and len(events) >= self.max_events:
                break

        logger.info(
            "[portaldisc/cartelera] %r: %d events", venue_name, len(events)
        )
        return events

    def _fetch_carteleras(self, seen: set[str]) -> list[dict[str, Any]]:
        """Run the venue-cartelera strategy and return all events found."""
        venue_slugs = self._fetch_venue_slugs()
        events: list[dict[str, Any]] = []

        for slug, venue_name in venue_slugs:
            evs = self._fetch_venue_cartelera(slug, venue_name, seen)
            events.extend(evs)
            if self.max_events and len(events) >= self.max_events:
                break

        return events

    # ══════════════════════════════════════════════════════════════════════════
    # Detail page (both strategies share this)
    # ══════════════════════════════════════════════════════════════════════════

    def fetch_event_detail(self, url: str) -> dict[str, Any]:
        """Fetch a PortalDisc event detail page and extract full metadata.

        PortalDisc detail page structure (typical static HTML):
          <div class="evento-detalle"> or <div id="evento">
            <h1 class="titulo-evento">Title</h1>
            <div class="fecha-evento">Sábado 23 de Abril 2026 · 20:00 hrs</div>
            <div class="venue">Teatro Caupolicán</div>
            <div class="direccion">San Diego 850, Santiago Centro</div>
            <div class="descripcion">...</div>
            <div class="precio">Desde $12.000</div>
            <img class="poster" src="...">
          </div>

        Also tries JSON-LD and og: meta tags as fallback.
        """
        time.sleep(REQUEST_DELAY)
        soup = self._get_soup(url)
        if soup is None:
            return {}

        result: dict[str, Any] = {}

        # ── 1. JSON-LD structured data ────────────────────────────────────────
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
                    raw_t = re.sub(r"^\d{4}-\d{2}-\d{2}", "", start)
                    t = _parse_time_safe(raw_t)
                    if t:
                        result["time_start"] = t
                    # End time
                    end = item.get("endDate") or ""
                    t_end = _parse_time_safe(re.sub(r"^\d{4}-\d{2}-\d{2}", "", end))
                    if t_end:
                        result["time_end"] = t_end
                    # Venue
                    loc = item.get("location") or {}
                    if isinstance(loc, dict):
                        vname = loc.get("name")
                        if vname:
                            result["venue_name"] = str(vname).strip()
                        addr = loc.get("address") or {}
                        if isinstance(addr, dict):
                            parts = [
                                addr.get("streetAddress", ""),
                                addr.get("addressLocality", ""),
                            ]
                            a_str = " ".join(p for p in parts if p).strip()
                            if a_str:
                                result["address"] = a_str
                        elif isinstance(addr, str) and addr.strip():
                            result["address"] = addr.strip()
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
                    img = item.get("image")
                    if isinstance(img, str) and img.startswith("http"):
                        result["image_url"] = img
                    elif isinstance(img, list) and img:
                        result["image_url"] = str(img[0])
                    break
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        # ── 2. DOM selectors ──────────────────────────────────────────────────

        # Date
        if "date" not in result:
            for cls_pat in (
                re.compile(r"fecha[_-]?evento|event[_-]?date|date[_-]?event|cuando", re.I),
                re.compile(r"^fecha$|^date$", re.I),
            ):
                el = soup.find(class_=cls_pat) or soup.find("time")
                if el:
                    raw = el.get("datetime") or el.get_text(strip=True)
                    d = _parse_date_safe(raw)
                    if d:
                        result["date"] = d
                        t = _parse_time_safe(raw)
                        if t:
                            result["time_start"] = t
                        break

        # Time (separate element)
        if "time_start" not in result:
            for cls_pat in (
                re.compile(r"\bhora\b|\bhorario\b|\btime[_-]?start\b|\bstart[_-]?time\b", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    t = _parse_time_safe(el.get_text(strip=True))
                    if t:
                        result["time_start"] = t
                        break

        # Venue name
        if "venue_name" not in result:
            for cls_pat in (
                re.compile(r"venue[_-]?name|recinto[_-]?nombre|nombre[_-]?venue|lugar[_-]?evento", re.I),
                re.compile(r"^venue$|^recinto$|^lugar$|^teatro$", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        result["venue_name"] = text
                        break

        # Address
        if "address" not in result:
            for cls_pat in (
                re.compile(r"\bdireccion\b|\bdirección\b|\baddress\b|\bubicacion\b|\bubicación\b", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        result["address"] = text
                        break

        # Description
        if "description" not in result:
            for cls_pat in (
                re.compile(r"descripcion|description|detalle[_-]?evento|event[_-]?detail|sobre[_-]?evento|info[_-]?evento", re.I),
                re.compile(r"^descripcion$|^description$|^detalle$|^about$", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    txt = el.get_text(" ", strip=True)
                    if len(txt) > 30:
                        result["description"] = txt[:1500]
                        break

        # Price
        if "price_range" not in result:
            for cls_pat in (
                re.compile(r"\bprecio\b|\bprice\b|\bvalor\b|\btarifa\b|\bentrada\b|\bcosto\b", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    pr = _parse_price(el.get_text(strip=True))
                    if pr is not None:
                        result["price_range"] = pr
                        break

        # Image
        if "image_url" not in result:
            img_url = _extract_image(soup.find(class_=re.compile(r"poster|flyer|imagen|banner|cover", re.I)) or soup)
            if img_url:
                result["image_url"] = img_url

        # ── 3. OpenGraph fallbacks ─────────────────────────────────────────────
        if "image_url" not in result:
            og = soup.find("meta", {"property": "og:image"})
            if og and og.get("content", "").startswith("http"):
                result["image_url"] = og["content"]

        if "description" not in result:
            for attr, name in (
                ("property", "og:description"),
                ("name",     "description"),
            ):
                meta = soup.find("meta", {attr: name})
                if meta and meta.get("content", "").strip():
                    txt = meta["content"].strip()
                    if len(txt) > 10:
                        result["description"] = txt[:1500]
                    break

        return result

    # ══════════════════════════════════════════════════════════════════════════
    # Public fetch_events
    # ══════════════════════════════════════════════════════════════════════════

    def fetch_events(self) -> list[dict[str, Any]]:
        """Run configured strategies and return all RM events.

        Shared `seen` set prevents double-processing events found by both
        lista_eventos and a venue cartelera page.
        """
        self._load_db_venues()

        seen:   set[str]        = set()
        events: list[dict[str, Any]] = []

        if self.strategy in ("lista", "both"):
            lista_events = self._fetch_lista_eventos(seen)
            events.extend(lista_events)
            logger.info("[portaldisc] lista_eventos: %d events", len(lista_events))

        if self.strategy in ("cartelera", "both"):
            if self.max_events and len(events) >= self.max_events:
                pass
            else:
                cart_events = self._fetch_carteleras(seen)
                events.extend(cart_events)
                logger.info("[portaldisc] cartelera: %d events", len(cart_events))

        logger.info("[portaldisc] Total events collected: %d", len(events))
        return events

    # ══════════════════════════════════════════════════════════════════════════
    # Debug helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _print_debug_lista(self, soup: BeautifulSoup) -> None:
        """Structural diagnostics for lista_eventos first page."""
        print("\n" + "=" * 70)
        print("DEBUG — PortalDiscScraper / lista_eventos")
        print("=" * 70)

        title = soup.find("title")
        print(f"\nPage <title>: {title.get_text(strip=True) if title else '(none)'}")

        # Tables
        tables = soup.find_all("table")
        print(f"\nTables: {len(tables)}")
        for i, tbl in enumerate(tables[:3]):
            rows = tbl.find_all("tr")
            print(f"  table[{i}]: {len(rows)} rows, sample TH: {[th.get_text(strip=True) for th in tbl.find_all('th')[:6]]}")

        # Top class names
        from collections import Counter
        cls_counts: Counter = Counter()
        for el in soup.find_all(["div", "article", "li", "tr", "td"]):
            for cls in el.get("class", []):
                cls_counts[cls] += 1
        print("\nTop-20 class names:")
        for cls, cnt in cls_counts.most_common(20):
            print(f"  {cnt:4d}×  .{cls}")

        # Event links
        event_links = [a["href"] for a in soup.find_all("a", href=True) if _is_event_href(a["href"])]
        print(f"\nEvent-detail links: {len(set(event_links))}")
        for href in list(set(event_links))[:5]:
            print(f"  {_abs(href)}")

        cards = self._find_lista_cards(soup)
        print(f"\nCards found by _find_lista_cards(): {len(cards)}")
        if cards:
            raw = str(cards[0])
            print(f"\nFirst card HTML (2000 chars):\n{raw[:2000]}")
            parsed = self._parse_lista_card(cards[0])
            print("\nParsed stub:", parsed)
        print("\n" + "=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="PortalDisc / PortalTickets Chile scraper")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and print sample events — no DB writes",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print first-page HTML structure for the selected strategy",
    )
    parser.add_argument(
        "--strategy",
        choices=["lista", "cartelera", "both"],
        default="both",
        help="Which scraping strategy to run (default: both)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=15,
        help="Maximum pages for lista_eventos pagination",
    )
    parser.add_argument(
        "--max-events", type=int, default=0,
        help="Stop after this many total events (0 = unlimited)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print price, image, and description for each sample event",
    )
    args = parser.parse_args()

    scraper = PortalDiscScraper(
        max_pages=args.max_pages,
        max_events=args.max_events,
        debug=args.debug,
        strategy=args.strategy,
    )
    events = scraper.fetch_events()

    if args.dry_run or args.debug:
        print(f"\n── PortalDisc dry-run: {len(events)} RM events ─────────────────")
        for ev in events[:10]:
            print(
                f"\n  name      : {ev.get('name')!r}\n"
                f"  date      : {ev.get('date')}\n"
                f"  time_start: {ev.get('time_start')}\n"
                f"  venue_name: {ev.get('venue_name')!r}\n"
                f"  address   : {ev.get('address')!r}\n"
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
