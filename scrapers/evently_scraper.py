"""Evently Chile scraper — fetches events from evently.cl listing and category pages.

Evently Chile (evently.cl) is a Next.js app with server-side rendering.
Every page embeds a <script id="__NEXT_DATA__"> tag containing the full
page props as JSON — this is extracted first and is the primary data source.
HTML card parsing is used as a fallback when __NEXT_DATA__ has no event list.

Listing pages scraped:
    https://www.evently.cl/?c=CL              (all events)
    https://www.evently.cl/?cat=dance&c=CL
    https://www.evently.cl/?cat=music&c=CL
    https://www.evently.cl/?cat=comedy&c=CL

Event detail pages live on organiser subdomains:
    https://{organizer}.evently.cl/{event-slug}

Each detail page also embeds __NEXT_DATA__ with full event metadata
(date, time, venue, description, price, image, coordinates).

Only Santiago / Región Metropolitana events are kept (SANTIAGO_TOKENS).

source_url:
    evently:cl:{organizer}:{slug}
    — uses the organiser subdomain + event slug, stable across re-scrapes.
    Falls back to the full detail URL when slug is not available.

Run:
    python scrapers/evently_scraper.py --dry-run
    python scrapers/evently_scraper.py --dry-run --verbose
    python scrapers/evently_scraper.py --category music --dry-run
    python scrapers/evently_scraper.py --debug
    python scrapers/evently_scraper.py --raw --category comedy
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
from urllib.parse import urlparse, urljoin

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

BASE_URL     = "https://www.evently.cl"
BASE_DOMAIN  = "evently.cl"

# (url_params, category_hint, lock_category)
# lock_category=True → _locked_category sentinel set; classifier cannot override.
LISTING_PAGES: list[tuple[str, str | None, bool]] = [
    ("?c=CL",             None,       False),   # all categories → classifier decides
    ("?cat=music&c=CL",   "Música",   True),
    ("?cat=comedy&c=CL",  "Comedia",  True),
    ("?cat=dance&c=CL",   None,       False),   # dance → let classifier pick category
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


# ── URL helpers ───────────────────────────────────────────────────────────────

def _is_evently_event_url(href: str) -> bool:
    """Return True if href looks like an Evently event detail URL.

    Valid shapes:
      https://organizer.evently.cl/event-slug
      https://organizer.evently.cl/event-slug/
      //organizer.evently.cl/event-slug

    Excluded non-event subdomains: www, app, admin, api, cdn, static.
    """
    if not href:
        return False
    try:
        parsed = urlparse(href if href.startswith("http") else "https:" + href)
        host = parsed.netloc.lower()
        path = parsed.path.strip("/")
        _EXCLUDED = ("www.", "app.", "admin.", "api.", "cdn.", "static.", "assets.")
        return (
            host.endswith("." + BASE_DOMAIN)
            and not any(host.startswith(s) for s in _EXCLUDED)
            and bool(path)
        )
    except Exception:
        return False


def _normalise_event_url(href: str) -> str:
    """Return a canonical https:// event URL."""
    if href.startswith("//"):
        return "https:" + href
    return href


def _organizer_and_slug(url: str) -> tuple[str, str]:
    """Extract (organizer_subdomain, event_slug) from an event URL.

    e.g. "https://foofest.evently.cl/gran-concierto" → ("foofest", "gran-concierto")
    Returns ("", "") on parse failure.
    """
    try:
        p = urlparse(url)
        organizer = p.netloc.split("." + BASE_DOMAIN)[0].lower()
        slug      = p.path.strip("/").split("/")[0]
        return organizer, slug
    except Exception:
        return "", ""


def _date_from_url_slug(url: str) -> str | None:
    """Extract a date from an Evently event URL slug.

    Evently appends dates to slugs in two formats:
      DD-MM-YYYY at the end:  .../Event-Name-06-05-2026  → 2026-05-06
      Day-N-Month-Name:       .../Miercoles-6-De-Mayo-…  → 2026-05-06

    Tries ISO-format end pattern first (most reliable), then Spanish month.
    """
    try:
        slug = urlparse(url).path.strip("/").split("/")[-1]
    except Exception:
        return None
    if not slug:
        return None

    # Pattern 1: ends with DD-MM-YYYY
    m = re.search(r"[-_](\d{2})-(\d{2})-(\d{4})$", slug, re.I)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # Pattern 2: N-MonthName anywhere in slug (e.g. "6-De-Mayo" or "6-mayo")
    slug_lower = slug.lower().replace("-de-", "-")
    m = re.search(
        r"(\d{1,2})[-_](enero|febrero|marzo|abril|mayo|junio|julio|agosto"
        r"|septiembre|octubre|noviembre|diciembre)",
        slug_lower,
    )
    if m:
        day  = int(m.group(1))
        mon  = _MONTHS_ES.get(m.group(2))
        if mon and 1 <= day <= 31:
            from datetime import date as _date
            year = _date.today().year
            # If the resulting date is in the past, bump to next year
            try:
                d = _date(year, mon, day)
                if d < _date.today():
                    d = _date(year + 1, mon, day)
                return d.isoformat()
            except ValueError:
                pass

    return None


def _build_source_url(url: str) -> str:
    organizer, slug = _organizer_and_slug(url)
    if organizer and slug:
        return f"evently:cl:{organizer}:{slug}"
    return url


def _add_page_param(base: str, page: int) -> str:
    """Append or replace ?page=N in a URL."""
    if "?page=" in base or "&page=" in base:
        return re.sub(r"([?&])page=\d+", lambda m: m.group(1) + f"page={page}", base)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page}"


# ── __NEXT_DATA__ extraction ──────────────────────────────────────────────────

def _next_data(soup: BeautifulSoup) -> dict:
    """Return the parsed __NEXT_DATA__ JSON object, or {} on failure."""
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag:
        return {}
    try:
        return json.loads(tag.string or "")
    except (json.JSONDecodeError, TypeError):
        return {}


def _deep_get(obj: Any, *keys: str) -> Any:
    """Safely traverse a nested dict/list structure."""
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int):
            obj = obj[key] if key < len(obj) else None
        else:
            return None
        if obj is None:
            return None
    return obj


def _extract_event_list_from_next_data(nd: dict) -> list[dict]:
    """Pull the event list out of __NEXT_DATA__ page props.

    Evently may nest the list under several possible key paths.
    We try the most likely paths first, then do a recursive search.
    """
    props = nd.get("props", {}).get("pageProps", {})

    # Direct common keys
    for key in ("events", "items", "data", "results", "eventList", "event_list"):
        val = props.get(key)
        if isinstance(val, list) and val:
            return val
        # one level deeper: {"data": {"events": [...]}}
        if isinstance(val, dict):
            for inner in ("events", "items", "data", "results"):
                inner_val = val.get(inner)
                if isinstance(inner_val, list) and inner_val:
                    return inner_val

    # Recursive search: walk props for the first list of dicts that looks like events
    def _search(obj: Any, depth: int = 0) -> list[dict] | None:
        if depth > 5:
            return None
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            # A list of dicts — check if any item has event-like keys
            first = obj[0]
            event_keys = {"name", "title", "slug", "date", "venue", "image", "price"}
            if len(event_keys & set(first.keys())) >= 2:
                return obj
        if isinstance(obj, dict):
            for v in obj.values():
                found = _search(v, depth + 1)
                if found:
                    return found
        return None

    found = _search(props)
    return found or []


# ── Field extraction from a Next.js event dict ───────────────────────────────

def _get_str(obj: dict, *keys: str) -> str:
    for k in keys:
        v = obj.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _parse_date_safe(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    iso = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if iso:
        return iso.group(1)
    first = raw.split(" - ")[0].split(" al ")[0].strip()
    return _parse_date_es(first)


def _parse_time_safe(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", raw.strip())
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _event_from_next_dict(raw: dict, listing_url: str) -> dict[str, Any] | None:
    """Convert a raw Evently event dict (from __NEXT_DATA__) to a pipeline dict.

    Returns None if mandatory fields (name + link) cannot be extracted.
    """
    # ── Name ──────────────────────────────────────────────────────────────────
    name = _get_str(raw, "name", "title", "event_name", "nombre", "titulo")
    if not name:
        return None

    # ── Event detail URL ─────────────────────────────────────────────────────
    # Try direct URL fields first
    url = ""
    for key in ("url", "link", "href", "event_url", "permalink"):
        val = raw.get(key)
        if val and isinstance(val, str) and _is_evently_event_url(val):
            url = _normalise_event_url(val)
            break

    # Build from organizer subdomain + slug if no direct URL
    if not url:
        organizer = _get_str(raw, "organizer_subdomain", "subdomain", "organizer_slug")
        if not organizer:
            org = raw.get("organizer") or raw.get("organiser") or {}
            if isinstance(org, dict):
                organizer = _get_str(org, "subdomain", "slug", "username")
        slug = _get_str(raw, "slug", "event_slug", "path")
        if organizer and slug:
            url = f"https://{organizer}.{BASE_DOMAIN}/{slug}"

    if not url:
        return None

    source_url = _build_source_url(url)

    # ── Date + time ───────────────────────────────────────────────────────────
    raw_date = _get_str(raw, "date", "start_date", "date_start", "fecha", "event_date", "startDate")
    date_str = _parse_date_safe(raw_date)

    raw_time = _get_str(raw, "time", "time_start", "start_time", "hora", "startTime")
    if not raw_time and " " in (raw_date or ""):
        raw_time = raw_date.split(" ", 1)[1]
    time_start = _parse_time_safe(raw_time)
    # Also try ISO datetime: "2026-04-23T20:00:00"
    if not time_start and raw_date:
        time_start = _parse_time_safe(re.sub(r"^\d{4}-\d{2}-\d{2}", "", raw_date))

    # ── Venue ─────────────────────────────────────────────────────────────────
    venue_name = ""
    venue_obj = raw.get("venue") or raw.get("location") or {}
    if isinstance(venue_obj, dict):
        venue_name = _get_str(venue_obj, "name", "venue_name", "title", "place")
        # Grab coordinates if present
    elif isinstance(venue_obj, str):
        venue_name = venue_obj.strip()

    if not venue_name:
        venue_name = _get_str(raw, "venue_name", "venue", "lugar", "recinto", "location_name")

    # ── Image ─────────────────────────────────────────────────────────────────
    image_url = None
    for key in ("image", "image_url", "imageUrl", "cover_image", "thumbnail", "banner", "poster", "img"):
        val = raw.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            image_url = val
            break
        if isinstance(val, dict):
            for sub in ("url", "src", "href"):
                inner = val.get(sub)
                if inner and isinstance(inner, str) and inner.startswith("http"):
                    image_url = inner
                    break
            if image_url:
                break

    # ── Description ───────────────────────────────────────────────────────────
    description = None
    for key in ("description", "descripcion", "about", "detail", "info", "summary", "body"):
        val = raw.get(key)
        if val and isinstance(val, str) and len(val.strip()) > 10:
            description = val.strip()[:1500]
            break

    # ── Price ─────────────────────────────────────────────────────────────────
    price_range = None
    for key in ("price", "price_range", "precio", "min_price", "ticket_price", "prices"):
        val = raw.get(key)
        if val is not None:
            price_range = _parse_price(val)
            if price_range:
                break

    # ── Category ──────────────────────────────────────────────────────────────
    raw_category = _get_str(raw, "category", "categoria", "type", "genre", "tag")

    # ── Assemble ──────────────────────────────────────────────────────────────
    ev: dict[str, Any] = {
        "name":       name,
        "url":        url,
        "source_url": source_url,
        "venue_name": venue_name,
    }
    if date_str:
        ev["date"] = date_str
    if time_start:
        ev["time_start"] = time_start
    if image_url:
        ev["image_url"] = image_url
    if description:
        ev["description"] = description
    if price_range is not None:
        ev["price_range"] = price_range
    if raw_category:
        ev["_raw_category"] = raw_category   # passed to classifier as a hint

    return ev


# ── HTML card parsing (fallback) ──────────────────────────────────────────────

def _find_cards(soup: BeautifulSoup) -> list[Any]:
    """Find event card elements on a listing page (HTML fallback).

    Evently migrated to Next.js App Router — __NEXT_DATA__ is no longer
    injected on listing or detail pages.  The current layout renders event
    cards as bare <a> tags pointing to organizer subdomains; there are no
    semantic card class names.

    Strategy (highest-confidence first):
      1. Named card CSS classes — kept for forward-compatibility.
      2. Collect every <a> that points to an organiser subdomain and return
         it directly.  The parent div is tried first (contains the <img>),
         but we cap at 1 level to avoid climbing to a shared page container.
    """
    # 1. Named card components
    for cls_pat in (
        re.compile(r"event[_-]?card|EventCard|eventCard", re.I),
        re.compile(r"event[_-]?item|EventItem", re.I),
        re.compile(r"event[_-]?tile|EventTile", re.I),
        re.compile(r"card[_-]?event", re.I),
    ):
        cards = soup.find_all(["div", "article", "li", "a"], class_=cls_pat)
        if cards:
            return cards

    # 2. Event <a> tags — use parent div only if it is a narrow single-child wrapper.
    seen: set[str] = set()
    cards: list[Any] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _is_evently_event_url(href):
            continue
        if href in seen:
            continue
        seen.add(href)
        # Try 1-level parent as the card container; fall back to <a> itself.
        parent = a.parent
        if (
            parent
            and parent.name in ("div", "article", "li")
            and len(parent.find_all("a", href=_is_evently_event_url)) == 1
        ):
            cards.append(parent)
        else:
            cards.append(a)

    return cards


def _parse_card(card: Any) -> dict[str, Any] | None:
    """Extract a stub event dict from an HTML listing card.

    With the current Evently App Router layout the listing cards are bare
    <a> tags containing only an image — no heading text.  The name is
    derived from the URL slug as a placeholder; the detail-page OG title
    will override it during the detail fetch.
    """
    ev: dict[str, Any] = {}

    # URL
    link = card.find("a", href=_is_evently_event_url) or (
        card if card.name == "a" and _is_evently_event_url(card.get("href", "")) else None
    )
    if not link:
        return None
    href = _normalise_event_url(link.get("href", ""))
    if not href:
        return None
    ev["url"] = href
    ev["source_url"] = _build_source_url(href)

    # Title — heading elements (present in older layouts)
    for tag in ("h1", "h2", "h3", "h4"):
        el = card.find(tag)
        if el:
            text = el.get_text(strip=True)
            if text:
                ev["name"] = text
                break
    if not ev.get("name"):
        for cls_pat in (
            re.compile(r"event[_-]?(?:name|title)|title[_-]?event", re.I),
        ):
            el = card.find(class_=cls_pat)
            if el:
                text = el.get_text(strip=True)
                if text:
                    ev["name"] = text
                    break

    # Title fallback — derive from URL slug (detail OG title will override)
    if not ev.get("name"):
        from urllib.parse import urlparse as _urlparse
        slug = _urlparse(href).path.strip("/").split("/")[0]
        # Strip trailing DD-MM-YYYY date component
        slug = re.sub(r"-\d{2}-\d{2}-\d{4}$", "", slug)
        name = slug.replace("-", " ").replace("_", " ").strip()
        if name:
            ev["name"] = name

    if not ev.get("name"):
        return None

    # Image
    img = card.find("img")
    if img:
        src = img.get("data-src") or img.get("src") or img.get("data-lazy-src")
        if src:
            # Decode Next.js image optimisation wrapper /_next/image?url=...
            if src and "/_next/image" in src:
                from urllib.parse import parse_qs, unquote as _unquote, urlparse as _up
                qs = parse_qs(_up(src).query)
                decoded = qs.get("url", [None])[0]
                if decoded:
                    src = _unquote(decoded)
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                ev["image_url"] = src

    # Date (listing cards may show a <time> element)
    time_el = card.find("time")
    if time_el:
        raw = time_el.get("datetime") or time_el.get_text(strip=True)
        d = _parse_date_safe(raw)
        if d:
            ev["date"] = d
        t = _parse_time_safe(raw)
        if t:
            ev["time_start"] = t

    if not ev.get("date"):
        for cls_pat in (re.compile(r"fecha|date|when|dia", re.I),):
            el = card.find(class_=cls_pat)
            if el:
                d = _parse_date_safe(el.get("datetime") or el.get_text(strip=True))
                if d:
                    ev["date"] = d
                    break

    return ev


# ── Detail page extraction ────────────────────────────────────────────────────

def _event_from_detail_next_data(nd: dict) -> dict[str, Any]:
    """Extract event fields from a detail page __NEXT_DATA__ object."""
    result: dict[str, Any] = {}
    props = nd.get("props", {}).get("pageProps", {})

    # The event data may live under various keys
    event_obj: dict = {}
    for key in ("event", "eventData", "data", "item", "product"):
        val = props.get(key)
        if isinstance(val, dict):
            event_obj = val
            break
    if not event_obj:
        # flat pageProps
        event_obj = props

    if not event_obj:
        return result

    # Date + time
    raw_date = _get_str(
        event_obj,
        "date", "start_date", "date_start", "fecha", "startDate", "event_date",
    )
    d = _parse_date_safe(raw_date)
    if d:
        result["date"] = d
    raw_time = _get_str(
        event_obj,
        "time", "time_start", "start_time", "hora", "startTime",
    )
    if not raw_time and " " in (raw_date or ""):
        raw_time = raw_date.split(" ", 1)[1]
    t = _parse_time_safe(raw_time)
    if t:
        result["time_start"] = t
    if not t and raw_date:
        t2 = _parse_time_safe(re.sub(r"^\d{4}-\d{2}-\d{2}", "", raw_date))
        if t2:
            result["time_start"] = t2

    # End time
    raw_end = _get_str(event_obj, "end_time", "time_end", "endTime", "end_date")
    t_end = _parse_time_safe(raw_end)
    if t_end:
        result["time_end"] = t_end

    # Venue
    venue_obj = event_obj.get("venue") or event_obj.get("location") or {}
    if isinstance(venue_obj, dict):
        vname = _get_str(venue_obj, "name", "venue_name", "title", "place")
        if vname:
            result["venue_name"] = vname
        coords = venue_obj.get("coordinates") or venue_obj.get("coords")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            result["coordinates"] = list(coords[:2])
        elif isinstance(venue_obj.get("lat"), (int, float)):
            result["coordinates"] = [venue_obj["lat"], venue_obj.get("lng", 0)]
        address_parts = [
            venue_obj.get("address") or venue_obj.get("street") or "",
            venue_obj.get("city") or "",
        ]
        addr = " ".join(p for p in address_parts if p).strip()
        if addr:
            result["address"] = addr
    elif isinstance(venue_obj, str) and venue_obj.strip():
        result["venue_name"] = venue_obj.strip()

    if "venue_name" not in result:
        vn = _get_str(event_obj, "venue_name", "venue", "lugar", "recinto", "location_name")
        if vn:
            result["venue_name"] = vn

    # Description
    for key in ("description", "descripcion", "about", "detail", "info", "body", "summary"):
        val = event_obj.get(key)
        if val and isinstance(val, str) and len(val.strip()) > 10:
            result["description"] = val.strip()[:1500]
            break

    # Price
    for key in ("price", "price_range", "precio", "min_price", "ticket_price"):
        val = event_obj.get(key)
        if val is not None:
            pr = _parse_price(val)
            if pr is not None:
                result["price_range"] = pr
                break

    # Image
    for key in ("image", "image_url", "imageUrl", "cover_image", "thumbnail", "banner", "poster"):
        val = event_obj.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            result["image_url"] = val
            break
        if isinstance(val, dict):
            for sub in ("url", "src"):
                inner = val.get(sub)
                if inner and isinstance(inner, str) and inner.startswith("http"):
                    result["image_url"] = inner
                    break
            if "image_url" in result:
                break

    # Category
    raw_cat = _get_str(event_obj, "category", "categoria", "type", "genre")
    if raw_cat:
        result["_raw_category"] = raw_cat

    return result


# ── Scraper class ─────────────────────────────────────────────────────────────

class EventlyScraper(BaseScraper):
    """Scrapes events from Evently Chile listing and category pages."""

    name = "evently"

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
            logger.error("[evently] GET %s failed: %s", url, exc)
            return None

    # ── Listing page processing ───────────────────────────────────────────────

    def _events_from_listing(
        self, soup: BeautifulSoup, listing_url: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Extract stub event dicts from a listing page.

        Returns (events, used_next_data).
        used_next_data=True means __NEXT_DATA__ was the source (richer data,
        no detail fetch needed for most fields).
        used_next_data=False means HTML cards were used (detail fetch required).
        """
        nd = _next_data(soup)
        raw_list = _extract_event_list_from_next_data(nd)

        if raw_list:
            logger.debug(
                "[evently] __NEXT_DATA__ yielded %d raw event dicts", len(raw_list)
            )
            events = []
            for raw in raw_list:
                ev = _event_from_next_dict(raw, listing_url)
                if ev:
                    events.append(ev)
            return events, True

        # Fallback: parse HTML cards
        logger.debug("[evently] __NEXT_DATA__ had no event list — falling back to HTML")
        cards = _find_cards(soup)
        events = [ev for card in cards if (ev := _parse_card(card))]
        return events, False

    # ── Detail page ───────────────────────────────────────────────────────────

    def fetch_event_detail(self, url: str) -> dict[str, Any]:
        """Fetch an event detail page and return enriched field dict.

        Detail pages are on organiser subdomains:
            https://{organizer}.evently.cl/{event-slug}

        Primary source: __NEXT_DATA__ JSON (rich structured data).
        Fallback: OpenGraph meta + DOM class selectors.
        """
        time.sleep(REQUEST_DELAY)
        soup = self._get_soup(url)
        if soup is None:
            return {}

        result: dict[str, Any] = {}

        # ── 1. __NEXT_DATA__ ──────────────────────────────────────────────────
        nd = _next_data(soup)
        if nd:
            result = _event_from_detail_next_data(nd)

        # ── 2. JSON-LD structured data ────────────────────────────────────────
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                ld = json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if item.get("@type") not in (
                        "Event", "MusicEvent", "TheaterEvent",
                        "SportsEvent", "ComedyEvent", "DanceEvent",
                        "VisualArtsEvent",
                    ):
                        continue
                    if "date" not in result:
                        start = item.get("startDate") or ""
                        d = _parse_date_safe(start)
                        if d:
                            result["date"] = d
                        t = _parse_time_safe(re.sub(r"^\d{4}-\d{2}-\d{2}", "", start))
                        if t:
                            result["time_start"] = t
                    if "venue_name" not in result:
                        loc = item.get("location") or {}
                        if isinstance(loc, dict):
                            vname = loc.get("name")
                            if vname:
                                result["venue_name"] = str(vname).strip()
                            addr_obj = loc.get("address") or {}
                            if isinstance(addr_obj, dict):
                                parts = [
                                    addr_obj.get("streetAddress", ""),
                                    addr_obj.get("addressLocality", ""),
                                ]
                                addr = " ".join(p for p in parts if p).strip()
                                if addr:
                                    result["address"] = addr
                    if "price_range" not in result:
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
                    if "description" not in result:
                        desc = (item.get("description") or "").strip()
                        if len(desc) > 10:
                            result["description"] = desc[:1500]
                    if "image_url" not in result:
                        img = item.get("image")
                        if isinstance(img, str) and img.startswith("http"):
                            result["image_url"] = img
                        elif isinstance(img, list) and img:
                            result["image_url"] = str(img[0])
                    break
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        # ── 3. OpenGraph fallback ─────────────────────────────────────────────
        if "image_url" not in result:
            og = soup.find("meta", {"property": "og:image"})
            if og and og.get("content", "").startswith("http"):
                result["image_url"] = og["content"]

        if "description" not in result:
            for attr, name in (("property", "og:description"), ("name", "description")):
                meta = soup.find("meta", {attr: name})
                if meta and meta.get("content", "").strip():
                    txt = meta["content"].strip()
                    if len(txt) > 10:
                        result["description"] = txt[:1500]
                    break

        # Extract event name and venue from og:title
        # Format: "Event Name - Venue Name | Evently"
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title:
            raw_title = og_title.get("content", "").strip()
            raw_title = raw_title.split(" | ")[0]  # drop " | Evently"
            parts = [p.strip() for p in raw_title.split(" - ") if p.strip()]
            if len(parts) >= 2 and "name" not in result:
                result["name"] = parts[0]
            if len(parts) >= 2 and "venue_name" not in result:
                result["venue_name"] = parts[-1]

        # ── 4. DOM fallback for date / venue / price ──────────────────────────
        if "date" not in result:
            time_el = soup.find("time")
            if time_el:
                raw = time_el.get("datetime") or time_el.get_text(strip=True)
                d = _parse_date_safe(raw)
                if d:
                    result["date"] = d
                t = _parse_time_safe(raw)
                if t:
                    result["time_start"] = t

        if "venue_name" not in result:
            for cls_pat in (
                re.compile(r"venue|lugar|recinto|location[_-]?name", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        result["venue_name"] = text
                        break

        if "price_range" not in result:
            for cls_pat in (re.compile(r"price|precio|ticket[_-]?price", re.I),):
                el = soup.find(class_=cls_pat)
                if el:
                    pr = _parse_price(el.get_text(strip=True))
                    if pr is not None:
                        result["price_range"] = pr
                        break

        if "description" not in result:
            for cls_pat in (
                re.compile(r"description|descripcion|event[_-]?info|about", re.I),
            ):
                el = soup.find(class_=cls_pat)
                if el:
                    txt = el.get_text(" ", strip=True)
                    if len(txt) > 30:
                        result["description"] = txt[:1500]
                        break

        return result

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page_url(
        self, soup: BeautifulSoup, current_url: str, nd: dict
    ) -> str | None:
        """Return the next listing page URL or None.

        Tries (in order):
          1. __NEXT_DATA__ pagination metadata (hasNextPage / nextPage)
          2. rel="next" link
          3. class-based "Next" / "Siguiente" anchor
          4. ?page=N auto-increment (only if page had cards)
        """
        # 1. Next.js router pagination
        props  = nd.get("props", {}).get("pageProps", {})
        paging = props.get("pagination") or props.get("meta") or props.get("paging") or {}
        if isinstance(paging, dict):
            if not paging.get("hasNextPage") and not paging.get("next_page"):
                # Explicit "no more pages" signal
                if "hasNextPage" in paging:
                    return None
            next_pg = paging.get("next_page") or paging.get("nextPage")
            if next_pg and isinstance(next_pg, int):
                return _add_page_param(current_url, next_pg)

        # 2. rel="next"
        link = soup.find("a", rel=lambda v: v and "next" in v)
        if link and link.get("href"):
            href = link["href"]
            return href if href.startswith("http") else BASE_URL + href

        # 3. Class / text "Next"
        link = (
            soup.find("a", class_=re.compile(r"\bnext\b|\bsiguiente\b", re.I))
            or soup.find("a", string=re.compile(r"siguiente|next|›|»", re.I))
        )
        if link and link.get("href"):
            href = link["href"]
            if href not in ("#", "javascript:void(0)"):
                return href if href.startswith("http") else BASE_URL + href

        # 4. ?page=N auto-increment
        m = re.search(r"[?&]page=(\d+)", current_url)
        if m:
            nxt = _add_page_param(current_url, int(m.group(1)) + 1)
            return nxt

        return None

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all Santiago events from configured listing pages.

        For each listing page:
          1. Extract event stubs from __NEXT_DATA__ (primary) or HTML cards
          2. If __NEXT_DATA__ was used and fields are complete, skip detail fetch
             unless date or venue_name is still missing
          3. If HTML cards were used, always fetch detail pages
          4. Apply Santiago/RM filter on venue_name + address
          5. Apply category hints / _locked_category sentinel

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        all_events:       list[dict[str, Any]] = []
        seen_source_urls: set[str]             = set()

        for url_params, cat_hint, lock in LISTING_PAGES:
            listing_url = f"{BASE_URL}/{url_params}"
            logger.info("[evently] Listing: %s", listing_url)

            url: str | None = listing_url
            page = 1

            while url and page <= self.max_pages:
                logger.info("[evently] page %d: %s", page, url)
                soup = self._get_soup(url)
                if soup is None:
                    break

                if self.debug and page == 1:
                    self._print_debug(soup, url_params)
                    break

                nd = _next_data(soup)
                stubs, used_nd = self._events_from_listing(soup, url)

                if not stubs:
                    logger.warning(
                        "[evently] No events on page %d (%s) — stopping", page, url_params
                    )
                    break

                page_new = 0
                for ev in stubs:
                    detail_url = ev.get("url", "")
                    if not detail_url or ev["source_url"] in seen_source_urls:
                        continue

                    # Fetch detail page when key fields are absent
                    needs_detail = (
                        not used_nd                          # HTML cards always need detail
                        or not ev.get("date")
                        or not ev.get("venue_name")
                        or not ev.get("description")
                    )
                    if needs_detail:
                        detail = self.fetch_event_detail(detail_url)
                        for key, val in detail.items():
                            # Detail OG title always wins over slug-derived placeholder names
                            if key in ("name", "venue_name") and val:
                                ev[key] = val
                            else:
                                ev.setdefault(key, val)

                    # Last resort: extract date from URL slug (DD-MM-YYYY suffix)
                    if not ev.get("date"):
                        slug_date = _date_from_url_slug(detail_url)
                        if slug_date:
                            ev["date"] = slug_date
                            logger.debug(
                                "[evently] Date from slug for %r: %s", ev.get("name"), slug_date
                            )

                    # Must have a date
                    if not ev.get("date"):
                        logger.debug(
                            "[evently] No date for %r — skipping", ev.get("name")
                        )
                        continue

                    # Santiago / RM filter
                    location_hint = " ".join(
                        str(ev.get(f, "") or "")
                        for f in ("venue_name", "address", "name")
                    )
                    if not _is_santiago(location_hint):
                        logger.debug(
                            "[evently] Non-Santiago %r — skipping", ev.get("name")
                        )
                        continue

                    seen_source_urls.add(ev["source_url"])

                    # Apply category hints from listing page
                    if cat_hint:
                        ev.setdefault("category", cat_hint)
                        if lock:
                            ev["_locked_category"] = cat_hint

                    # Promote _raw_category as a soft hint for the classifier
                    # (do not set _locked_category — classifier picks the final value)
                    raw_cat = ev.pop("_raw_category", None)
                    if raw_cat and not ev.get("category") and not ev.get("_locked_category"):
                        ev["_category_hint"] = raw_cat

                    all_events.append(ev)
                    page_new += 1

                    if self.max_events and len(all_events) >= self.max_events:
                        logger.info("[evently] Reached max_events=%d", self.max_events)
                        break

                logger.info(
                    "[evently] page %d: %d new RM events (total: %d)",
                    page, page_new, len(all_events),
                )

                if self.max_events and len(all_events) >= self.max_events:
                    break
                if page_new == 0 and page > 1:
                    break

                next_url = self._next_page_url(soup, url, nd)
                if not next_url or next_url == url:
                    break
                url = next_url
                page += 1
                time.sleep(REQUEST_DELAY)

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[evently] Total events collected: %d", len(all_events))
        return all_events

    # ── Debug helpers ─────────────────────────────────────────────────────────

    def _print_debug(self, soup: BeautifulSoup, label: str = "") -> None:
        """Print structural diagnostics for a listing page."""
        print("\n" + "=" * 70)
        print(f"DEBUG — EventlyScraper  [{label}]")
        print("=" * 70)

        title = soup.find("title")
        print(f"\nPage <title>: {title.get_text(strip=True) if title else '(none)'}")

        # __NEXT_DATA__ summary
        nd = _next_data(soup)
        if nd:
            print(f"\n__NEXT_DATA__ present — top-level keys: {list(nd.keys())}")
            props = nd.get("props", {}).get("pageProps", {})
            print(f"pageProps keys: {list(props.keys())}")
            raw_list = _extract_event_list_from_next_data(nd)
            print(f"Event list extracted from __NEXT_DATA__: {len(raw_list)} items")
            if raw_list:
                print(f"First item keys: {list(raw_list[0].keys())}")
                import pprint
                print("First item:")
                pprint.pprint(raw_list[0])
        else:
            print("\n__NEXT_DATA__ NOT FOUND — will use HTML parsing")

        # HTML card summary
        from collections import Counter
        cls_counts: Counter = Counter()
        for el in soup.find_all(["div", "article", "li", "a"]):
            for cls in el.get("class", []):
                cls_counts[cls] += 1
        print("\nTop-20 class names:")
        for cls, cnt in cls_counts.most_common(20):
            print(f"  {cnt:4d}×  .{cls}")

        event_links = [
            a["href"] for a in soup.find_all("a", href=True) if _is_evently_event_url(a["href"])
        ]
        print(f"\nEvently subdomain event links: {len(set(event_links))}")
        for href in list(set(event_links))[:5]:
            print(f"  {href}")

        cards = _find_cards(soup)
        print(f"\nHTML cards found: {len(cards)}")
        if cards:
            raw = str(cards[0])
            print(f"\nFirst card HTML (2000 chars):\n{raw[:2000]}")

        print("\n" + "=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    _CATEGORY_CHOICES = [
        p.lstrip("?").split("&")[0].replace("cat=", "") or "all"
        for p, *_ in LISTING_PAGES
    ]

    parser = argparse.ArgumentParser(description="Evently Chile scraper")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and print sample events — no DB writes",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print first-page structure for each listing URL and exit",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="Pretty-print the raw __NEXT_DATA__ for the first listing page and exit",
    )
    parser.add_argument(
        "--category",
        choices=["all", "music", "comedy", "dance"],
        help="Scrape only this category (default: all)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=5,
        help="Maximum listing pages per category",
    )
    parser.add_argument(
        "--max-events", type=int, default=0,
        help="Stop after this many events (0 = unlimited)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print price, image, and description for each sample event",
    )
    args = parser.parse_args()

    # Filter to one category if requested
    if args.category and args.category != "all":
        import scrapers.evently_scraper as _self
        _self.LISTING_PAGES = [
            p for p in LISTING_PAGES
            if f"cat={args.category}" in p[0]
        ]

    scraper = EventlyScraper(
        max_pages=args.max_pages,
        max_events=args.max_events,
        debug=args.debug,
    )

    if args.raw:
        import pprint
        url_params, *_ = LISTING_PAGES[0]
        listing_url = f"{BASE_URL}/{url_params}"
        soup = scraper._get_soup(listing_url)
        if soup:
            nd = _next_data(soup)
            print(f"\n=== __NEXT_DATA__ for {listing_url} ===")
            print(f"Top-level keys: {list(nd.keys())}")
            props = nd.get("props", {}).get("pageProps", {})
            print(f"pageProps keys: {list(props.keys())}")
            raw_list = _extract_event_list_from_next_data(nd)
            print(f"\nEvent list: {len(raw_list)} items")
            if raw_list:
                print("\nFirst 2 items:")
                pprint.pprint(raw_list[:2])
        sys.exit(0)

    events = scraper.fetch_events()

    if args.dry_run or args.debug:
        print(f"\n── Evently dry-run: {len(events)} RM events ─────────────────────")
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
                    f"  category  : {ev.get('category')}\n"
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
