"""Cinépolis Chile scraper — fetches sessions from the cartelera POST API.

The API is a single ASP.NET WebMethod endpoint:

    POST https://cinepolischile.cl/Cartelera.aspx/GetNowPlayingByCity
    Content-Type: application/json
    Body: {"claveCiudad": "<sector>", "esVIP": false}

ASP.NET WebMethod wraps every response in {"d": "<json-string>"} — the
value of "d" is a JSON-encoded string that must be parsed a second time.

The decoded object has the structure:
    {
      "Cinemas": [
        {
          "Name":    "Cinépolis La Reina",
          "VistaId": "1234",
          "CityKey": "santiago-oriente",
          "Movies": [
            {
              "Title":         "...",
              "Rating":        "TE",
              "RunTime":       "120 min",
              "PosterDynamic": "https://...",
              "Synopsis":      "...",
              "Dates": [
                {
                  "ShowtimeDate": "Miércoles 23 de Abril de 2026",
                  "Showtimes": [
                    {
                      "Time":         "14:30",
                      "ShowtimeAMPM": "PM",
                      "ShowtimeId":   "...",
                      "Formats":      ["2D", "Español"]
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }

Each Showtime entry becomes one Event row in the DB.
Category and type are hard-locked to "Cine" via the _locked_category
sentinel — the classifier will not override this.

The cinema.Name is written to venue_name so the enricher can match it
against existing Cinépolis venue rows (seed.sql IDs 120–127).

Deduplication key (source_url):
    cinepolis:cl:{vista_id}:{showtime_id}
  — stable across re-scrapes because both come from Vista's booking system.

Run:
    python scrapers/cinepolis_scraper.py --dry-run          # all sectors
    python scrapers/cinepolis_scraper.py --sector santiago-oriente --dry-run
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

API_URL = "https://cinepolischile.cl/Cartelera.aspx/GetNowPlayingByCity"

# All 4 Santiago sectors Cinépolis operates in.
# Each POST call returns all cinemas + sessions in that sector.
SANTIAGO_SECTORS: list[str] = [
    "santiago-centro",
    "santiago-oriente",
    "santiago-poniente-y-norte",
    "santiago-sur",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": "https://cinepolischile.cl/Cartelera.aspx",
    "Origin": "https://cinepolischile.cl",
    "X-Requested-With": "XMLHttpRequest",
}

REQUEST_DELAY = 1  # seconds between POST calls

# Base URL for public movie/ticket pages (used to build the event url)
TICKET_BASE = "https://cinepolischile.cl/comprar-boletos"

# Spanish month names for date parsing
_MONTHS_ES: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5,
    "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


# ── Response parsing helpers ──────────────────────────────────────────────────

def _unwrap_d(raw: Any) -> dict | list | None:
    """Unwrap the ASP.NET WebMethod {"d": "<json-string>"} envelope.

    Three cases handled:
      1. {"d": "<json-encoded string>"}  → JSON.parse the string value
      2. {"d": {...object...}}           → use the object directly
      3. Bare list or dict (no envelope) → use as-is
    """
    if isinstance(raw, dict):
        d = raw.get("d")
        if d is None:
            return raw  # no envelope — use the dict directly
        if isinstance(d, str):
            try:
                return json.loads(d)
            except json.JSONDecodeError as exc:
                logger.error("Failed to JSON-parse d value: %s", exc)
                return None
        return d  # already a dict/list
    return raw  # bare list or other


def _get_cinemas(parsed: Any) -> list[dict]:
    """Extract the Cinemas list from the parsed response object."""
    if isinstance(parsed, dict):
        for key in ("Cinemas", "cinemas", "data", "results"):
            val = parsed.get(key)
            if isinstance(val, list):
                return val
        # Some responses nest under a city wrapper
        for val in parsed.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    if isinstance(parsed, list):
        return parsed
    return []


def _parse_showtime_date(raw: str) -> str | None:
    """Parse ShowtimeDate to YYYY-MM-DD.

    Accepts:
      - "Miércoles 23 de Abril de 2026"
      - "23 de abril"         (year defaults to current)
      - "2026-04-23"          (already ISO)
      - "23/04/2026"
    """
    if not raw:
        return None
    s = raw.strip().lower()

    # Already ISO
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Slash format
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return f"{y:04d}-{mo:02d}-{d:02d}"

    # Spanish prose: "23 de abril de 2026" / "miércoles 23 de abril"
    m = re.search(
        r"(\d{1,2})\s+de\s+([a-záéíóúüñ]+)(?:\s+de\s+(\d{4}))?", s
    )
    if m:
        day = int(m.group(1))
        month_num = _MONTHS_ES.get(m.group(2)[:3])
        if month_num:
            year = int(m.group(3)) if m.group(3) else datetime.now().year
            return f"{year:04d}-{month_num:02d}-{day:02d}"

    return None


def _parse_time(time_val: str, ampm_val: str) -> str | None:
    """Convert Cinépolis time fields to HH:MM (24-hour).

    The AngularJS template calls getHourFormat(showtime.ShowtimeAMPM,
    showtime.Time). Observed value shapes:
      Time="14:30", ShowtimeAMPM="" → "14:30"
      Time="2:30",  ShowtimeAMPM="PM" → "14:30"
      Time="10:00", ShowtimeAMPM="AM" → "10:00"
      Time="14:30:00"                 → "14:30"
    """
    if not time_val:
        return None

    # Strip seconds if present
    time_clean = re.sub(r":\d{2}$", "", time_val.strip())

    m = re.match(r"(\d{1,2}):(\d{2})", time_clean)
    if not m:
        return None

    hour, minute = int(m.group(1)), int(m.group(2))

    # Apply AM/PM correction only when hour is in 12h range
    if ampm_val:
        suffix = ampm_val.strip().upper()
        if suffix == "PM" and hour < 12:
            hour += 12
        elif suffix == "AM" and hour == 12:
            hour = 0

    return f"{hour:02d}:{minute:02d}"


def _extract_poster(movie: dict) -> str | None:
    """Return the best available poster URL from a movie dict."""
    for key in (
        "PosterDynamic", "Poster", "poster", "ImageUrl", "imageUrl",
        "Image", "image", "Thumbnail", "thumbnail",
    ):
        val = movie.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    return None


def _extract_synopsis(movie: dict) -> str | None:
    """Return the movie synopsis/description, truncated to 1 500 chars."""
    for key in ("Synopsis", "synopsis", "Description", "description", "Sinopsis"):
        val = movie.get(key)
        if val and isinstance(val, str) and len(val.strip()) > 10:
            return val.strip()[:1500]
    return None


def _extract_formats(showtime: dict) -> str:
    """Return a display label for the projection format (2D, 3D, IMAX…)."""
    # Formats field may be a list of strings like ["3D", "Español"]
    fmts = showtime.get("Formats") or showtime.get("formats") or []
    if isinstance(fmts, list):
        # Keep projection-type tokens; drop language tokens
        proj = [
            f for f in fmts
            if f and not any(
                lang in f.lower()
                for lang in ("español", "subtitulad", "doblad", "vose", "vos")
            )
        ]
        return " ".join(proj) if proj else ""
    if isinstance(fmts, str):
        return fmts
    # Fallback: check for a single Format string field
    single = showtime.get("Format") or showtime.get("format") or ""
    return single.upper() if single else ""


def _build_source_url(vista_id: str, showtime_id: str) -> str:
    """Stable deduplication key for one showtime session."""
    return f"cinepolis:cl:{vista_id}:{showtime_id}"


def _build_ticket_url(vista_id: str, showtime_id: str) -> str:
    """Public purchase URL for this session.

    Cinépolis Chile ticket flow uses:
        /Compra?vistaId={id}&showtimeId={id}
    Falls back to the cartelera page if VistaId is empty.
    """
    if vista_id and showtime_id:
        return (
            f"https://cinepolischile.cl/Compra"
            f"?vistaId={vista_id}&showtimeId={showtime_id}"
        )
    return "https://cinepolischile.cl/Cartelera.aspx"


# ── Scraper class ─────────────────────────────────────────────────────────────

class CinepolisScraper(BaseScraper):
    """Fetches Cinépolis Chile sessions from the cartelera POST API."""

    name = "cinepolis"

    def __init__(
        self,
        sectors: list[str] | None = None,
        max_events: int = 0,
        debug: bool = False,
    ) -> None:
        """
        Args:
            sectors:    Subset of SANTIAGO_SECTORS to query.
                        None (default) scrapes all 4 Santiago sectors.
            max_events: Stop after this many events (0 = unlimited).
            debug:      Print sample events without writing to DB.
        """
        super().__init__()
        self.sectors = sectors or SANTIAGO_SECTORS
        self.max_events = max_events
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _post_sector(self, sector: str) -> dict | list | None:
        """POST GetNowPlayingByCity for one sector and return the parsed city object.

        Returns None on network/parse failure.
        """
        body = {"claveCiudad": sector, "esVIP": False}
        try:
            resp = self.session.post(API_URL, json=body, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
        except requests.RequestException as exc:
            logger.error("[cinepolis] POST failed for sector %r: %s", sector, exc)
            return None
        except ValueError as exc:
            logger.error("[cinepolis] JSON decode error for sector %r: %s", sector, exc)
            return None

        parsed = _unwrap_d(raw)
        if parsed is None:
            logger.warning("[cinepolis] Empty/invalid response for sector %r", sector)
        return parsed

    # ── Event builder ─────────────────────────────────────────────────────────

    def _build_event(
        self,
        cinema: dict,
        movie: dict,
        date_entry: dict,
        showtime: dict,
    ) -> dict[str, Any] | None:
        """Build one event dict from a single scheduled showtime.

        Returns None if mandatory fields (name, date) cannot be extracted.
        """
        title = (
            movie.get("Title")
            or movie.get("title")
            or movie.get("Titulo")
        )
        if not title or not isinstance(title, str):
            return None
        title = title.strip()

        raw_date = (
            date_entry.get("ShowtimeDate")
            or date_entry.get("showtimeDate")
            or date_entry.get("Date")
        )
        date = _parse_showtime_date(str(raw_date)) if raw_date else None
        if not date:
            logger.debug(
                "[cinepolis] Unparseable date %r for %r — skipping showtime",
                raw_date, title,
            )
            return None

        raw_time = showtime.get("Time") or showtime.get("time") or ""
        raw_ampm = showtime.get("ShowtimeAMPM") or showtime.get("showtimeAMPM") or ""
        time_start = _parse_time(str(raw_time), str(raw_ampm))
        if not time_start:
            logger.debug(
                "[cinepolis] Unparseable time %r/%r for %r — skipping",
                raw_time, raw_ampm, title,
            )
            return None

        vista_id = str(cinema.get("VistaId") or cinema.get("vistaId") or "")
        showtime_id = str(
            showtime.get("ShowtimeId")
            or showtime.get("showtimeId")
            or showtime.get("Id")
            or ""
        )
        if not showtime_id:
            # Fallback: synthetic key from cinema+date+time
            showtime_id = f"{cinema.get('Name', '')}:{date}:{time_start}"

        source_url = _build_source_url(vista_id, showtime_id)
        ticket_url = _build_ticket_url(vista_id, showtime_id)

        cinema_name = (cinema.get("Name") or cinema.get("name") or "").strip()

        fmt = _extract_formats(showtime)
        display_name = f"{title} ({fmt})" if fmt and fmt.upper() not in ("2D", "") else title

        event: dict[str, Any] = {
            "name":             display_name,
            "date":             date,
            "time_start":       time_start,
            "source_url":       source_url,
            "url":              ticket_url,
            "venue_name":       cinema_name,
            # Hard-locked — classifier must not override
            "category":         "Cine",
            "type":             "Cine",
            "_locked_category": "Cine",
            "kids_friendly":    False,
        }

        poster = _extract_poster(movie)
        if poster:
            event["image_url"] = poster

        synopsis = _extract_synopsis(movie)
        if synopsis:
            event["description"] = synopsis

        # Runtime → description fallback or separate field (not in Event schema,
        # so we append it to description when synopsis is absent)
        runtime = movie.get("RunTime") or movie.get("runTime") or ""
        if runtime and not synopsis:
            event["description"] = f"Duración: {runtime}"

        return event

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all sessions from all configured Santiago sectors.

        Pipeline per sector:
          POST GetNowPlayingByCity → unwrap {"d": "..."} → parse city JSON
          → iterate Cinemas → Movies → Dates → Showtimes → build event

        Deduplicates on source_url across sectors (same session may appear
        in overlapping sector responses).

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        all_events: list[dict[str, Any]] = []
        seen_source_urls: set[str] = set()

        for sector in self.sectors:
            logger.info("[cinepolis] Fetching sector %r", sector)
            time.sleep(REQUEST_DELAY)

            parsed = self._post_sector(sector)
            cinemas = _get_cinemas(parsed) if parsed is not None else []

            if not cinemas:
                logger.warning(
                    "[cinepolis] No cinemas in sector %r — "
                    "check claveCiudad value or API response shape",
                    sector,
                )
                continue

            sector_events = 0

            for cinema in cinemas:
                cinema_name = (cinema.get("Name") or cinema.get("name") or "").strip()
                movies = cinema.get("Movies") or cinema.get("movies") or []

                if not isinstance(movies, list):
                    continue

                for movie in movies:
                    dates = movie.get("Dates") or movie.get("dates") or []
                    if not isinstance(dates, list):
                        continue

                    for date_entry in dates:
                        showtimes = (
                            date_entry.get("Showtimes")
                            or date_entry.get("showtimes")
                            or []
                        )
                        if not isinstance(showtimes, list):
                            continue

                        for showtime in showtimes:
                            ev = self._build_event(cinema, movie, date_entry, showtime)
                            if ev is None:
                                continue

                            src = ev["source_url"]
                            if src in seen_source_urls:
                                continue
                            seen_source_urls.add(src)

                            all_events.append(ev)
                            sector_events += 1

                            if self.max_events and len(all_events) >= self.max_events:
                                logger.info(
                                    "[cinepolis] Reached max_events=%d", self.max_events
                                )
                                break

                        if self.max_events and len(all_events) >= self.max_events:
                            break
                    if self.max_events and len(all_events) >= self.max_events:
                        break
                if self.max_events and len(all_events) >= self.max_events:
                    break

            logger.info(
                "[cinepolis] Sector %r: %d cinemas, %d new session events",
                sector, len(cinemas), sector_events,
            )

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[cinepolis] Total events collected: %d", len(all_events))
        return all_events

    # ── Debug helper ──────────────────────────────────────────────────────────

    def _print_debug(self, events: list[dict[str, Any]], n: int = 8) -> None:
        """Print a sample of fetched events (no DB writes)."""
        print("\n" + "=" * 70)
        print("DEBUG — CinepolisScraper")
        print("=" * 70)
        print(f"\nTotal events fetched: {len(events)}")

        # Venue breakdown
        from collections import Counter
        venues = Counter(ev.get("venue_name", "?") for ev in events)
        print("\nVenue breakdown:")
        for vname, count in venues.most_common():
            print(f"  {count:4d}×  {vname}")

        print(f"\n── First {min(n, len(events))} events ──────────────────────────────")
        for ev in events[:n]:
            print(
                f"\n  name      : {ev.get('name')!r}\n"
                f"  date      : {ev.get('date')}\n"
                f"  time_start: {ev.get('time_start')}\n"
                f"  venue_name: {ev.get('venue_name')!r}\n"
                f"  source_url: {ev.get('source_url')}\n"
                f"  url       : {ev.get('url')}\n"
                f"  image_url : {ev.get('image_url')}\n"
                f"  desc      : {str(ev.get('description', ''))[:100]!r}"
            )
        print("\n" + "=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cinépolis Chile scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print sample events — no DB writes",
    )
    parser.add_argument(
        "--sector",
        choices=SANTIAGO_SECTORS,
        help="Scrape only this sector (default: all 4)",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Stop after this many events (0 = unlimited)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw API response for the first sector and exit",
    )
    args = parser.parse_args()

    scraper = CinepolisScraper(
        sectors=[args.sector] if args.sector else None,
        max_events=args.max_events,
        debug=args.dry_run,
    )

    if args.raw:
        # Diagnostic: print the raw unwrapped API response
        import pprint
        sector = args.sector or SANTIAGO_SECTORS[0]
        raw = scraper._post_sector(sector)
        cinemas = _get_cinemas(raw) if raw else []
        print(f"\n=== Raw response for {sector!r} ===")
        print(f"Top-level keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")
        print(f"Cinemas found: {len(cinemas)}")
        if cinemas:
            first = cinemas[0]
            print(f"\nFirst cinema keys: {list(first.keys())}")
            movies = first.get("Movies") or first.get("movies") or []
            if movies:
                print(f"First cinema movie count: {len(movies)}")
                print(f"First movie keys: {list(movies[0].keys())}")
                dates = movies[0].get("Dates") or movies[0].get("dates") or []
                if dates:
                    print(f"First date keys: {list(dates[0].keys())}")
                    shows = dates[0].get("Showtimes") or dates[0].get("showtimes") or []
                    if shows:
                        print(f"First showtime keys: {list(shows[0].keys())}")
                        pprint.pprint(shows[0])
        sys.exit(0)

    events = scraper.fetch_events()

    if args.dry_run:
        scraper._print_debug(events, n=10)
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
