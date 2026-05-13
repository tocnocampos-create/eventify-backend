"""Cineplanet Chile scraper — fetches movie sessions from the v3 cache API.

Calls three global cache endpoints on www.cineplanet.cl/v3/api/:

    GET /v3/api/cache/cinemascache
        → All cinemas with metadata and price formats.

    GET /v3/api/cache/moviescache
        → All current + coming-soon movies, each with a cinema/date/session map.

    GET /v3/api/cache/sessioncache
        → All individual sessions (showtime, formats, languages).

The three responses cross-reference via session IDs in the format
"{cinemaID}-{numericSessionID}", e.g. "0000000001-144497".

Each scheduled session becomes one Event row in the DB.
Coming-soon movies (isComingSoon=True, no sessions) are skipped.

Category and type are hard-locked to "Cine" — the classifier sentinel
(_locked_category) prevents any keyword rule from overriding this.

Authentication: the API requires the channel-token JWT cookie that the
server sets on any first request to www.cineplanet.cl. The scraper
initialises this cookie automatically before calling cache endpoints.

Run for a dry-run (fetch only, no DB writes):
    python scrapers/cineplanet_scraper.py --dry-run

Verify a specific cinema:
    python scrapers/cineplanet_scraper.py --cinema-id 0000000004 --dry-run --verbose
"""
from __future__ import annotations

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

# ── API constants ──────────────────────────────────────────────────────────────

API_BASE = "https://www.cineplanet.cl/v3/api"

# Headers that trigger the Express API backend instead of the CDN's SPA shell.
# The Sec-Fetch-* headers are the critical routing signal for Azure Front Door.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": "https://www.cineplanet.cl/peliculas",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Seed URL used only to obtain the channel-token JWT cookie.
COOKIE_SEED_URL = "https://www.cineplanet.cl/"

# Ticket/movie page base URL — used to build source_url for deduplication.
MOVIE_PAGE_BASE = "https://www.cineplanet.cl/peliculas"

# Price fallback in CLP (min = cheapest 2D weekday; max = most expensive 3D weekend).
# Prices obtained from formatsRate in cinemascache (values there are in centavos /100).
FALLBACK_PRICE = [3800.0, 5900.0]

# ── Cinema → venue name map ────────────────────────────────────────────────────
#
# Only Santiago-RM cinemas are listed here; non-Santiago cinemas are silently
# skipped. The venue_name string must match an existing Venue.name in the DB
# so the event enricher can populate venue_id automatically.
#
# Confirmed by fetching /v3/api/cache/cinemascache (May 2026):
#   ID=0000000001  CP Alameda       → Mallplaza Alameda (Estacion Central)
#   ID=0000000004  CP Costanera     → Costanera Center (Providencia)
#   ID=0000000007  CP Florida       → Florida Center (La Florida)
#   ID=0000000008  CP Quilin        → Mall Quilin, Macul (DB id=473)
#   ID=0000000012  CP Independencia → Mallplaza Norte (Independencia)
#
CINEMA_MAP: dict[str, str] = {
    "0000000001": "Cineplanet Mallplaza Alameda",
    "0000000004": "Cineplanet Costanera Center",
    "0000000007": "Cineplanet Florida Center",
    "0000000008": "Cineplanet Quilín",  # DB id=473 (Mall Quilín, Macul)
    "0000000012": "Cineplanet Mallplaza Norte",  # DB id=138 (Mallplaza Norte is in Independencia)
    # Non-Santiago (excluded — no DB venue rows):
    # "0000000002": "CP Concepcion"
    # "0000000003": "CP Copiapo"
    # "0000000005": "CP Curico"
    # "0000000009": "CP Temuco"
    # "0000000010": "CP Valdivia"
    # "0000000011": "CP Valparaiso"
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_showtime(raw: str) -> tuple[str, str] | tuple[None, None]:
    """Parse an ISO-8601 showtime string into (date, time_start).

    Accepts: "2026-05-10T17:15:00"  or  "2026-05-10T17:15:00-04:00"
    Returns: ("2026-05-10", "17:15") or (None, None) on failure.
    """
    if not raw:
        return None, None
    try:
        dt_str = raw[:19]  # strip timezone suffix
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except (ValueError, TypeError):
        logger.debug("Cannot parse showtime %r", raw)
        return None, None


def _format_label(formats: list[str], languages: list[str]) -> str:
    """Return a display label like '2D · SUBT' or '3D · DOBLAD'."""
    fmt = "/".join(f for f in formats if f != "CONV") or (formats[0] if formats else "2D")
    lang = languages[0] if languages else ""
    if lang:
        return f"{fmt} · {lang[:6]}"
    return fmt


def _slug(title: str) -> str:
    """Convert a movie title to a URL-safe slug (lowercase, hyphens)."""
    s = title.lower().strip()
    s = re.sub(r"[aàáä]", "a", s)
    s = re.sub(r"[eèéë]", "e", s)
    s = re.sub(r"[iìíï]", "i", s)
    s = re.sub(r"[oòóö]", "o", s)
    s = re.sub(r"[uùúü]", "u", s)
    s = re.sub(r"[n]", "n", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _build_source_url(cinema_id: str, movie_id: str, date: str, time_start: str) -> str:
    """Stable deduplication key for a single showtime session.

    Uses cinema+movie+date+time rather than sessionId because numeric session
    IDs can change between scrape runs when the booking system is updated.
    """
    return f"cineplanet:cl:{cinema_id}:{movie_id}:{date}:{time_start}"


def _extract_price_range(cinema: dict) -> list[float]:
    """Extract [min_price, max_price] in CLP from a cinema's formatsRate.

    formatsRate values are in centavos (380000 = 3800 CLP), divide by 100.
    Returns FALLBACK_PRICE if no rates are found.
    """
    rates: list[float] = []
    for day_rates in cinema.get("formatsRate", []):
        for r in day_rates.get("rates", []):
            gen = r.get("generalRate")
            if gen and isinstance(gen, (int, float)) and gen > 0:
                rates.append(gen / 100)
    if not rates:
        return list(FALLBACK_PRICE)
    return [min(rates), max(rates)]


# ── Main scraper class ─────────────────────────────────────────────────────────


class CineplanetScraper(BaseScraper):
    """Fetches Cineplanet Chile movie sessions via the /v3/ cache API."""

    name = "cineplanet"

    def __init__(
        self,
        cinema_ids: list[str] | None = None,
        max_events: int = 0,
        debug: bool = False,
    ) -> None:
        """
        Args:
            cinema_ids:  Subset of CINEMA_MAP keys to scrape.
                         None (default) scrapes all configured cinemas.
            max_events:  Stop after this many events (0 = unlimited).
            debug:       Print API responses without writing to DB.
        """
        super().__init__()
        self.cinema_ids = set(cinema_ids or CINEMA_MAP.keys())
        self.max_events = max_events
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── API helpers ───────────────────────────────────────────────────────────

    def _bootstrap_cookie(self) -> None:
        """Perform one request to the site root to obtain the channel-token JWT.

        The token is automatically stored in self.session.cookies and sent
        on all subsequent requests within this session.
        """
        try:
            self.session.get(COOKIE_SEED_URL, timeout=15)
            logger.debug("[cineplanet] channel-token cookie acquired")
        except requests.RequestException as exc:
            logger.warning("[cineplanet] Cookie bootstrap failed: %s", exc)

    def _get_json(self, path: str) -> Any:
        """GET {API_BASE}{path} and return parsed JSON, or None on failure."""
        url = f"{API_BASE}{path}"
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "json" not in ct:
                logger.error(
                    "[cineplanet] %s returned Content-Type %r (expected JSON). "
                    "Cookie may have expired — channel-token TTL is 3 minutes.",
                    path, ct,
                )
                return None
            return resp.json()
        except requests.RequestException as exc:
            logger.error("[cineplanet] Request failed %s: %s", path, exc)
            return None
        except ValueError as exc:
            logger.error("[cineplanet] JSON parse error %s: %s", path, exc)
            return None

    # ── Event building ────────────────────────────────────────────────────────

    def _build_event(
        self,
        movie: dict,
        cinema: dict,
        session_info: dict,
        price_range: list[float],
    ) -> dict | None:
        """Build a single event dict from movie + cinema + session data."""
        showtime = session_info.get("showtime", "")
        date, time_start = _parse_showtime(showtime)
        if not date or not time_start:
            return None

        title = movie.get("title", "").strip()
        if not title:
            return None

        cinema_id = cinema.get("ID", "")
        venue_name = CINEMA_MAP.get(cinema_id, cinema.get("name", ""))
        movie_id = movie.get("id", "")
        movie_url = movie.get("movieDetailsUrl") or _slug(title)

        formats = session_info.get("formats", [])
        languages = session_info.get("languages", [])
        fmt_label = _format_label(formats, languages)

        synopsis = (movie.get("synopsis") or "").strip()
        poster = movie.get("posterUrl") or movie.get("thumbnailUrl") or None
        if isinstance(poster, list):
            poster = poster[0] if poster else None

        runtime = movie.get("runTime")
        rating = movie.get("ratingDescription", "")

        description_parts = []
        if synopsis:
            description_parts.append(synopsis)
        info_parts = []
        if fmt_label:
            info_parts.append(fmt_label)
        if runtime:
            info_parts.append(f"{runtime} min")
        if rating:
            info_parts.append(f"Clasificacion: {rating}")
        if info_parts:
            description_parts.append(" | ".join(info_parts))
        description = "\n".join(description_parts)

        source_url = _build_source_url(cinema_id, movie_id, date, time_start)
        ticket_url = f"{MOVIE_PAGE_BASE}/{movie_url}"
        api_trailer = movie.get("trailer") or None

        return {
            "name": title,
            "description": description,
            "date": date,
            "time_start": time_start,
            "venue_name": venue_name,
            "category": "Cine",
            "type": "Cine",
            "_locked_category": "Cine",
            "price_range": price_range,
            "image_url": poster if isinstance(poster, str) and poster.startswith("http") else None,
            "source_url": source_url,
            "url": ticket_url,
            "_trailer_url": api_trailer,  # YouTube URL from Cineplanet API (ignored by deduplicator)
            "kids_friendly": False,
        }

    # ── Main fetch ────────────────────────────────────────────────────────────

    def fetch_events(self) -> list[dict]:
        """Fetch all Cineplanet Chile events for Santiago cinemas.

        Returns a flat list of event dicts (one per session x movie x cinema).
        """
        self._bootstrap_cookie()

        # 1. Fetch all three cache endpoints
        logger.info("[cineplanet] Fetching cinemascache ...")
        cinemas_data = self._get_json("/cache/cinemascache")
        if not cinemas_data:
            logger.error("[cineplanet] Failed to fetch cinemascache")
            return []
        cinemas_raw: list[dict] = cinemas_data.get("cinemas", [])

        logger.info("[cineplanet] Fetching moviescache ...")
        time.sleep(1)
        movies_data = self._get_json("/cache/moviescache")
        if not movies_data:
            logger.error("[cineplanet] Failed to fetch moviescache")
            return []
        movies_raw: list[dict] = movies_data.get("movies", [])

        logger.info("[cineplanet] Fetching sessioncache ...")
        time.sleep(1)
        sessions_data = self._get_json("/cache/sessioncache")
        if not sessions_data:
            logger.error("[cineplanet] Failed to fetch sessioncache")
            return []
        sessions_raw: list[dict] = sessions_data.get("sessions", [])

        # 2. Build lookup indexes
        cinema_by_id: dict[str, dict] = {c["ID"]: c for c in cinemas_raw}
        session_by_id: dict[str, dict] = {s["id"]: s for s in sessions_raw}

        # Price range per cinema (extracted once, reused across all sessions)
        price_by_cinema: dict[str, list[float]] = {
            cid: _extract_price_range(cinema_by_id[cid])
            for cid in self.cinema_ids
            if cid in cinema_by_id
        }

        logger.info(
            "[cineplanet] Loaded %d cinemas, %d movies, %d sessions",
            len(cinemas_raw), len(movies_raw), len(sessions_raw),
        )

        # 3. Iterate movies -> cinemas -> dates -> sessions -> build events
        events: list[dict] = []
        seen_source_urls: set[str] = set()

        for movie in movies_raw:
            if movie.get("isComingSoon"):
                continue  # no scheduled sessions yet

            movie_cinemas: list[dict] = movie.get("cinemas", [])

            for cinema_entry in movie_cinemas:
                cinema_id = cinema_entry.get("cinemaId", "")
                if cinema_id not in self.cinema_ids:
                    continue  # non-Santiago cinema

                cinema = cinema_by_id.get(cinema_id, {})
                price_range = price_by_cinema.get(cinema_id, list(FALLBACK_PRICE))

                for date_entry in cinema_entry.get("dates", []):
                    for session_key in date_entry.get("sessions", []):
                        session_info = session_by_id.get(session_key)
                        if not session_info:
                            logger.debug(
                                "[cineplanet] Session key %r not found in sessioncache",
                                session_key,
                            )
                            continue

                        event = self._build_event(movie, cinema, session_info, price_range)
                        if not event:
                            continue

                        # Deduplicate by source_url
                        src = event["source_url"]
                        if src in seen_source_urls:
                            continue
                        seen_source_urls.add(src)

                        events.append(event)

                        if self.max_events and len(events) >= self.max_events:
                            logger.info(
                                "[cineplanet] Reached max_events=%d, stopping early",
                                self.max_events,
                            )
                            return events

        logger.info("[cineplanet] Total events generated: %d", len(events))
        return events


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Cineplanet Chile scraper")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and print events without writing to DB")
    parser.add_argument("--cinema-id", metavar="ID",
                        help="Scrape only this cinema ID (e.g. 0000000004)")
    parser.add_argument("--max-events", type=int, default=0,
                        help="Stop after N events (0 = unlimited)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each event")
    args = parser.parse_args()

    cinema_ids = [args.cinema_id] if args.cinema_id else None
    scraper = CineplanetScraper(
        cinema_ids=cinema_ids,
        max_events=args.max_events,
        debug=args.dry_run,
    )

    events = scraper.fetch_events()
    print(f"\nTotal events: {len(events)}")

    if args.dry_run:
        from collections import Counter
        venue_counts: Counter = Counter()
        for e in events:
            venue_counts[e["venue_name"]] += 1
        print("\nEvents per venue:")
        for venue, count in sorted(venue_counts.items()):
            print(f"  {venue}: {count}")

        if args.verbose:
            print("\nFirst 5 events:")
            for e in events[:5]:
                print(json.dumps(e, indent=2, ensure_ascii=False, default=str))
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
        print(f"Saved — created={stats['created']}  updated={stats['updated']}  "
              f"skipped={stats['skipped']}  failed={stats['failed']}")

        # ── Upsert API trailer links (before TMDB so TMDB skips covered movies) ──
        from app.db.models import Event as EventModel, EventCommunityLink
        trailer_count = 0
        for ev in events:
            api_trailer = ev.get("_trailer_url")
            if not api_trailer:
                continue
            src = ev.get("source_url")
            if not src:
                continue
            event_row = db.query(EventModel).filter(EventModel.source_url == src).first()
            if not event_row:
                continue
            existing_link = db.query(EventCommunityLink).filter(
                EventCommunityLink.event_id == event_row.id,
                EventCommunityLink.platform == "youtube",
            ).first()
            if not existing_link:
                db.add(EventCommunityLink(
                    event_id=event_row.id,
                    platform="youtube",
                    url=api_trailer,
                ))
                trailer_count += 1
        db.commit()
        if trailer_count:
            print(f"Trailers — added={trailer_count} (from Cineplanet API)")

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
