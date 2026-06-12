"""Cineteca Nacional scraper — film events at Cineteca Nacional (CCLaMoneda).

cinetecanacional.gob.cl uses the Modern Events Calendar (MEC) WordPress plugin.
The /cartelera/ page renders all upcoming events as article.mec-event-article
elements in the initial HTML (66+ items, no AJAX required for the visible list).

Structure per article:
    <article class="mec-event-article mec-clear">
      <div class="mec-event-date mec-bg-color">
        <span class="mec-start-date-label">12 Junio 2026</span>
        <div class="mec-time-details">
          <span class="mec-start-time">18:00</span>
        </div>
      </div>
      <h4 class="mec-event-title">
        <a href="https://cinetecanacional.gob.cl/eventos/...">Title</a>
      </h4>
      <p class="mec-grid-event-location">Sala de cine</p>
      ...
      <a href="...">Reservar ticket</a>  <!-- only when available -->
    </article>

The event detail URL is used as source_url for stable deduplication.

Run:
    python scrapers/cineteca_scraper.py --dry-run
    python scrapers/cineteca_scraper.py --dry-run --verbose
    python scrapers/cineteca_scraper.py
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

CINETECA_CONFIG = {
    "url": "https://cinetecanacional.gob.cl/cartelera/",
    "venue_name": "Cineteca Nacional Centro Cultural La Moneda",
    "category": "Cine",
    "source_prefix": "cineteca",
}

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _parse_mec_date(text: str) -> str | None:
    """Parse '12 Junio 2026' → '2026-06-12'."""
    m = re.match(r"(\d+)\s+(\w+)\s+(\d{4})", text.strip())
    if not m:
        return None
    day   = int(m.group(1))
    month = _MONTHS_ES.get(m.group(2).lower())
    year  = int(m.group(3))
    if not month:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


class CinetecaScraper(BaseScraper):
    """Scrapes upcoming film events from cinetecanacional.gob.cl."""

    name = "cineteca"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch_events(self) -> list[dict[str, Any]]:
        url = CINETECA_CONFIG["url"]
        logger.info("[cineteca] Fetching %s", url)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("[cineteca] Fetch failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        today = date.today().isoformat()

        # ── Strategy: two passes ─────────────────────────────────────────────
        # Pass 1: main listing grid (mec-start-date-label articles) — 2-3 items
        # Pass 2: calendar widget (div.mec-calendar-events-sec[data-mec-cell])
        #   Each cell contains DIRECT article children for that specific date.
        #   Cells are deeply nested (each day nested inside the prev day's article),
        #   so we must NOT do recursive find_all — only direct .children of each cell.

        events: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        def _extract_article(article: Any, iso_date: str) -> dict[str, Any] | None:
            """Pull fields from an mec-event-article given a known iso_date."""
            # Time
            time_start: str | None = None
            for time_cls in ("mec-start-time", "mec-event-time"):
                time_el = article.find(class_=time_cls)
                if time_el:
                    raw_t = re.sub(r"[^\d:]", "", time_el.get_text(strip=True))
                    m_t = re.match(r"(\d{1,2}):(\d{2})", raw_t)
                    if m_t:
                        time_start = f"{int(m_t.group(1)):02d}:{m_t.group(2)}"
                    break

            # Title + URL
            title_el = article.find("h4", class_="mec-event-title")
            if not title_el:
                return None
            a_el = title_el.find("a", href=True)
            if not a_el:
                return None
            title     = a_el.get_text(strip=True)
            event_url = a_el["href"]
            if not title or not event_url:
                return None

            # Image (only first img; skip nested-section images)
            img_el = article.find("img")
            image_url: str | None = None
            if img_el:
                src = img_el.get("src") or img_el.get("data-src")
                if src and src.startswith("http"):
                    image_url = src

            # Location
            loc_el = article.find(class_=re.compile(r"mec-grid-event-location|mec-event-loc-place"))
            location = loc_el.get_text(strip=True) if loc_el else None

            ev: dict[str, Any] = {
                "name":       title,
                "date":       iso_date,
                "source_url": event_url,
                "url":        event_url,
                "venue_name": CINETECA_CONFIG["venue_name"],
                "category":   CINETECA_CONFIG["category"],
            }
            if time_start:
                ev["time_start"] = time_start
            if image_url:
                ev["image_url"] = image_url
            if location:
                ev["description"] = f"Sala: {location}"
            return ev

        # ── Pass 1: main grid articles (have mec-start-date-label) ───────────
        from bs4 import Tag as _Tag  # noqa: PLC0415
        articles = soup.find_all("article", class_=re.compile(r"mec-event-article"))
        logger.info("[cineteca] Found %d MEC event articles (all views)", len(articles))
        for article in articles:
            date_el = article.find(class_="mec-start-date-label")
            if not date_el:
                continue
            iso_date = _parse_mec_date(date_el.get_text(strip=True))
            if not iso_date or iso_date < today:
                continue
            ev = _extract_article(article, iso_date)
            if ev and ev["source_url"] not in seen_urls:
                seen_urls.add(ev["source_url"])
                events.append(ev)

        # ── Pass 2: calendar widget cells ─────────────────────────────────────
        # find_all returns ALL cells including deeply nested ones — each has its own date.
        cal_cells = soup.find_all("div", class_="mec-calendar-events-sec")
        logger.debug("[cineteca] Calendar cells: %d", len(cal_cells))
        for cell in cal_cells:
            cell_str = cell.get("data-mec-cell", "")
            if len(cell_str) != 8:
                continue
            iso_date = f"{cell_str[:4]}-{cell_str[4:6]}-{cell_str[6:8]}"
            if iso_date < today:
                continue

            # Only DIRECT article children — skip nested calendar sub-cells
            for child in cell.children:
                if not isinstance(child, _Tag) or child.name != "article":
                    continue
                cls = set(child.get("class") or [])
                if "mec-past-event" in cls:
                    continue
                detail = child.find(class_="mec-event-detail")
                if detail and "Sin eventos" in detail.get_text():
                    continue
                ev = _extract_article(child, iso_date)
                if ev and ev["source_url"] not in seen_urls:
                    seen_urls.add(ev["source_url"])
                    events.append(ev)
                    if self.max_events and len(events) >= self.max_events:
                        break
            if self.max_events and len(events) >= self.max_events:
                break

        logger.info("[cineteca] Total upcoming events: %d", len(events))
        return events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cineteca Nacional scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = CinetecaScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── Cineteca dry-run: {len(events)} events ────────────────")
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
