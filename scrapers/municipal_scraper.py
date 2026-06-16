"""Teatro Municipal de Santiago scraper.

municipal.cl uses a Vue.js <events-list> component that calls a WordPress
AJAX endpoint returning structured JSON.  Each show includes a `functions`
array with individual performance dates, times, and ticket URLs.

Architecture:
  GET /cms/wp-admin/admin-ajax.php?action=get_events&page=N&limit=50
  → paginate → one event dict per function with a valid date_event

Run:
    python scrapers/municipal_scraper.py --dry-run
    python scrapers/municipal_scraper.py --dry-run --verbose
    python scrapers/municipal_scraper.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

AJAX_URL = "https://municipal.cl/cms/wp-admin/admin-ajax.php"
EVENTS_PER_PAGE = 50
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": "https://municipal.cl/espectaculos/",
}

VENUE_NAME = "Teatro Municipal de Santiago"

# API category string → Eventify category
_CATEGORY_MAP: dict[str, str] = {
    "Ballet y danza":         "Teatro",
    "Cartelera digital":      "Arte",
    "Conciertos y recitales": "Música",
    "Familiar":               "Familia",
    "Grandes estrellas":      "Música",
    "Musical":                "Teatro",
    "Ópera":                  "Música",
    "Pianistas":              "Música",
    "Sonatas Beethoven":      "Música",
    "Visitas guiadas temáticas": "Otros",
}


def _slugify(text: str) -> str:
    text = text.lower().strip()
    for src, dst in [
        ("á","a"),("à","a"),("ä","a"),("é","e"),("è","e"),("ë","e"),
        ("í","i"),("ì","i"),("ï","i"),("ó","o"),("ò","o"),("ö","o"),
        ("ú","u"),("ù","u"),("ü","u"),("ñ","n"),
    ]:
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _parse_hour(hour_str: str) -> str:
    """Convert '4:00 pm' / '12:00 am' → '16:00' / '00:00'."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", hour_str.strip(), re.IGNORECASE)
    if not m:
        return "20:00"
    h, mins, period = int(m.group(1)), m.group(2), m.group(3).lower()
    if period == "pm" and h != 12:
        h += 12
    elif period == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mins}"


def _url_slug(show_url: str) -> str:
    """Extract slug from 'https://municipal.cl/espectaculos/{slug}/'."""
    path = urlparse(show_url).path.rstrip("/")
    return path.split("/")[-1] if path else ""


class MunicipalScraper(BaseScraper):
    """Scrapes upcoming performances from Teatro Municipal de Santiago."""

    name = "municipal"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _fetch_page(self, page: int) -> dict:
        """Fetch one page from the AJAX endpoint; return parsed JSON."""
        r = self.session.get(
            AJAX_URL,
            params={
                "action": "get_events",
                "page": page,
                "limit": EVENTS_PER_PAGE,
                "query": "",
                "category": "",
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def fetch_events(self) -> list[dict[str, Any]]:
        today = date.today()
        all_events: list[dict[str, Any]] = []
        page = 1

        logger.info("[municipal] Fetching page 1 from AJAX API")
        try:
            payload = self._fetch_page(page)
        except requests.RequestException as exc:
            raise RuntimeError(f"[municipal] AJAX API unreachable: {exc}") from exc

        max_page: int = payload.get("max_page") or 1
        logger.info("[municipal] total shows: %d, pages: %d", payload.get("total", "?"), max_page)

        while True:
            shows = payload.get("data") or []
            for show in shows:
                title = (show.get("title") or "").strip()
                if not title:
                    continue

                show_url = (show.get("url") or "").strip()
                image_url = (show.get("img") or {}).get("src") or None
                price_raw = (show.get("price") or "").strip() or None
                api_category = (show.get("category") or "").strip()
                category = _CATEGORY_MAP.get(api_category, "Música")

                show_slug = _url_slug(show_url) or _slugify(title)
                functions = [
                    f for f in (show.get("functions") or [])
                    if f.get("date_event")
                ]

                if functions:
                    for fn in functions:
                        raw_date = fn["date_event"]
                        try:
                            ev_date = date.fromisoformat(raw_date)
                        except ValueError:
                            continue
                        if ev_date < today:
                            continue

                        ev_time = _parse_hour(fn.get("hour") or "")
                        ticket_url = (fn.get("url") or show_url).strip()
                        iso_date = ev_date.isoformat()
                        time_compact = ev_time.replace(":", "")
                        source_url = f"municipal:{show_slug}:{iso_date}:{time_compact}"

                        ev: dict[str, Any] = {
                            "name":       title,
                            "date":       iso_date,
                            "time_start": ev_time,
                            "source_url": source_url,
                            "url":        ticket_url or show_url,
                            "venue_name": VENUE_NAME,
                            "category":   category,
                        }
                        if image_url:
                            ev["image_url"] = image_url
                        if price_raw:
                            ev["price_range"] = price_raw
                        all_events.append(ev)

                else:
                    # No individual functions — create one event from start_date
                    raw_date = (show.get("start_date") or "").strip()
                    if not raw_date:
                        continue
                    try:
                        ev_date = date.fromisoformat(raw_date)
                    except ValueError:
                        continue
                    if ev_date < today:
                        continue

                    ev_time = (show.get("time") or "")[:5] or "20:00"
                    iso_date = ev_date.isoformat()
                    time_compact = ev_time.replace(":", "")
                    source_url = f"municipal:{show_slug}:{iso_date}:{time_compact}"

                    ev = {
                        "name":       title,
                        "date":       iso_date,
                        "time_start": ev_time,
                        "source_url": source_url,
                        "url":        show_url,
                        "venue_name": VENUE_NAME,
                        "category":   category,
                    }
                    if image_url:
                        ev["image_url"] = image_url
                    if price_raw:
                        ev["price_range"] = price_raw
                    all_events.append(ev)

                if self.max_events and len(all_events) >= self.max_events:
                    break

            if self.max_events and len(all_events) >= self.max_events:
                break

            page += 1
            if page > max_page:
                break

            time.sleep(REQUEST_DELAY)
            logger.info("[municipal] Fetching page %d/%d", page, max_page)
            try:
                payload = self._fetch_page(page)
            except requests.RequestException as exc:
                logger.warning("[municipal] Page %d failed: %s", page, exc)
                break

        logger.info("[municipal] Total events: %d", len(all_events))
        return all_events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Teatro Municipal de Santiago scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = MunicipalScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── Municipal dry-run: {len(events)} events ───────────────────")
    for ev in events[:20]:
        print(
            f"\n  name      : {ev.get('name')!r}\n"
            f"  date      : {ev.get('date')}\n"
            f"  time_start: {ev.get('time_start')}\n"
            f"  category  : {ev.get('category')}\n"
            f"  price     : {ev.get('price_range')}\n"
            f"  source_url: {ev.get('source_url')}"
        )
        if args.verbose:
            print(f"  url       : {ev.get('url')}")
            print(f"  image_url : {ev.get('image_url')}")

    if not args.dry_run:
        from scrapers.base_scraper import make_scraper_session
        from scrapers import classifier, enricher, deduplicator

        engine, db = make_scraper_session()
        now = datetime.now(timezone.utc)
        stats: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
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
        print(f"\n── Results: {stats}")
