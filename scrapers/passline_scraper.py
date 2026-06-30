"""Passline Chile scraper — fetches events from the billboard POST API.

API:
    POST https://api.passline.com/v1/event/GetBillboardByFilters
    Content-Type: application/json
    Body: {
        "country":    "chile",
        "commune":    "",
        "type":       0,
        "start_date": "YYYY-MM-DD 00:00:00",
        "end_date":   "YYYY-MM-DD 23:59:59",
        "limit":      "0,300",        # MySQL-style LIMIT offset,count
        "offset":     "1",
        "tag":        null,
        "tag_id":     null,
        "text":       "",
        "region":     "13",           # Región Metropolitana — server-side RM filter
    }

The API is protected by Cloudflare Bot Management.  Simple requests with a
plain User-Agent are blocked with HTTP 403 (Cf-Mitigated: challenge).
Fix: use curl-cffi (pip install curl-cffi) which impersonates Chrome at the
TLS/HTTP2 fingerprint level.  A session warm-up GET to passline.com obtains
the __cf_bm bot-management cookie before the API POST is made.

The scraper issues requests for a rolling 60-day window from today,
paginating in batches of 300 until the API returns fewer than 300 records.
RM filter is applied server-side via "region": "13"; SANTIAGO_TOKENS check
provides a secondary client-side guard.

Each event in the response becomes one DB Event row.
Category and type are left to the classifier — Passline covers all
categories (Música, Teatro, Comedia, Deportes excluded by the pipeline
if desired, Arte, etc.).

Deduplication key (source_url):
    passline:cl:{event_id}
  — uses the platform-assigned numeric/string ID, which is stable across
    re-scrapes.

Ticket URL:
    https://passline.com/eventos/{slug}   (preferred — SEO-friendly)
    https://passline.com/evento/{id}      (fallback when slug absent)

Run:
    python scrapers/passline_scraper.py --dry-run
    python scrapers/passline_scraper.py --dry-run --verbose
    python scrapers/passline_scraper.py --max-events 50 --dry-run
    python scrapers/passline_scraper.py --raw
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# curl-cffi impersonates real Chrome TLS/HTTP2 fingerprints — required to pass
# Cloudflare Bot Management on api.passline.com (plain requests → 403).
try:
    from curl_cffi import requests as _cf_requests  # type: ignore[import]
    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    import requests as _cf_requests  # type: ignore[assignment]
    _HAS_CURL_CFFI = False

from scrapers.base_scraper import BaseScraper
from scrapers.puntoticket_scraper import SANTIAGO_TOKENS

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

API_URL = "https://api.passline.com/v1/event/GetBillboardByFilters"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json, */*",
    "Accept-Language": "es-CL,es;q=0.9",
    "Origin": "https://passline.com",
    "Referer": "https://passline.com/",
}

# Rolling window: today → today + WINDOW_DAYS
WINDOW_DAYS = 60

# Page size — matches the "limit" field in the API body
PAGE_SIZE = 300

REQUEST_DELAY = 2  # seconds between paginated requests

# Public base URL for ticket / event detail pages
WEB_BASE = "https://passline.com"

# Warm-up URL — visited once per session so Cloudflare issues __cf_bm cookie
WARMUP_URL = "https://passline.com"

# Server-side region filter — 13 = Región Metropolitana de Santiago
RM_REGION = "13"

# Events whose titles contain any of these tokens are season passes /
# membership campaigns, not single purchasable events — skip them.
_SUBSCRIPTION_KEYWORDS: tuple[str, ...] = (
    "abono",
    "campaña",
    "membres",        # membresía / membresia
    "acceso abonado",
    "acceso especial",
    "acceso vip",
    "temporada",
    "pase de temporada",
    "pase anual",
    "suscripci",      # suscripción / suscripcion
    " vs ",           # sports matchday passes (e.g. "Team A vs Team B")
)

# If fecha_termino − fecha_inicio exceeds this many days the listing is
# almost certainly a multi-date pass or subscription, not a single event.
_MAX_EVENT_SPAN_DAYS = 30


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_window() -> tuple[str, str]:
    """Return (start_date, end_date) strings for the 60-day window."""
    today = date.today()
    end = today + timedelta(days=WINDOW_DAYS)
    return (
        today.strftime("%Y-%m-%d 00:00:00"),
        end.strftime("%Y-%m-%d 23:59:59"),
    )


def _build_body(start_date: str, end_date: str, offset: int) -> dict:
    """Build the POST body for one paginated request."""
    return {
        "country":    "chile",
        "commune":    "",
        "type":       0,
        "start_date": start_date,
        "end_date":   end_date,
        "limit":      f"{offset},{PAGE_SIZE}",
        "offset":     "1",
        "tag":        None,
        "tag_id":     None,
        "text":       "",
        "region":     RM_REGION,   # server-side RM filter — reduces payload and false positives
    }


def _extract_events(response: Any) -> list[dict]:
    """Return the list of event dicts from an API response.

    The API may return:
      - A plain list:                   [ev, ev, …]
      - A dict with a list value:       {"events": […]}  /  {"data": […]}
                                        {"result": […]}  /  {"results": […]}
      - A nested wrapper:               {"data": {"events": […]}}
    """
    if isinstance(response, list):
        return response

    if isinstance(response, dict):
        # Direct list under common keys
        for key in ("events", "data", "results", "result", "items", "billboard"):
            val = response.get(key)
            if isinstance(val, list):
                return val
            # One level of nesting: {"data": {"events": [...]}}
            if isinstance(val, dict):
                for inner_key in ("events", "data", "results", "items"):
                    inner = val.get(inner_key)
                    if isinstance(inner, list):
                        return inner

        # Last resort: first list-typed value in the dict
        for val in response.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val

    return []


def _get_str(obj: dict, *keys: str) -> str:
    """Return the first non-empty string value found for any of the given keys."""
    for k in keys:
        v = obj.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _parse_date(raw: str) -> str | None:
    """Parse a date string to YYYY-MM-DD.

    Handles:
      - "2026-04-23 20:00:00"  (datetime ISO)
      - "2026-04-23"           (date ISO)
      - "23/04/2026"           (slash)
      - "23-04-2026"           (dash, day-first)
    """
    if not raw:
        return None
    s = raw.strip()

    # ISO datetime or ISO date (YYYY-MM-DD …)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Slash: DD/MM/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"

    # Dash day-first: DD-MM-YYYY
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def _parse_time(raw: str) -> str | None:
    """Extract HH:MM from a datetime or time string."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", raw.strip())
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _parse_price(raw: Any) -> list[float] | None:
    """Parse price info into [min, max].

    Accepts:
      - A number (float/int):  3500 → [3500.0, 3500.0]
      - A list of two numbers: [3500, 8000] → [3500.0, 8000.0]
      - A dict:                {"min": 3500, "max": 8000}
      - A string:              "$3.500" / "Gratis" / "3500 - 8000"
      - None → None
    """
    if raw is None:
        return None

    if isinstance(raw, (int, float)):
        v = float(raw)
        return [0.0, 0.0] if v == 0 else [v, v]

    if isinstance(raw, list) and len(raw) >= 2:
        try:
            return [float(raw[0]), float(raw[1])]
        except (TypeError, ValueError):
            pass

    if isinstance(raw, dict):
        pmin = raw.get("min") or raw.get("minimum") or raw.get("precio_min") or raw.get("price_min")
        pmax = raw.get("max") or raw.get("maximum") or raw.get("precio_max") or raw.get("price_max")
        if pmin is not None:
            try:
                mn = float(pmin)
                mx = float(pmax) if pmax is not None else mn
                return [mn, mx]
            except (TypeError, ValueError):
                pass

    if isinstance(raw, str):
        lower = raw.lower()
        if any(w in lower for w in ("gratis", "gratuito", "libre", "free")):
            return [0.0, 0.0]
        numbers = re.findall(r"[\d]+(?:[.,]\d+)?", raw.replace(".", ""))
        prices: list[float] = []
        for n in numbers:
            try:
                val = float(n.replace(",", "."))
                if val >= 100:
                    prices.append(val)
            except ValueError:
                pass
        if prices:
            return [min(prices), max(prices)]

    return None


def _is_santiago(text: str) -> bool:
    """Return True if text suggests a Santiago event."""
    lower = text.lower()
    return any(tok in lower for tok in SANTIAGO_TOKENS)


def _build_source_url(event_id: Any) -> str:
    return f"passline:cl:{event_id}"


def _build_ticket_url(event: dict) -> str:
    """Return the best public URL for buying tickets to this event."""
    # Prefer slug-based URL (SEO-friendly)
    slug = _get_str(event, "slug", "url_slug", "event_slug", "permalink")
    if slug:
        slug = slug.lstrip("/")
        return f"{WEB_BASE}/eventos/{slug}"

    # Fallback: ID-based URL
    event_id = event.get("id") or event.get("event_id") or event.get("_id")
    if event_id:
        return f"{WEB_BASE}/evento/{event_id}"

    return WEB_BASE


# ── Scraper class ─────────────────────────────────────────────────────────────

class PasslineScraper(BaseScraper):
    """Fetches events from Passline Chile's billboard API (rolling 60-day window)."""

    name = "passline"

    def __init__(self, max_events: int = 0, debug: bool = False) -> None:
        """
        Args:
            max_events: Stop after this many events (0 = unlimited).
            debug:      Print sample events without writing to DB.
        """
        super().__init__()
        self.max_events = max_events
        self.debug = debug

        if _HAS_CURL_CFFI:
            self.session = _cf_requests.Session(impersonate="chrome120")
        else:
            self.session = _cf_requests.Session()
        self.session.headers.update(HEADERS)

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _post_page(self, start_date: str, end_date: str, offset: int) -> list[dict]:
        """POST one paginated request and return the raw event list.

        Returns an empty list on network/parse failure.
        """
        body = _build_body(start_date, end_date, offset)
        try:
            resp = self.session.post(API_URL, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except _cf_requests.RequestException as exc:
            logger.error("[passline] POST failed (offset=%d): %s", offset, exc)
            return []
        except ValueError as exc:
            logger.error("[passline] JSON decode error (offset=%d): %s", offset, exc)
            return []

        events = _extract_events(data)
        if not isinstance(events, list):
            logger.warning("[passline] Unexpected response type at offset=%d: %s", offset, type(data))
            return []

        logger.debug("[passline] offset=%d → %d raw events", offset, len(events))
        return events

    # ── Event builder ─────────────────────────────────────────────────────────

    def _build_event(self, raw: dict) -> dict[str, Any] | None:
        """Build a normalised event dict from a raw API record.

        Returns None if mandatory fields (name, date) cannot be extracted.
        """
        # ── Name ──────────────────────────────────────────────────────────────
        name = _get_str(raw, "name", "title", "event_name", "nombre", "titulo")
        if not name:
            return None

        # ── Subscription / membership filter ──────────────────────────────────
        # Skip season passes, abonos, and membership campaigns — they are not
        # single purchasable events and confuse availability/sold-out logic.
        name_lower = name.lower()
        if any(kw in name_lower for kw in _SUBSCRIPTION_KEYWORDS):
            logger.debug("[passline] Skipping subscription event %r", name)
            return None

        # ── Date + time ───────────────────────────────────────────────────────
        raw_date = _get_str(
            raw,
            "date", "start_date", "date_start", "event_date",
            "fecha", "fecha_inicio", "fecha_evento",
        )
        date_str = _parse_date(raw_date)
        if not date_str:
            logger.debug("[passline] Unparseable date %r for %r — skipping", raw_date, name)
            return None

        # Skip listings whose fecha_termino is more than _MAX_EVENT_SPAN_DAYS
        # after fecha_inicio — these are multi-date passes, not single events.
        fecha_termino_raw = _get_str(raw, "fecha_termino", "end_date", "date_end")
        fecha_termino_str = _parse_date(fecha_termino_raw)
        if fecha_termino_str and fecha_termino_str != date_str:
            try:
                span = (
                    date.fromisoformat(fecha_termino_str)
                    - date.fromisoformat(date_str)
                ).days
                if span > _MAX_EVENT_SPAN_DAYS:
                    logger.debug(
                        "[passline] Skipping multi-date listing %r (span=%d days)",
                        name, span,
                    )
                    return None
            except (ValueError, TypeError):
                pass

        raw_time = _get_str(
            raw,
            "time", "time_start", "start_time", "hora", "hora_inicio",
        )
        # Date fields often carry the time: "2026-04-23 20:00:00"
        if not raw_time and " " in raw_date:
            raw_time = raw_date.split(" ", 1)[1]
        time_start = _parse_time(raw_time)

        # ── IDs + URLs ────────────────────────────────────────────────────────
        event_id = raw.get("id") or raw.get("event_id") or raw.get("_id") or raw.get("idevento")
        if not event_id:
            # Synthetic fallback: name + date uniquely identifies the event
            event_id = f"{name}:{date_str}"

        source_url = _build_source_url(event_id)
        ticket_url = _build_ticket_url(raw)

        # ── Location / Santiago filter ────────────────────────────────────────
        # API field is "nombre_communa" (HTML-encoded); decode before use.
        import html as _html  # noqa: PLC0415
        commune_raw = _get_str(
            raw,
            "nombre_communa", "commune", "comuna", "city", "ciudad",
            "location", "venue_commune", "venue_city",
        )
        commune = _html.unescape(commune_raw)
        venue_name = _get_str(
            raw,
            "venue_name", "venue", "recinto", "lugar",
            "venue_title", "place", "place_name",
        )
        region = _get_str(raw, "nombre_region", "region", "region_name")

        # server-side region=13 filter already limits to RM; keep token check
        # as a secondary guard to exclude edge-case non-Santiago RM venues.
        location_hint = f"{commune} {venue_name} {region} {name}"
        if not _is_santiago(location_hint):
            return None

        # ── Image ─────────────────────────────────────────────────────────────
        image_url = None
        for key in (
            "miniatura", "recorte",                           # Passline API fields
            "image", "image_url", "imageUrl", "imagen",
            "thumbnail", "banner", "cover", "photo",
            "image_banner", "poster",
        ):
            val = raw.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                image_url = val
                break

        # ── Description ───────────────────────────────────────────────────────
        description = None
        for key in (
            "description", "descripcion", "detail", "detalle",
            "content", "contenido", "info",
        ):
            val = raw.get(key)
            if val and isinstance(val, str) and len(val.strip()) > 10:
                description = val.strip()[:1500]
                break

        # ── Price ─────────────────────────────────────────────────────────────
        # Passline returns precio_min as a decimal string: "2500.00", "0.00".
        # _parse_price's Chilean-format path strips ALL dots (for "$15.000"
        # thousand-separator notation), which corrupts "2500.00" → 250000.
        # Parse precio_min directly as a float to avoid the 100× inflation.
        precio_min_raw = raw.get("precio_min")
        if precio_min_raw is not None and str(precio_min_raw).strip():
            try:
                v = float(str(precio_min_raw).strip())
                price_range = [0.0, 0.0] if v == 0 else [v, v]
            except (ValueError, TypeError):
                price_range = _parse_price(
                    raw.get("price") or raw.get("price_range")
                    or raw.get("precio") or raw.get("prices")
                    or raw.get("ticket_price") or raw.get("min_price")
                )
        else:
            price_range = _parse_price(
                raw.get("price") or raw.get("price_range")
                or raw.get("precio") or raw.get("prices")
                or raw.get("ticket_price") or raw.get("min_price")
            )

        # ── Sold out ──────────────────────────────────────────────────────────
        # agotado="1" means Passline has exhausted its allocated ticket quota,
        # but it ALSO fires when the event never had public Passline tickets at
        # all (abonos, door-only, external channel) — in that case precio_min
        # and disponibles are both empty strings.
        #
        # Rule: only mark sold-out when agotado="1" AND the event DID have a
        # public Passline price (precio_min is non-empty), meaning tickets were
        # actually sold through Passline and are now exhausted.  When
        # precio_min is empty, the event simply has no public Passline channel
        # — we leave is_sold_out=False and let users check the Passline page.
        _agotado = str(raw.get("agotado", "0")) == "1"
        _had_public_tickets = bool(str(raw.get("precio_min", "")).strip())
        is_sold_out = _agotado and _had_public_tickets

        # ── Category hint — let classifier decide; provide hint if API gives one ──
        raw_category = _get_str(raw, "category_name", "category", "categoria", "type", "tipo", "genre")

        # ── Assemble event ────────────────────────────────────────────────────
        event: dict[str, Any] = {
            "name":       name,
            "date":       date_str,
            "source_url": source_url,
            "url":        ticket_url,
            "venue_name": venue_name or commune or "",
        }

        if time_start:
            event["time_start"] = time_start
        if image_url:
            event["image_url"] = image_url
        if description:
            event["description"] = description
        if price_range is not None:
            event["price_range"] = price_range
        event["is_sold_out"] = is_sold_out
        if raw_category:
            # Pass as a hint; classifier may override unless _locked_category is set
            event["_category_hint"] = raw_category

        return event

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all Santiago events in a rolling 60-day window.

        Paginates the API in batches of PAGE_SIZE (300) until the response
        returns fewer records than PAGE_SIZE (last page).

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        # Warm up the session so Cloudflare issues a __cf_bm cookie before the
        # first API POST (required when curl_cffi is available).
        try:
            self.session.get(WARMUP_URL, timeout=15)
            logger.debug("[passline] Warmup GET complete")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[passline] Warmup GET failed (continuing): %s", exc)

        start_date, end_date = _date_window()
        logger.info(
            "[passline] Window: %s → %s (WINDOW_DAYS=%d)",
            start_date, end_date, WINDOW_DAYS,
        )

        all_events: list[dict[str, Any]] = []
        seen_source_urls: set[str] = set()
        offset = 0

        while True:
            logger.info("[passline] Fetching page at offset=%d", offset)
            raw_events = self._post_page(start_date, end_date, offset)

            if not raw_events:
                logger.info("[passline] Empty page at offset=%d — stopping", offset)
                break

            page_kept = 0
            for raw in raw_events:
                if not isinstance(raw, dict):
                    continue

                ev = self._build_event(raw)
                if ev is None:
                    continue

                src = ev["source_url"]
                if src in seen_source_urls:
                    continue
                seen_source_urls.add(src)

                all_events.append(ev)
                page_kept += 1

                if self.max_events and len(all_events) >= self.max_events:
                    logger.info("[passline] Reached max_events=%d", self.max_events)
                    break

            logger.info(
                "[passline] offset=%d: %d raw → %d Santiago events kept "
                "(total so far: %d)",
                offset, len(raw_events), page_kept, len(all_events),
            )

            if self.max_events and len(all_events) >= self.max_events:
                break

            # If the page was smaller than PAGE_SIZE it was the last page
            if len(raw_events) < PAGE_SIZE:
                break

            offset += PAGE_SIZE
            time.sleep(REQUEST_DELAY)

        logger.info("[passline] Total events collected: %d", len(all_events))

        if not all_events and not self.max_events:
            raise RuntimeError(
                "[passline] Returned 0 events on a full run — "
                "likely a Cloudflare block on the Railway egress IP. "
                "Check the warmup GET response and __cf_bm cookie."
            )

        return all_events

    # ── Debug helper ──────────────────────────────────────────────────────────

    def _print_debug(self, events: list[dict[str, Any]], verbose: bool = False, n: int = 10) -> None:
        """Print a sample of fetched events (no DB writes)."""
        print("\n" + "=" * 70)
        print("DEBUG — PasslineScraper")
        print("=" * 70)
        print(f"\nTotal Santiago events fetched: {len(events)}")

        from collections import Counter
        venues = Counter(ev.get("venue_name", "?") for ev in events)
        print(f"\nUnique venues: {len(venues)}")
        print("Top-10 venues:")
        for vname, count in venues.most_common(10):
            print(f"  {count:4d}×  {vname!r}")

        print(f"\n── First {min(n, len(events))} events ──────────────────────────────")
        for ev in events[:n]:
            print(
                f"\n  name      : {ev.get('name')!r}\n"
                f"  date      : {ev.get('date')}\n"
                f"  time_start: {ev.get('time_start')}\n"
                f"  venue_name: {ev.get('venue_name')!r}\n"
                f"  source_url: {ev.get('source_url')}\n"
                f"  url       : {ev.get('url')}"
            )
            if verbose:
                print(
                    f"  image_url : {ev.get('image_url')}\n"
                    f"  price     : {ev.get('price_range')}\n"
                    f"  hint      : {ev.get('_category_hint')}\n"
                    f"  desc      : {str(ev.get('description', ''))[:120]!r}"
                )
        print("\n" + "=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Passline Chile scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print sample events — no DB writes",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Stop after this many events (0 = unlimited)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print image, price, and description for each sample event",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw first-page API response and exit",
    )
    args = parser.parse_args()

    scraper = PasslineScraper(max_events=args.max_events, debug=args.dry_run)

    if args.raw:
        import pprint
        start_date, end_date = _date_window()
        raw = scraper._post_page(start_date, end_date, 0)
        print(f"\n=== Raw first-page response (first 3 records) ===")
        print(f"Total records in page: {len(raw)}")
        if raw and isinstance(raw, list) and raw[0]:
            print(f"\nField names on first record:")
            pprint.pprint(list(raw[0].keys()))
            print("\nFirst record:")
            pprint.pprint(raw[0])
        sys.exit(0)

    events = scraper.fetch_events()

    if args.dry_run:
        scraper._print_debug(events, verbose=args.verbose, n=10)
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
