"""Thelonious Club de Jazz scraper — weekly jazz schedule.

theloniouschile.com/cartelera is a mywebsitebuilder.com hosted site that
renders its schedule as rich-text blocks inside the SPA.  Playwright is
required to wait for JavaScript to complete rendering before parsing.

Each day block is a `div.rich-text-positioning-wrapper` containing:
  - A header span with the day abbreviation and date: "JUE 11/06"
  - One or two show lines: "21:00 hrs  ARTIST NAME"

Fixed venue prices (not per-event, but included as best-effort):
  - General (all shows): $6.000
  - Fri/Sat second show:  $8.000

source_url: f"thelonious:{iso_date}:{time_start_no_colon}:{artist_slug}"

Run:
    python scrapers/thelonious_scraper.py --dry-run
    python scrapers/thelonious_scraper.py --dry-run --verbose
    python scrapers/thelonious_scraper.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

THELONIOUS_CONFIG = {
    "url": "https://www.theloniouschile.com/cartelera",
    "venue_name": "Thelonious Club de Jazz",
    "category": "Música",
    "source_prefix": "thelonious",
    "ticket_url": "https://www.theloniouschile.com/reservas",
}

# Day abbreviation → ISO weekday (Mon=0)
_DAY_ABBR = {
    "lun": 0, "mar": 1, "mie": 2, "mié": 2,
    "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6,
}

# Fri/Sat weekdays for 2nd-show price rule
_FRISAB_WEEKDAYS = {4, 5}

_DAY_HEADER_RE = re.compile(
    r"^(LUN|MAR|MIE|MIÉ|JUE|VIE|SAB|SÁB|DOM)\s+(\d{1,2})/(\d{2})",
    re.IGNORECASE,
)

# Matches a time + the artist that follows it (non-greedy, stops at next time or end).
# "hrs" suffix is optional — some blocks omit it.
_SHOW_RE = re.compile(
    r"(\d{1,2}:\d{2})(?:\s*hrs?)?\s+(.+?)(?=\s*\d{1,2}:\d{2}|\Z)",
    re.DOTALL,
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


def _resolve_date(day: int, month: int) -> str | None:
    """Return ISO date for DD/MM in the nearest plausible year."""
    today = date.today()
    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if (today - candidate).days > 30:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None
    return candidate.isoformat()


def _clean_block_text(raw: str) -> str:
    """Remove zero-width spaces, normalize broken 'hr s' artifacts, collapse whitespace."""
    # Zero-width space (U+200B) and similar invisible chars
    raw = re.sub(r"[​­﻿]", "", raw)
    # "hr s" artifact from overlapping HTML spans
    raw = re.sub(r"\bhr\s+s\b", "hrs", raw, flags=re.IGNORECASE)
    # Collapse whitespace
    raw = re.sub(r"[ \t\xa0]+", " ", raw).strip()
    return raw


class TheloniousScraper(BaseScraper):
    """Scrapes the weekly jazz schedule from theloniouschile.com using Playwright."""

    name = "thelonious"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events

    def fetch_events(self) -> list[dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("[thelonious] playwright not installed — run: pip install playwright && playwright install chromium")
            return []

        url = THELONIOUS_CONFIG["url"]
        logger.info("[thelonious] Fetching %s (Playwright)", url)

        content: str = ""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                content = page.content()
                browser.close()
        except Exception as exc:
            logger.error("[thelonious] Playwright fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(content, "lxml")
        today = date.today().isoformat()
        events: list[dict[str, Any]] = []

        # Each day occupies one div.rich-text-positioning-wrapper
        for wrapper in soup.find_all("div", class_="rich-text-positioning-wrapper"):
            raw = wrapper.get_text(" ", strip=True)
            block = _clean_block_text(raw)

            m_hdr = _DAY_HEADER_RE.match(block)
            if not m_hdr:
                continue

            day_abbr = m_hdr.group(1).lower()
            day_num  = int(m_hdr.group(2))
            month    = int(m_hdr.group(3))

            iso_date = _resolve_date(day_num, month)
            if not iso_date or iso_date < today:
                continue

            weekday = _DAY_ABBR.get(day_abbr)

            # Strip the header from the rest of the block text
            rest = block[m_hdr.end():].strip()

            show_matches = list(_SHOW_RE.finditer(rest))
            for idx, sm in enumerate(show_matches):
                time_raw = sm.group(1)
                artist   = sm.group(2).strip()

                # Normalise time
                tm = re.match(r"(\d{1,2}):(\d{2})", time_raw)
                if not tm:
                    continue
                time_start = f"{int(tm.group(1)):02d}:{tm.group(2)}"

                # Sanitise artist: strip trailing stray punctuation / spaces
                artist = re.sub(r"[\s​]+$", "", artist).strip()
                if not artist:
                    continue

                # Price: Fri/Sat second show = 8000, everything else = 6000
                price: list[float]
                if weekday in _FRISAB_WEEKDAYS and idx > 0:
                    price = [8000.0, 8000.0]
                else:
                    price = [6000.0, 6000.0]

                artist_slug = _slugify(artist)
                source_url  = (
                    f"{THELONIOUS_CONFIG['source_prefix']}"
                    f":{iso_date}:{time_start.replace(':', '')}:{artist_slug}"
                )

                events.append({
                    "name":        artist,
                    "date":        iso_date,
                    "time_start":  time_start,
                    "source_url":  source_url,
                    "url":         THELONIOUS_CONFIG["ticket_url"],
                    "venue_name":  THELONIOUS_CONFIG["venue_name"],
                    "category":    THELONIOUS_CONFIG["category"],
                    "price_range": price,
                })

                if self.max_events and len(events) >= self.max_events:
                    break

            if self.max_events and len(events) >= self.max_events:
                break

        logger.info("[thelonious] Total upcoming events: %d", len(events))
        return events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Thelonious Club de Jazz scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = TheloniousScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── Thelonious dry-run: {len(events)} events ────────────────")
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
