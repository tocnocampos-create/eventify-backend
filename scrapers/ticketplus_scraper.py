"""TicketPlus Chile scraper — comprehensive Santiago/RM event coverage.

TicketPlus (ticketplus.cl) is built on Spree Commerce (Ruby on Rails).
Event detail pages carry metadata in Spree "properties" and JSON-LD.

Scraping strategy (in priority order — seen_urls deduplicates across sources):

1. Company pages  (venue_name forced to canonical DB name, geo filter skipped)
   These run FIRST so the correct venue_id is resolved before the catch-all
   processes the same event with only JSON-LD venue info.

   Cultural / performing-arts venues (Theatre, Music, etc.):
     gran-sala-sinfonica-nacional → Gran Sala Sinfónica Nacional
     club-subterraneo             → Club Subterráneo
     cachafaz                     → Teatro Cachafaz
     teatro-azares                → Teatro Azares
     camilo-henriquez             → Teatro Camilo Henríquez
     delpuente                    → Teatro del Puente
     teatro-nacional-chileno      → Teatro Nacional Chileno
     ictus                        → Teatro Ictus
     teatro-lospleimovil          → Teatro Lospleimovil
     teatro-la-memoria            → Teatro La Memoria
     teatro-finis-terrae          → Teatro Finis Terrae
     pontificia-universidad-cat.  → Teatro UC
     sala-nemesio                 → Sala Nemesio Antúnez
     m100                         → Matucana 100
     corpartes                    → CorpArtes
     teatro-universidad-de-chile  → Teatro Universidad de Chile
     planetario-huechuraba        → Planetario de Santiago
     gam                          → GAM (usually empty; gam-subdomain covers it)
     universidad-mayor            → Sala K U.Mayor
     centro-arte-alameda          → Centro Arte Alameda (404 → skipped; states-rm covers it)

2. /states/region-metropolitana  (catch-all RM page, 337+ event links)
   Runs after company pages; anything not already in seen_urls gets fetched.
   Enricher resolves venue from JSON-LD location.name.

3. GAM subdomain  https://gam.ticketplus.cl/events/search.json
   56 upcoming GAM events, venue forced to "GAM".

source_url is the canonical /events/{slug} URL — stable across re-scrapes.

Run:
    python scrapers/ticketplus_scraper.py --dry-run
    python scrapers/ticketplus_scraper.py --dry-run --verbose
    python scrapers/ticketplus_scraper.py --source company:corpartes --dry-run
    python scrapers/ticketplus_scraper.py --source states-rm --dry-run
    python scrapers/ticketplus_scraper.py --source gam-subdomain --dry-run
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
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

# ── Scraper-level timeout ──────────────────────────────────────────────────────

FETCH_TIMEOUT_SECONDS = 300  # 5 minutes — abort fetch_events() if it hangs this long


class _FetchTimeout(Exception):
    """Raised by SIGALRM handler when fetch_events() exceeds FETCH_TIMEOUT_SECONDS."""


def _alarm_handler(signum: int, frame: object) -> None:
    raise _FetchTimeout(
        f"TicketPlus fetch_events exceeded {FETCH_TIMEOUT_SECONDS}s limit"
    )


# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://ticketplus.cl"

# ── Company pages ─────────────────────────────────────────────────────────────
# Each corresponds to a known RM venue in the DB.  venue_name_override is
# forced before the enricher runs so the correct venue_id is always resolved.
# tuple: (company_slug, canonical_venue_name, category_hint, lock_category)
COMPANY_CONFIG: list[tuple[str, str, str | None, bool]] = [
    # Performing arts & cultural centres
    ("gran-sala-sinfonica-nacional",             "Gran Sala Sinfónica Nacional", "Música",  True),
    ("club-subterraneo",                         "Club Subterráneo",              "Música",  False),
    ("cachafaz",                                 "Teatro Cachafaz",               "Teatro",  True),
    ("teatro-azares",                            "Teatro Azares",                 "Teatro",  True),
    ("camilo-henriquez",                         "Teatro Camilo Henríquez",       "Teatro",  True),
    ("delpuente",                                "Teatro del Puente",             "Teatro",  True),
    ("teatro-nacional-chileno",                  "Teatro Nacional Chileno",       "Teatro",  True),
    ("ictus",                                    "Teatro Ictus",                  "Teatro",  True),
    ("teatro-lospleimovil",                      "Teatro Lospleimovil",           "Teatro",  True),
    ("teatro-la-memoria",                        "Teatro La Memoria",             "Teatro",  True),
    ("teatro-finis-terrae",                      "Teatro Finis Terrae",           "Teatro",  True),
    ("pontificia-universidad-catolica-de-chile", "Teatro UC",                     "Teatro",  True),
    # Already-configured venues (kept for compatibility)
    ("sala-nemesio",                             "Sala Nemesio",                  None,      False),
    ("m100",                                     "Centro Cultural Matucana 100",  None,      False),
    ("corpartes",                                "CorpArtes",                     "Teatro",  True),
    ("teatro-universidad-de-chile",              "Teatro Universidad de Chile",   "Teatro",  True),
    ("planetario-huechuraba",                    "Planetario de Santiago",        None,      False),
    ("gam",                                      "GAM",                           None,      False),
    # Small cultural venues
    ("universidad-mayor",                        "Sala K U.Mayor",                "Arte",    False),
    # centro-arte-alameda has no company page on TicketPlus (404); events are
    # captured by the states-rm catch-all and attributed to venue id=118.
    # The entry below enables the fallback-search path for future-proofing.
    ("centro-arte-alameda",                      "Centro Arte Alameda",           "Arte",    False),
]

# ── Región Metropolitana catch-all ────────────────────────────────────────────
# All RM events on a single HTML page (337+ links, no pagination).
# Runs AFTER company pages so seen_urls deduplication skips already-covered events.
STATES_RM_URL = "https://ticketplus.cl/states/region-metropolitana"

# ── GAM subdomain ─────────────────────────────────────────────────────────────
GAM_SUBDOMAIN  = "https://gam.ticketplus.cl"
GAM_VENUE_NAME = "GAM"

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

# Matches trailing datetime noise appended to Spree slugs/titles:
#   "La Elegida 2026 04 29 20 30 00 0400"   ← slug-derived (spaces, unsigned tz)
#   "Concierto 2026-04-29T20:30:00-04:00"   ← ISO with sign
#   "Show 2026 04 29"                        ← date only
_TRAILING_DATETIME_RE = re.compile(
    r"\s+\d{4}[\s\-]\d{1,2}[\s\-]\d{1,2}"  # YYYY MM DD (spaces or hyphens)
    r"(?:[\s:T]\d{2}){0,4}"                 # up to 4 × exact 2-digit time parts
    r"(?:\s*[+\-]?\d{4})?"                  # optional ±HHMM or bare HHMM tz
    r"\s*$",
    re.I,
)


def _clean_title(title: str) -> str:
    """Strip trailing date/time garbage from Spree-generated event titles."""
    return _TRAILING_DATETIME_RE.sub("", title).strip()


def _absolute_url(href: str, base: str = BASE_URL) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return base + href if href.startswith("/") else href


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
            resp = self.session.get(url, timeout=30)
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

        # 4. Last resort: collect block ancestors of /products/ or /events/ links
        cards = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.search(r"/products?/|/events?/", href, re.I):
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

    def _parse_card(self, card: Any, base_url: str = BASE_URL) -> dict[str, Any] | None:
        """Extract stub event fields from a listing-page card.

        Returns a partial dict; detail page fills in date, time, venue, price.
        base_url is used to resolve relative hrefs (important for subdomains).
        """
        event: dict[str, Any] = {}

        # ── URL ───────────────────────────────────────────────────────────────
        link = card.find("a", href=re.compile(r"/(?:products?|events?)/", re.I))
        if not link:
            link = card.find("a", href=True)
        if not link:
            return None
        href = link.get("href", "")
        if not href or href in ("#", "javascript:void(0)"):
            return None
        full_url = _absolute_url(href, base_url)
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

        # ── 0. div.description-content — primary description source ───────────
        # TicketPlus renders the real event copy inside div.description-content.
        # We check this FIRST so it wins over the JSON-LD "description" field,
        # which is always auto-generated as "EventName, Venue, City, Date".
        desc_div = soup.find("div", class_="description-content")
        if desc_div:
            txt = desc_div.get_text(" ", strip=True)
            if len(txt) > 30:
                result["description"] = txt[:1500]

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
                    ld_type = item.get("@type", "")

                    # ── Product JSON-LD → extract price from offers ────────────
                    if ld_type == "Product" and "price_range" not in result:
                        offers = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        low = offers.get("price") or offers.get("lowPrice")
                        high = offers.get("highPrice") or low
                        if low is not None:
                            try:
                                result["price_range"] = [float(low), float(high)]
                            except (TypeError, ValueError):
                                pass
                        continue

                    if ld_type not in (
                        "Event", "MusicEvent", "TheaterEvent",
                        "SportsEvent", "ComedyEvent", "DanceEvent",
                    ):
                        continue

                    # ── Event JSON-LD ──────────────────────────────────────────
                    # Name — use LD name as fallback (may have encoding issues);
                    # H1/og:title extraction below gives better results.
                    if "name" not in result:
                        ld_name = (item.get("name") or "").strip()
                        if ld_name:
                            result["name"] = _clean_title(ld_name)

                    # Date + time — extract startDate regardless of whether date
                    # was already set from the Spree properties table, so we
                    # always capture time_start from the ISO datetime string.
                    start = item.get("startDate") or ""
                    if "date" not in result:
                        iso_m = re.match(r"(\d{4}-\d{2}-\d{2})", start)
                        if iso_m:
                            result["date"] = iso_m.group(1)
                    if "time_start" not in result and start:
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
                            offers = offers[0] if offers else {}
                        low = offers.get("price") or offers.get("lowPrice")
                        high = offers.get("highPrice") or low
                        if low is not None:
                            try:
                                result["price_range"] = [float(low), float(high)]
                            except (TypeError, ValueError):
                                pass

                    # Description — TicketPlus auto-generates the JSON-LD
                    # description as "EventName, Venue, City, DD/MM/YYYY".
                    # Skip that; use div.description-content (section 0) or
                    # accept only descriptions that are clearly real event copy
                    # (i.e., do NOT end with a DD/MM/YYYY date string).
                    if "description" not in result:
                        desc = (item.get("description") or "").strip()
                        if desc and not re.search(r"\d{2}/\d{2}/\d{4}\s*$", desc):
                            if len(desc) > 30:
                                result["description"] = desc[:1500]

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

        # ── 4. Name: H1 or og:title (cleaned of TP prefix + date/time suffix) ─
        # TicketPlus H1: "Tus entradas para <EventName>"
        # TicketPlus og:title: "Entradas para <EventName> - Ticketplus"
        # Strip known prefixes/suffixes to get the bare event name.
        _TP_NAME_PREFIXES = ("Tus entradas para ", "Entradas para ", "Entradas ")
        _TP_NAME_SUFFIX   = " - Ticketplus"

        def _extract_tp_name(raw: str) -> str:
            for pfx in _TP_NAME_PREFIXES:
                if raw.lower().startswith(pfx.lower()):
                    raw = raw[len(pfx):]
                    break
            if raw.lower().endswith(_TP_NAME_SUFFIX.lower()):
                raw = raw[: -len(_TP_NAME_SUFFIX)]
            return _clean_title(raw.strip())

        h1 = soup.find("h1")
        if h1:
            raw_name = h1.get_text(strip=True)
            if raw_name and len(raw_name) > 2:
                # Override weaker LD name with the properly accented H1 name
                result["name"] = _extract_tp_name(raw_name)

        if not result.get("name"):
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title and og_title.get("content"):
                result["name"] = _extract_tp_name(og_title["content"])

        # ── 5. Description: Spree product-description div only ────────────────
        # Do NOT fall back to meta description — on Spree event pages the meta
        # description is auto-generated from product properties and typically
        # contains the venue address, not actual event copy.
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

        # ── 6. Time fallback: parse HH:MM from URL slug ───────────────────────
        # TP slugs end with -YYYY-MM-DD-HH-MM-SS-TZ
        # e.g. la-elegida-2026-04-29-20-30-00-0400 → time_start "20:30"
        if "time_start" not in result:
            slug = url.rstrip("/").split("/")[-1]
            t_m = re.search(
                r"-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-\d{2}-?\d{4}$", slug
            )
            if t_m:
                result["time_start"] = f"{t_m.group(4)}:{t_m.group(5)}"

        # ── 7. Image fallback: og:image ───────────────────────────────────────
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

    # ── Company URL resolver (with search fallback) ───────────────────────────

    def _resolve_company_url(self, slug: str, venue_name: str) -> str | None:
        """Return the listing URL for a company slug, or None if unavailable.

        Tries the canonical /companies/{slug} page first.  If that returns a
        4xx, falls back to /search?q={venue_name}.  Returns None (with a
        warning) if both fail so the caller can skip gracefully.
        """
        from urllib.parse import quote_plus

        direct = f"{BASE_URL}/companies/{slug}"
        try:
            r = self.session.head(direct, timeout=10, allow_redirects=True)
            if r.status_code < 400:
                return direct
            logger.warning(
                "[ticketplus] Company page %s → %d, trying search fallback",
                direct, r.status_code,
            )
        except requests.RequestException as exc:
            logger.warning("[ticketplus] HEAD %s failed: %s", direct, exc)

        search = f"{BASE_URL}/search?q={quote_plus(venue_name)}"
        try:
            r = self.session.head(search, timeout=10, allow_redirects=True)
            if r.status_code < 400:
                logger.info(
                    "[ticketplus] Search fallback for %r: %s", venue_name, search
                )
                return search
        except requests.RequestException as exc:
            logger.warning("[ticketplus] HEAD search %s failed: %s", search, exc)

        logger.warning(
            "[ticketplus] No working URL for company=%r venue=%r — skipping",
            slug, venue_name,
        )
        return None

    # ── Company page direct-link extractor ───────────────────────────────────

    def _scrape_company_page(
        self,
        company_slug: str,
        source_label: str,
        venue_name_override: str | None,
        cat_hint: str | None,
        lock: bool,
        all_events: list[dict[str, Any]],
        seen_urls: set[str],
        start_url: str | None = None,
        skip_geo_filter: bool = True,
        kids: bool = False,
    ) -> None:
        """Scrape a TicketPlus company page (or any TP listing URL) by extracting
        all /events/{slug} links directly from the HTML, then fetching each detail page.

        Pages with static event links (company pages, states/region-metropolitana)
        work well here.  Pagination follows rel="next" links.

        Args:
            start_url:       Full URL to start from.  When None, resolved via
                             _resolve_company_url (tries company page, then search).
            skip_geo_filter: False = apply SANTIAGO_TOKENS filter on event address.
                             True (default) = company/RM pages are already RM-scoped.
        """
        if start_url:
            url: str | None = start_url
        else:
            url = self._resolve_company_url(
                company_slug, venue_name_override or company_slug
            )
        if url is None:
            return
        page = 1

        logger.info("[ticketplus] Scraping page %r — %s", source_label, url)

        while url and page <= self.max_pages:
            soup = self._get_soup(url)
            if soup is None:
                break

            # Collect every unique /events/{slug} link on this page
            page_urls: list[str] = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.match(r"^/events/([^/?#]+)", href)
                if not m:
                    continue
                full = f"{BASE_URL}/events/{m.group(1)}"
                if full not in seen_urls and full not in page_urls:
                    page_urls.append(full)

            if not page_urls:
                logger.info(
                    "[ticketplus] [%s] No event links on page %d — stopping",
                    source_label, page,
                )
                break

            page_new = 0
            for detail_url in page_urls:
                if self.max_events and len(all_events) >= self.max_events:
                    break

                time.sleep(REQUEST_DELAY)
                detail = self.fetch_event_detail(detail_url)

                if not detail.get("date"):
                    logger.debug(
                        "[ticketplus] [%s] No date for %s — skipping", source_label, detail_url
                    )
                    continue

                ev: dict[str, Any] = {
                    "url":        detail_url,
                    "source_url": detail_url,
                    "_source_label": source_label,
                }
                ev.update(detail)

                # Name: prefer detail page extraction; fall back to URL slug
                if ev.get("name"):
                    ev["name"] = _clean_title(ev["name"])
                else:
                    slug_name = detail_url.rstrip("/").split("/")[-1].replace("-", " ").title()
                    ev["name"] = _clean_title(slug_name)

                if venue_name_override:
                    ev["venue_name"] = venue_name_override

                # Optional geo filter (used for the RM catch-all page to drop
                # any stray non-RM event that slipped into the listing)
                if not skip_geo_filter:
                    loc_hint = (
                        ev.get("venue_name", "") + " "
                        + ev.get("address", "") + " "
                        + ev.get("name", "")
                    )
                    if not _is_santiago(loc_hint):
                        logger.debug(
                            "[ticketplus] [%s] Non-RM event %r — skipping",
                            source_label, ev.get("name"),
                        )
                        seen_urls.add(detail_url)   # avoid re-fetching on next run
                        continue

                if cat_hint:
                    ev.setdefault("category", cat_hint)
                    if lock:
                        ev["_locked_category"] = cat_hint
                if kids:
                    ev["kids_friendly"] = True

                ev["_source_label"] = source_label
                seen_urls.add(detail_url)
                all_events.append(ev)
                page_new += 1

            logger.info(
                "[ticketplus] [%s] page %d: %d new events (total so far: %d)",
                source_label, page, page_new, len(all_events),
            )

            if self.max_events and len(all_events) >= self.max_events:
                break

            next_url = self._next_page_url(soup, url)
            if not next_url or next_url == url:
                break
            url = next_url
            page += 1
            time.sleep(REQUEST_DELAY)

    # ── /events/search.json API crawler (used for GAM subdomain) ─────────────

    @staticmethod
    def _parse_search_date(date_str: str) -> tuple[str | None, str | None]:
        """Parse a TicketPlus search.json date string like 'Jueves 16 de Abril 19:30'.

        Returns (iso_date, time_start) or (None, None).
        """
        if not date_str:
            return None, None
        t_m = re.search(r"(\d{1,2}):(\d{2})", date_str)
        time_start = f"{int(t_m.group(1)):02d}:{t_m.group(2)}" if t_m else None
        # Remove day-of-week prefix and time
        clean = re.sub(r"^\s*(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+", "", date_str, flags=re.I)
        clean = re.sub(r"\d{1,2}:\d{2}", "", clean).strip()
        iso_date = _parse_date_es(clean)
        return iso_date, time_start

    def _scrape_from_search_json(
        self,
        base_url: str,
        source_label: str,
        venue_name_override: str | None,
        cat_hint: str | None,
        lock: bool,
        all_events: list[dict[str, Any]],
        seen_urls: set[str],
        fetch_details: bool = True,
    ) -> None:
        """Fetch events from /events/search.json API and append to all_events.

        Designed for company subdomains (gam.ticketplus.cl) where the JSON API
        returns the complete event listing without pagination.

        Args:
            base_url:            Subdomain base (e.g. https://gam.ticketplus.cl).
            fetch_details:       If True, also GET each event's detail page for
                                 description.  Adds ~2s per event but enriches data.
        """
        api_url = f"{base_url.rstrip('/')}/events/search.json"
        logger.info("[ticketplus] Scraping search.json %r — %s", source_label, api_url)

        try:
            resp = self.session.get(api_url, timeout=30)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            logger.error("[ticketplus] search.json fetch failed (%s): %s", source_label, exc)
            return

        logger.info("[ticketplus] [%s] search.json returned %d results", source_label, len(results))
        new_count = 0

        for raw in results:
            url = (raw.get("url") or "").strip()
            if not url or url in seen_urls:
                continue

            title = (raw.get("title") or "").strip()
            if not title:
                continue

            iso_date, time_start = self._parse_search_date(raw.get("date", ""))
            if not iso_date:
                logger.debug("[ticketplus] [%s] No date for %r — skipping", source_label, title)
                continue

            ev: dict[str, Any] = {
                "name":       title,
                "url":        url,
                "source_url": url,
                "date":       iso_date,
                "_source_label": source_label,
            }
            if time_start:
                ev["time_start"] = time_start
            if raw.get("img"):
                ev["image_url"] = raw["img"]
            price_raw = raw.get("price")
            if price_raw is not None:
                try:
                    p = float(price_raw)
                    ev["price_range"] = [0.0, 0.0] if p == 0 else [p, p]
                except (ValueError, TypeError):
                    pass
            if raw.get("location"):
                ev["address"] = raw["location"]

            # Optionally enrich with detail page (description, structured venue, exact price)
            if fetch_details:
                time.sleep(REQUEST_DELAY)
                detail = self.fetch_event_detail(url)
                for key, val in detail.items():
                    ev.setdefault(key, val)

            # Force venue_name so enricher matches the correct DB row
            if venue_name_override:
                ev["venue_name"] = venue_name_override

            if cat_hint:
                ev.setdefault("category", cat_hint)
                if lock:
                    ev["_locked_category"] = cat_hint

            seen_urls.add(url)
            all_events.append(ev)
            new_count += 1

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[ticketplus] [%s] search.json: %d new events added", source_label, new_count)

    # ── Core listing crawler (shared by taxon pages, company pages, subdomain) ──

    def _scrape_source(
        self,
        start_url: str,
        source_label: str,
        cat_hint: str | None,
        lock: bool,
        kids: bool,
        venue_name_override: str | None,
        skip_geo_filter: bool,
        all_events: list[dict[str, Any]],
        seen_urls: set[str],
        base_url_for_links: str = BASE_URL,
    ) -> None:
        """Paginate one listing source and append collected events to all_events.

        Args:
            start_url:           First page URL of the listing.
            source_label:        Human-readable label for logs / per-source stats.
            cat_hint:            Category to apply (setdefault) on each event.
            lock:                If True, set _locked_category so classifier can't override.
            kids:                Set kids_friendly=True on every event.
            venue_name_override: Force this venue_name before enrichment.  Used for
                                 company pages and the GAM subdomain so the enricher
                                 resolves the correct DB venue instead of guessing.
            skip_geo_filter:     Skip the Santiago region filter (company/subdomain
                                 pages already represent known RM venues).
            all_events:          Shared accumulator list (mutated in-place).
            seen_urls:           Shared dedup set (mutated in-place).
            base_url_for_links:  Base URL used to resolve relative hrefs on the page.
        """
        url: str | None = start_url
        page = 1

        logger.info("[ticketplus] Scraping source %r — %s", source_label, start_url)

        while url and page <= self.max_pages:
            logger.info("[ticketplus] [%s] page %d: %s", source_label, page, url)
            soup = self._get_soup(url)
            if soup is None:
                break

            if self.debug and page == 1:
                self._print_debug(soup, source_label)
                break

            cards = self._find_cards(soup)
            if not cards:
                logger.warning(
                    "[ticketplus] [%s] No cards on page %d — stopping",
                    source_label, page,
                )
                break

            page_new = 0
            for card in cards:
                ev = self._parse_card(card, base_url=base_url_for_links)
                if ev is None:
                    continue

                detail_url = ev.get("url", "")
                if not detail_url or detail_url in seen_urls:
                    continue

                # Fetch detail page for date, time, venue, description, price
                detail = self.fetch_event_detail(detail_url)
                for key, val in detail.items():
                    ev.setdefault(key, val)

                # Mandatory: skip if no date could be extracted
                if not ev.get("date"):
                    logger.debug(
                        "[ticketplus] No date for %r — skipping", ev.get("name")
                    )
                    continue

                # Force venue_name for company/subdomain pages so the enricher
                # matches the correct DB row and never falls back to a wrong venue.
                if venue_name_override:
                    ev["venue_name"] = venue_name_override

                # Región Metropolitana filter (skipped for known RM venues)
                if not skip_geo_filter:
                    location_hint = (
                        ev.get("venue_name", "")
                        + " "
                        + ev.get("address", "")
                        + " "
                        + ev.get("name", "")
                    )
                    if not _is_santiago(location_hint):
                        logger.debug(
                            "[ticketplus] Non-Santiago event %r — skipping",
                            ev.get("name"),
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

                # Tag for per-source stats (stripped by deduplicator before DB write)
                ev["_source_label"] = source_label

                all_events.append(ev)
                page_new += 1

                if self.max_events and len(all_events) >= self.max_events:
                    logger.info("[ticketplus] Reached max_events=%d", self.max_events)
                    break

            logger.info(
                "[ticketplus] [%s] page %d: %d new events (total so far: %d)",
                source_label, page, page_new, len(all_events),
            )

            if self.max_events and len(all_events) >= self.max_events:
                break

            next_url = self._next_page_url(soup, url)
            if not next_url or next_url == url:
                break
            url = next_url
            page += 1
            time.sleep(REQUEST_DELAY)

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all Santiago/RM events from TicketPlus.

        Execution order (seen_urls deduplicates across all sources):

          1. Company pages  — venue_name forced → correct venue_id in DB
          2. /states/region-metropolitana — catch-all for events not covered
             by company pages; enricher resolves venue from JSON-LD
          3. GAM subdomain  — /events/search.json API, venue forced to "GAM"

        Returns a flat list of event dicts ready for classifier + enricher.
        A SIGALRM fires after FETCH_TIMEOUT_SECONDS (5 min) to prevent hangs.
        """
        all_events: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(FETCH_TIMEOUT_SECONDS)
        try:
            # ── 1. Company pages (explicit venue_name override) ───────────────────
            for slug, venue_name, cat_hint, lock in COMPANY_CONFIG:
                if self.max_events and len(all_events) >= self.max_events:
                    break
                self._scrape_company_page(
                    company_slug=slug,
                    source_label=f"company:{slug}",
                    venue_name_override=venue_name,
                    cat_hint=cat_hint,
                    lock=lock,
                    all_events=all_events,
                    seen_urls=seen_urls,
                )

            # ── 2. /states/region-metropolitana — RM catch-all ───────────────────
            if STATES_RM_URL and not (self.max_events and len(all_events) >= self.max_events):
                self._scrape_company_page(
                    company_slug="",          # unused — start_url overrides it
                    source_label="states-rm",
                    venue_name_override=None, # enricher resolves from JSON-LD
                    cat_hint=None,
                    lock=False,
                    all_events=all_events,
                    seen_urls=seen_urls,
                    start_url=STATES_RM_URL,
                    skip_geo_filter=True,     # /states/region-metropolitana is already RM-scoped
                )

            # ── 3. GAM subdomain — /events/search.json API ────────────────────────
            if GAM_SUBDOMAIN and not (self.max_events and len(all_events) >= self.max_events):
                self._scrape_from_search_json(
                    base_url=GAM_SUBDOMAIN,
                    source_label="gam-subdomain",
                    venue_name_override=GAM_VENUE_NAME,
                    cat_hint=None,
                    lock=False,
                    all_events=all_events,
                    seen_urls=seen_urls,
                )

        except _FetchTimeout:
            logger.warning(
                "[ticketplus] fetch_events timed out after %ds — returning %d events "
                "collected so far", FETCH_TIMEOUT_SECONDS, len(all_events),
            )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

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

    _ALL_SOURCES = (
        [f"company:{s}" for s, *_ in COMPANY_CONFIG]
        + ["states-rm", "gam-subdomain"]
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
        "--source",
        choices=_ALL_SOURCES,
        help="Scrape only this source (default: all sources)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=10,
        help="Maximum listing pages per source",
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

    # Filter to one source if requested.
    # Must mutate module-level globals in-place (or rebind them here in __main__)
    # so that fetch_events(), which runs in __main__'s global scope, sees the change.
    # Using _self.COMPANY_CONFIG = … would modify scrapers.ticketplus_scraper's
    # namespace — a different dict — and be silently ignored.
    if args.source:
        if args.source.startswith("company:"):
            slug = args.source[len("company:"):]
            COMPANY_CONFIG[:] = [c for c in COMPANY_CONFIG if c[0] == slug]
            STATES_RM_URL = ""   # noqa: F841  rebinds __main__ global
            GAM_SUBDOMAIN  = ""  # noqa: F841
        elif args.source == "states-rm":
            COMPANY_CONFIG[:] = []
            GAM_SUBDOMAIN  = ""  # noqa: F841
        elif args.source == "gam-subdomain":
            COMPANY_CONFIG[:] = []
            STATES_RM_URL = ""   # noqa: F841

    scraper = TicketPlusScraper(
        max_pages=args.max_pages,
        max_events=args.max_events,
        debug=args.debug,
    )
    events = scraper.fetch_events()

    if args.dry_run or args.debug:
        print(f"\n── TicketPlus dry-run: {len(events)} events ────────────────")
        for ev in events[:15]:
            print(
                f"\n  name      : {ev.get('name')!r}\n"
                f"  source    : {ev.get('_source_label')}\n"
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
        from collections import defaultdict

        engine, db = make_scraper_session()
        now = datetime.now(timezone.utc)
        totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        per_source: dict[str, dict[str, int]] = defaultdict(
            lambda: {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        )

        for ev in events:
            src = ev.get("_source_label", "unknown")
            try:
                ev = classifier.classify(ev)
                ev = enricher.enrich(ev, db)
                ev.setdefault("scraped_at", now)
                ev.setdefault("is_verified", False)
                result = deduplicator.save_or_update(ev, db)
                totals[result] += 1
                per_source[src][result] += 1
            except Exception as exc:
                logger.warning("Failed to save %r: %s", ev.get("name"), exc)
                db.rollback()
                totals["failed"] += 1
                per_source[src]["failed"] += 1

        db.commit()
        db.close()
        engine.dispose()

        print("\n── TicketPlus results per source ──────────────────────────────────────")
        print(f"{'Source':<35} {'created':>8} {'updated':>8} {'skipped':>8} {'failed':>7}")
        print("-" * 70)
        for src in sorted(per_source):
            s = per_source[src]
            print(f"{src:<35} {s['created']:>8} {s['updated']:>8} {s['skipped']:>8} {s['failed']:>7}")
        print("-" * 70)
        print(
            f"{'TOTAL':<35} {totals['created']:>8} {totals['updated']:>8} "
            f"{totals['skipped']:>8} {totals['failed']:>7}"
        )
