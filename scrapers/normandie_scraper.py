"""Normandie scraper — daily film schedule at Cine Arte Normandie.

normandie.cl/cartelera/ uses day-based sections:

    <section class="jueves">
      <h5>Jueves 11</h5>
      <hr>
      15:00 hrs. <br>
      <strong><a href="https://www.flow.cl/...">Film title</a></strong>
      ...
    </section>

Each <a> inside <strong> carries a unique flow.cl token as the ticket URL.
That token is used as source_url so re-runs update rather than duplicate.

Run:
    python scrapers/normandie_scraper.py --dry-run
    python scrapers/normandie_scraper.py --dry-run --verbose
    python scrapers/normandie_scraper.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

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

NORMANDIE_CONFIG = {
    "url": "https://normandie.cl/cartelera/",
    "venue_name": "Cine Normandie",
    "category": "Cine",
    "source_prefix": "normandie",
}

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# Maps CSS class name → (day_name_es, iso_weekday 0=Mon)
_DAY_CLASSES = {
    "lunes":      ("Lunes",     0),
    "martes":     ("Martes",    1),
    "miercoles":  ("Miércoles", 2),
    "miércoles":  ("Miércoles", 2),
    "jueves":     ("Jueves",    3),
    "viernes":    ("Viernes",   4),
    "sabado":     ("Sábado",    5),
    "sábado":     ("Sábado",    5),
    "domingo":    ("Domingo",   6),
}


def _parse_week_header(text: str) -> tuple[int, str] | None:
    """Parse 'Semana desde el jueves 11 al miércoles 17 de junio' → (start_day_num, month_name)."""
    m = re.search(
        r"desde\s+el\s+\w+\s+(\d+)\s+al\s+\w+\s+\d+\s+de\s+(\w+)",
        text, re.IGNORECASE,
    )
    if m:
        return int(m.group(1)), m.group(2).lower()
    return None


def _resolve_date(day_num: int, month_name: str) -> date | None:
    """Given a day-of-month and Spanish month name, return the best-matching future date."""
    month = _MONTHS_ES.get(month_name)
    if not month:
        return None
    today = date.today()
    year = today.year
    try:
        candidate = date(year, month, day_num)
    except ValueError:
        return None
    # If the candidate is more than 30 days in the past, try next year
    if (today - candidate).days > 30:
        try:
            candidate = date(year + 1, month, day_num)
        except ValueError:
            return None
    return candidate


def _parse_day_section(
    section: Tag,
    month_name: str,
) -> list[dict[str, Any]]:
    """Extract showtime events from one day section.

    Structure inside each section:
        <h5>Jueves 11</h5>
        <hr>
        15:00 hrs. <br>
        <strong><a href="flow.cl/...">Film title</a></strong>
        <hr>
        17:00 hrs. <br>
        <strong><a href="flow.cl/...">Other film</a></strong>
    """
    events: list[dict[str, Any]] = []

    # Get day number from h5
    h5 = section.find("h5")
    if not h5:
        return events
    day_text = h5.get_text(strip=True)
    m_day = re.search(r"(\d+)", day_text)
    if not m_day:
        return events
    day_num = int(m_day.group(1))

    show_date = _resolve_date(day_num, month_name)
    if show_date is None:
        return events
    if show_date < date.today():
        return events

    iso_date = show_date.isoformat()

    # Walk children looking for "HH:MM hrs." text nodes followed by <strong><a>
    current_time: str | None = None
    for child in section.children:
        if isinstance(child, NavigableString):
            txt = child.strip()
            m_time = re.search(r"(\d{1,2}):(\d{2})\s*hrs", txt)
            if m_time:
                current_time = f"{int(m_time.group(1)):02d}:{m_time.group(2)}"
        elif isinstance(child, Tag):
            if child.name == "strong":
                a = child.find("a", href=True)
                if a and current_time:
                    title = a.get_text(strip=True)
                    href  = a["href"]
                    if title and href:
                        events.append({
                            "name":       title,
                            "date":       iso_date,
                            "time_start": current_time,
                            "source_url": href,
                            "url":        href,
                            "venue_name": NORMANDIE_CONFIG["venue_name"],
                            "category":   NORMANDIE_CONFIG["category"],
                        })
                    current_time = None
            # Time text sometimes appears inside a <br> sibling — check for
            # text nodes inside non-strong tags
            elif child.name not in ("h5", "hr", "strong"):
                txt = child.get_text(strip=True)
                m_time = re.search(r"(\d{1,2}):(\d{2})\s*hrs", txt)
                if m_time:
                    current_time = f"{int(m_time.group(1)):02d}:{m_time.group(2)}"

    return events


class NormandieScraper(BaseScraper):
    """Scrapes the weekly film schedule from normandie.cl."""

    name = "normandie"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_events(self) -> list[dict[str, Any]]:
        url = NORMANDIE_CONFIG["url"]
        logger.info("[normandie] Fetching %s", url)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("[normandie] Fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        # ── Parse week header to get month name ───────────────────────────────
        titulocartelera = soup.find(class_="titulocartelera")
        month_name = "junio"  # fallback
        if titulocartelera:
            h4 = titulocartelera.find("h4")
            if h4:
                result = _parse_week_header(h4.get_text(strip=True))
                if result:
                    _, month_name = result

        # ── Parse each day section ─────────────────────────────────────────────
        # The cartelera may contain TWO weeks (current + previous).
        # Only the FIRST .titulocartelera + following day sections is current.
        # We stop collecting once we hit a second .titulocartelera.
        contenedorcartelera = soup.find(class_="contenedorcartelera")
        if not contenedorcartelera:
            logger.warning("[normandie] No .contenedorcartelera found")
            return []

        events: list[dict[str, Any]] = []
        past_first_title = False

        for child in contenedorcartelera.children:
            if not isinstance(child, Tag):
                continue

            if "titulocartelera" in (child.get("class") or []):
                if past_first_title:
                    # Second week block — stop
                    break
                past_first_title = True
                # Re-parse month name from the first encountered header
                h4 = child.find("h4")
                if h4:
                    result = _parse_week_header(h4.get_text(strip=True))
                    if result:
                        _, month_name = result
                continue

            # Check if this section is a day section
            section_classes = set(child.get("class") or [])
            matched_day = section_classes & set(_DAY_CLASSES)
            if matched_day:
                day_events = _parse_day_section(child, month_name)
                events.extend(day_events)
                logger.debug(
                    "[normandie] %s: %d events", matched_day, len(day_events)
                )

            if self.max_events and len(events) >= self.max_events:
                break

        logger.info("[normandie] Total events: %d", len(events))
        return events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Normandie scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = NormandieScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── Normandie dry-run: {len(events)} events ────────────────")
    for ev in events[:20]:
        print(
            f"\n  name      : {ev.get('name')!r}\n"
            f"  date      : {ev.get('date')}\n"
            f"  time_start: {ev.get('time_start')}\n"
            f"  source_url: {ev.get('source_url')}"
        )
        if args.verbose:
            print(f"  url       : {ev.get('url')}")

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
