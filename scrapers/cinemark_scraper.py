"""Cinemark Chile scraper — fetches movie sessions from the BFF API.

Cinemark migrated from api.cinemark.cl (Vista API, dead as of ~2026-04-26)
to a new BFF at bff.cinemark.cl.  The new API is a flat-session endpoint:

    GET https://bff.cinemark.cl/api/cinema/showtimes?theater={id}
        Required header: country: CL
        → Flat list of all scheduled sessions for one cinema.

    GET https://bff.cinemark.cl/api/cinema/movies
        Required header: country: CL
        → All currently-showing + presale films: slug, posterUrl, corporateId.

Each scheduled session becomes one Event row in the DB.

Category and type are hard-locked to "Cine" — the classifier sentinel
(_locked_category) prevents any keyword rule from overriding this.

The venue_name written to each event is the human-readable cinema name
(e.g. "Cinemark Mallplaza Vespucio") so the enricher can match it to
an existing Venue row and populate venue_id automatically.

Run for a dry-run (fetch only, no DB writes):
    python scrapers/cinemark_scraper.py --dry-run

Verify a specific cinema:
    python scrapers/cinemark_scraper.py --cinema-id 511 --dry-run --verbose
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ── API constants ──────────────────────────────────────────────────────────────

BFF_BASE = "https://bff.cinemark.cl/api"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": "https://www.cinemark.cl/es/cartelera",
    "Origin": "https://www.cinemark.cl",
    # Required: BFF returns 500 "Country undefined not implemented" without this
    "country": "CL",
}

REQUEST_DELAY = 1  # seconds between per-theater API calls

# Session-specific ticket purchase URL.
# Format: TICKET_PURCHASE_BASE/{slug}/tickets-purchase/{sessionId}
TICKET_PURCHASE_BASE = "https://www.cinemark.cl/pelicula"

# Chile mainland is permanently UTC-3 (no DST since ~2016).
CHILE_UTC_OFFSET = timedelta(hours=-3)

# ── Cinema ID map ─────────────────────────────────────────────────────────────
#
# Maps Cinemark Chile's internal theater ID to the human-readable venue name
# matching existing Venue rows in seed.sql.  Only Santiago RM cinemas included.
#
# IDs confirmed from GET /api/cinema/theaters (May 2026) — same IDs as the
# old Vista API, carried over in the BFF migration.
# Non-Santiago cinemas excluded — no matching DB Venue rows.

CINEMA_MAP: dict[int, str] = {
    511: "Cinemark Mallplaza Vespucio",           # La Florida
    512: "Cinemark Alto Las Condes",              # Las Condes
    513: "Cinemark Mallplaza Oeste",              # Cerrillos
    519: "Cinemark Plaza Tobalaba",               # Puente Alto
    572: "Cinemark Plaza Norte",                  # Huecharaba
    2300: "Cinemark Portal Ñuñoa",               # Ñuñoa
    2307: "Cinemark Mid Mall Maipú",             # Maipú
    2310: "Cinemark Espacio Urbano Gran Avenida", # San Miguel
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_session_datetime(iso: str) -> tuple[str, str] | tuple[None, None]:
    """Convert a UTC ISO-8601 sessionDateTime to local (date, HH:MM).

    BFF returns datetimes in UTC with Z suffix, e.g. "2026-05-11T17:20:00.000Z".
    Chile mainland is permanently UTC-3.
    Returns (YYYY-MM-DD, HH:MM) in local time, or (None, None) on failure.
    """
    if not iso:
        return None, None
    try:
        # Remove the Z and treat as UTC, then shift to local
        dt_utc = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        dt_local = dt_utc + CHILE_UTC_OFFSET
        return dt_local.strftime("%Y-%m-%d"), dt_local.strftime("%H:%M")
    except (ValueError, TypeError):
        logger.debug("Cannot parse sessionDateTime %r", iso)
        return None, None


# ── Main scraper class ─────────────────────────────────────────────────────────


class CinemarkScraper(BaseScraper):
    """Fetches Cinemark Chile movie sessions via the BFF REST API."""

    name = "cinemark"

    def __init__(
        self,
        cinema_ids: list[int] | None = None,
        include_releases: bool = False,
        max_events: int = 0,
        debug: bool = False,
    ) -> None:
        """
        Args:
            cinema_ids:       Subset of CINEMA_MAP keys to scrape.
                              None (default) scrapes all configured cinemas.
            include_releases: Unused — presale movies are included automatically
                              via /cinema/movies (status=PRESALE). Kept for
                              backwards-compatibility with run_all.py callers.
            max_events:       Stop after this many events (0 = unlimited).
            debug:            Print API responses without writing to DB.
        """
        super().__init__()
        self.cinema_ids = cinema_ids or list(CINEMA_MAP.keys())
        self.max_events = max_events
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── API helpers ───────────────────────────────────────────────────────────

    def _get_json(self, url: str, params: dict | None = None) -> Any:
        """GET the URL and return parsed JSON, or None on failure."""
        try:
            resp = self.session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("API request failed %s (params=%s): %s", url, params, exc)
            return None
        except ValueError as exc:
            logger.error("JSON parse error for %s: %s", url, exc)
            return None

    def _fetch_movies(self) -> dict[str, dict[str, Any]]:
        """GET /cinema/movies → corporateId → movie metadata dict.

        Returns a map of corporateId → {slug, posterUrl, title, runTime, status}
        used to enrich session showtimes with poster and ticket URL.
        """
        data = self._get_json(f"{BFF_BASE}/cinema/movies")
        if not data or not isinstance(data.get("data"), list):
            logger.warning("[cinemark] Empty /cinema/movies response")
            return {}
        movie_map: dict[str, dict[str, Any]] = {}
        for m in data["data"]:
            corp_id = m.get("corporateId")
            if corp_id:
                movie_map[corp_id] = m
        logger.info("[cinemark] Loaded %d movies from /cinema/movies", len(movie_map))
        return movie_map

    def _fetch_showtimes(self, cinema_id: int) -> list[dict[str, Any]]:
        """GET /cinema/showtimes?theater={cinema_id} → flat session list."""
        time.sleep(REQUEST_DELAY)
        data = self._get_json(f"{BFF_BASE}/cinema/showtimes", params={"theater": cinema_id})
        if not data or not isinstance(data.get("data"), list):
            logger.warning("[cinemark] Empty showtimes for theater=%d", cinema_id)
            return []
        return data["data"]

    # ── Event builder ─────────────────────────────────────────────────────────

    def _build_event(
        self,
        session: dict[str, Any],
        movie: dict[str, Any],
        venue_name: str,
    ) -> dict[str, Any] | None:
        """Build one event dict from a BFF showtime session + movie metadata.

        Returns None if mandatory fields (name, date) cannot be extracted.
        """
        title = session.get("movieName", "").strip()
        if not title:
            return None

        date, time_start = _parse_session_datetime(session.get("sessionDateTime", ""))
        if not date:
            logger.debug("Skipping session — unparseable sessionDateTime for %r", title)
            return None

        session_id = session.get("sessionId", "")
        theater_id = session.get("theaterId", str(list(CINEMA_MAP.keys())[0]))

        # Stable deduplication key: theater + Vista sessionId
        source_url = f"cinemark:cl:{theater_id}:{session_id}"

        # Session-specific ticket purchase deep-link
        movie_slug = movie.get("slug", "")
        if movie_slug and session_id:
            ticket_url = f"{TICKET_PURCHASE_BASE}/{movie_slug}/tickets-purchase/{session_id}"
        elif movie_slug:
            ticket_url = f"{TICKET_PURCHASE_BASE}/{movie_slug}"
        else:
            ticket_url = None

        # Append non-2D format to display name for differentiation
        fmt = session.get("sessionFormat", "").strip()
        display_name = f"{title} ({fmt})" if fmt and fmt != "2D" else title

        poster = movie.get("posterUrl") or None

        # Sold out when no seats available
        occ = session.get("occupation") or {}
        is_sold_out = occ.get("availableSeats", 1) == 0 or occ.get("status") == "SOLD_OUT"

        event: dict[str, Any] = {
            "name": display_name,
            "date": date,
            "time_start": time_start,
            "source_url": source_url,
            "venue_name": venue_name,
            # Hard-lock category so classifier never overrides Cine events
            "category": "Cine",
            "type": "Cine",
            "_locked_category": "Cine",
            "kids_friendly": False,
            "is_sold_out": is_sold_out,
            # Cinemark BFF does not expose live prices; use known CLP range.
            "price_range": [3800.0, 9500.0],
        }

        if ticket_url:
            event["url"] = ticket_url
        if poster:
            event["image_url"] = poster

        return event

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all sessions from configured Santiago cinemas.

        Pipeline:
          1. GET /cinema/movies → build corporateId → movie metadata map
          2. For each cinema_id in self.cinema_ids:
               a. GET /cinema/showtimes?theater={id}
               b. For each flat session → build one event dict

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        # Build movie metadata map once for all theaters
        movie_map = self._fetch_movies()

        all_events: list[dict[str, Any]] = []
        seen_source_urls: set[str] = set()

        for cinema_id in self.cinema_ids:
            venue_name = CINEMA_MAP.get(cinema_id, f"Cinemark {cinema_id}")
            logger.info("[cinemark] Scraping showtimes for %r (theater=%d)", venue_name, cinema_id)

            sessions = self._fetch_showtimes(cinema_id)
            if not sessions:
                logger.warning("[cinemark] No sessions found for theater=%d", cinema_id)
                continue

            cinema_events = 0
            for session in sessions:
                corp_id = session.get("corporateId", "")
                movie = movie_map.get(corp_id, {})
                ev = self._build_event(session, movie, venue_name)
                if ev is None:
                    continue

                src = ev["source_url"]
                if src in seen_source_urls:
                    continue
                seen_source_urls.add(src)

                all_events.append(ev)
                cinema_events += 1

                if self.max_events and len(all_events) >= self.max_events:
                    logger.info("[cinemark] Reached max_events=%d", self.max_events)
                    break

            logger.info("[cinemark] theater=%d  %r → %d session events",
                        cinema_id, venue_name, cinema_events)

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[cinemark] Total events collected: %d", len(all_events))
        return all_events

    # ── Debug / CLI helper ────────────────────────────────────────────────────

    def _print_debug(self, events: list[dict[str, Any]], n: int = 5) -> None:
        """Print a sample of fetched events without writing to DB."""
        print("\n" + "=" * 70)
        print("DEBUG — CinemarkScraper")
        print("=" * 70)
        print(f"\nTotal events fetched: {len(events)}")
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
                f"  is_sold_out: {ev.get('is_sold_out')}\n"
                f"  price     : {ev.get('price_range')}"
            )
        print("\n" + "=" * 70)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cinemark Chile scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch events and print a sample — no DB writes",
    )
    parser.add_argument(
        "--cinema-id",
        type=int,
        dest="cinema_id",
        help="Scrape only this cinema_id (default: all in CINEMA_MAP)",
    )
    parser.add_argument(
        "--no-releases",
        action="store_true",
        help="Skip fetching coming-soon releases",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Stop after this many events (0 = unlimited)",
    )
    parser.add_argument(
        "--list-cinemas",
        action="store_true",
        help="Print the current CINEMA_MAP and exit",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra fields in the sample output",
    )
    args = parser.parse_args()

    if args.list_cinemas:
        print("Configured cinemas:")
        for cid, name in CINEMA_MAP.items():
            print(f"  cinema_id={cid}  →  {name!r}")
        sys.exit(0)

    cinema_ids = [args.cinema_id] if args.cinema_id else None

    scraper = CinemarkScraper(
        cinema_ids=cinema_ids,
        include_releases=False,   # /releases endpoint returns 400 — disabled
        max_events=args.max_events,
        debug=args.dry_run,
    )
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

        # ── TMDB metadata enrichment ──────────────────────────────────────────
        try:
            from scrapers.tmdb_enricher import apply_tmdb_to_cinema_events
            tmdb = apply_tmdb_to_cinema_events(db)
            print(f"TMDB  — enriched={tmdb['enriched']}  "
                  f"trailers_added={tmdb['trailers_added']}  "
                  f"not_found={tmdb['not_found']}")
        except EnvironmentError as exc:
            print(f"\nTMDB enrichment skipped — {exc}")
        except Exception as exc:
            logger.warning("TMDB enrichment failed: %s", exc)

        db.close()
        engine.dispose()

        print(f"\nDone — created={stats['created']}  updated={stats['updated']}  "
              f"skipped={stats['skipped']}  failed={stats['failed']}")
