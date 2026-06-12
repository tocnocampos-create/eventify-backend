"""Club de Jazz de Santiago scraper — weekly jazz schedule.

www.clubdejazz.cl publishes its weekly program as plain text inside the
WordPress entry-content block on the homepage.  The page is updated manually
each week (last observed: 2026-06-02).

Schedule format (parsed with regex):
    Martes 2 de junio ($8.000) desde las 21:00 hrs. se presentará: Ensamble Bepop
    Viernes 5 de junio ($10.000), desde las 22:15 hrs. se presentará: Banjology

Tickets/reservations are NOT sold online — a reservation URL at La Fabbrica
(la-fabbrica.cl/reservas/) is linked instead.

source_url: f"clubdejazz:{iso_date}:{time_start}:{artist_slug}"

Run:
    python scrapers/clubdejazz_scraper.py --dry-run
    python scrapers/clubdejazz_scraper.py --dry-run --verbose
    python scrapers/clubdejazz_scraper.py
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

CLUBDEJAZZ_CONFIG = {
    "url": "https://www.clubdejazz.cl/",
    "venue_name": "Club de Jazz de Santiago",
    "ticket_url": "https://la-fabbrica.cl/reservas/",
    "category": "Música",
    "source_prefix": "clubdejazz",
}

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

_DAYS_ES = {
    "lunes", "martes", "miércoles", "miercoles",
    "jueves", "viernes", "sábado", "sabado", "domingo",
}

# Matches one show line:
#   "Martes 2 de junio ($8.000) desde las 21:00 hrs. se presentará: Artist"
#   "Viernes 5 de junio ($10.000), desde las 22:15 hrs. se presentará: Artist"
_SHOW_RE = re.compile(
    r"(Lunes|Martes|Mi[eé]rcoles|Jueves|Viernes|S[aá]bado|Domingo)"  # day name
    r"\s+(\d{1,2})\s+de\s+(\w+)"                                     # D de MONTH
    r"\s*\(\$([\d.,]+)\)[,\s]+"                                       # ($price)
    r"desde\s+las\s+(\d{1,2}:\d{2})\s+hrs\.\s+se\s+presentar[aá]:\s*"  # time
    r"(.+?)(?=\s+(?:Lunes|Martes|Mi[eé]rcoles|Jueves|Viernes|S[aá]bado|Domingo)|$)",
    re.IGNORECASE | re.DOTALL,
)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[áàä]", "a", text)
    text = re.sub(r"[éèë]", "e", text)
    text = re.sub(r"[íìï]", "i", text)
    text = re.sub(r"[óòö]", "o", text)
    text = re.sub(r"[úùü]", "u", text)
    text = re.sub(r"[ñ]", "n", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:40]


def _parse_price(raw: str) -> list[float] | None:
    """'8.000' → [8000.0, 8000.0]"""
    cleaned = re.sub(r"[^\d]", "", raw)
    if cleaned:
        try:
            val = float(cleaned)
            return [val, val]
        except ValueError:
            pass
    return None


def _resolve_date(day: int, month_name: str) -> str | None:
    """Return ISO date for a given day+month in the nearest plausible year."""
    month = _MONTHS_ES.get(month_name.lower())
    if not month:
        return None
    today = date.today()
    year  = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    # If >45 days in the past, assume next year
    if (today - candidate).days > 45:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None
    return candidate.isoformat()


class ClubDeJazzScraper(BaseScraper):
    """Scrapes the weekly jazz schedule from clubdejazz.cl."""

    name = "clubdejazz"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_events(self) -> list[dict[str, Any]]:
        url = CLUBDEJAZZ_CONFIG["url"]
        logger.info("[clubdejazz] Fetching %s", url)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("[clubdejazz] Fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        # The schedule lives in the WordPress entry-content block
        content_block = soup.find(class_=re.compile(r"entry-content|post-content", re.I))
        if not content_block:
            logger.warning("[clubdejazz] No entry-content block found")
            return []

        schedule_text = content_block.get_text(" ", strip=True)
        logger.debug("[clubdejazz] Schedule text (%d chars): %s…", len(schedule_text), schedule_text[:200])

        today = date.today().isoformat()
        events: list[dict[str, Any]] = []

        for m in _SHOW_RE.finditer(schedule_text):
            day_num    = int(m.group(2))
            month_name = m.group(3)
            price_raw  = m.group(4)
            time_raw   = m.group(5)
            artist     = m.group(6).strip()

            # Normalise time
            tm = re.match(r"(\d{1,2}):(\d{2})", time_raw)
            if not tm:
                continue
            time_start = f"{int(tm.group(1)):02d}:{tm.group(2)}"

            iso_date = _resolve_date(day_num, month_name)
            if not iso_date:
                logger.debug("[clubdejazz] Could not resolve date for day=%d month=%s", day_num, month_name)
                continue
            if iso_date < today:
                continue

            price_range = _parse_price(price_raw)
            artist_slug = _slugify(artist)
            source_url  = (
                f"{CLUBDEJAZZ_CONFIG['source_prefix']}"
                f":{iso_date}:{time_start.replace(':', '')}:{artist_slug}"
            )

            ev: dict[str, Any] = {
                "name":       artist,
                "date":       iso_date,
                "time_start": time_start,
                "source_url": source_url,
                "url":        CLUBDEJAZZ_CONFIG["ticket_url"],
                "venue_name": CLUBDEJAZZ_CONFIG["venue_name"],
                "category":   CLUBDEJAZZ_CONFIG["category"],
            }
            if price_range:
                ev["price_range"] = price_range

            events.append(ev)
            if self.max_events and len(events) >= self.max_events:
                break

        logger.info("[clubdejazz] Total upcoming events: %d", len(events))
        return events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Club de Jazz scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = ClubDeJazzScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── Club de Jazz dry-run: {len(events)} events ────────────────")
    for ev in events[:20]:
        print(
            f"\n  name      : {ev.get('name')!r}\n"
            f"  date      : {ev.get('date')}\n"
            f"  time_start: {ev.get('time_start')}\n"
            f"  price     : {ev.get('price_range')}\n"
            f"  source_url: {ev.get('source_url')}"
        )

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
