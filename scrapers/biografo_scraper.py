"""El Biógrafo scraper — weekly film schedule at Cine El Biógrafo.

elbiografo.cl is a custom WordPress theme.  The cartelera section lists
3 films per week (Thu–Wed), each with a single fixed showtime.
The same film plays every day of the current week at its showtime.

Generates one Event per film × per remaining day in the week (≥ today).

Run:
    python scrapers/biografo_scraper.py --dry-run
    python scrapers/biografo_scraper.py --dry-run --verbose
    python scrapers/biografo_scraper.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
}
REQUEST_TIMEOUT = 20

BIOGRAFO_CONFIG = {
    "url": "https://elbiografo.cl/",
    "venue_name": "Cine El Biógrafo",
    "ticket_url": "https://elbiografo.cl/",
    "category": "Cine",
    "source_prefix": "biografo",
}

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _parse_week_badge(text: str) -> tuple[date, date] | None:
    """Parse '11 – al 17 de Junio · 2026' → (start_date, end_date)."""
    m = re.search(
        r"(\d+)\s*[–\-]\s*al\s+(\d+)\s+de\s+(\w+)\s*[·\-]\s*(\d{4})",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    day_start, day_end = int(m.group(1)), int(m.group(2))
    month = _MONTHS_ES.get(m.group(3).lower())
    year = int(m.group(4))
    if not month:
        return None
    try:
        return date(year, month, day_start), date(year, month, day_end)
    except ValueError:
        return None


def _parse_showtime(raw: str) -> str | None:
    """Extract 'HH:MM' from '▶ 15:30 hrs'."""
    m = re.search(r"(\d{1,2}):(\d{2})", raw)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[áàä]", "a", text)
    text = re.sub(r"[éèë]", "e", text)
    text = re.sub(r"[íìï]", "i", text)
    text = re.sub(r"[óòö]", "o", text)
    text = re.sub(r"[úùü]", "u", text)
    text = re.sub(r"[ñ]", "n", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


class BiografoScraper(BaseScraper):
    """Scrapes the weekly film schedule from elbiografo.cl."""

    name = "biografo"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_events(self) -> list[dict[str, Any]]:
        url = BIOGRAFO_CONFIG["url"]
        logger.info("[biografo] Fetching %s", url)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("[biografo] Fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        today = date.today()

        # ── Week range ────────────────────────────────────────────────────────
        badge = soup.find(class_="week-badge")
        week_range = _parse_week_badge(badge.get_text(" ", strip=True)) if badge else None
        if not week_range:
            logger.warning("[biografo] Could not parse week badge — using today+6")
            week_start = today
            week_end   = today + timedelta(days=6)
        else:
            week_start, week_end = week_range

        # Generate dates from max(week_start, today) to week_end (inclusive)
        first_day = max(week_start, today)
        dates: list[date] = []
        d = first_day
        while d <= week_end:
            dates.append(d)
            d += timedelta(days=1)

        if not dates:
            logger.info("[biografo] No remaining dates in current week")
            return []

        # ── Film cards ────────────────────────────────────────────────────────
        cards = soup.find_all(class_="movie-card")
        logger.info("[biografo] Found %d film card(s), %d remaining date(s)", len(cards), len(dates))

        events: list[dict[str, Any]] = []

        for card in cards:
            title_el = card.find(class_="movie-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue

            time_el = card.find(class_="movie-time")
            showtime = _parse_showtime(time_el.get_text(strip=True)) if time_el else None

            rating_el = card.find(class_="movie-rating")
            rating = rating_el.get_text(strip=True) if rating_el else None

            img_el = card.find("img", class_="poster-img")
            image_url = img_el["src"] if img_el and img_el.get("src") else None

            # First substantial paragraph = description
            description = None
            for p in card.find_all("p"):
                txt = p.get_text(strip=True)
                if len(txt) > 40:
                    description = txt[:1000]
                    break

            meta_el = card.find(class_="movie-meta-bar")
            meta_text = meta_el.get_text(strip=True) if meta_el else ""

            title_slug = _slugify(title)

            for show_date in dates:
                iso_date = show_date.isoformat()
                time_str = showtime or "15:00"
                source_url = (
                    f"{BIOGRAFO_CONFIG['source_prefix']}:{title_slug}"
                    f":{iso_date}:{time_str.replace(':', '')}"
                )
                ev: dict[str, Any] = {
                    "name":       title,
                    "date":       iso_date,
                    "source_url": source_url,
                    "url":        BIOGRAFO_CONFIG["ticket_url"],
                    "venue_name": BIOGRAFO_CONFIG["venue_name"],
                    "category":   BIOGRAFO_CONFIG["category"],
                }
                if showtime:
                    ev["time_start"] = showtime
                if image_url:
                    ev["image_url"] = image_url
                if description:
                    ev["description"] = description
                if rating:
                    ev["description"] = (
                        f"Clasificación: {rating}. {description or ''}"
                    ).strip()
                if meta_text:
                    ev.setdefault("description", meta_text)

                events.append(ev)
                if self.max_events and len(events) >= self.max_events:
                    break

            if self.max_events and len(events) >= self.max_events:
                break

        logger.info("[biografo] Total events: %d", len(events))
        return events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="El Biógrafo scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = BiografoScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── El Biógrafo dry-run: {len(events)} events ────────────────")
    for ev in events[:20]:
        print(
            f"\n  name      : {ev.get('name')!r}\n"
            f"  date      : {ev.get('date')}\n"
            f"  time_start: {ev.get('time_start')}\n"
            f"  source_url: {ev.get('source_url')}"
        )
        if args.verbose:
            print(f"  desc      : {str(ev.get('description', ''))[:120]!r}")

    if not args.dry_run:
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
        print(f"\n── Results: {stats}")
