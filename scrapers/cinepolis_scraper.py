"""Cinépolis Chile scraper — fetches sessions from the GraphQL API.

The cinepolis.com/cl site migrated from the ASP.NET cinepolischile.cl endpoint
(which now 302-redirects to cinepolis.com/cl) to a Next.js + GraphQL stack.

Two-step fetch per scraper run:
  1. movies(countryId: "CL") → v2 endpoint → current movie listing
  2. billboard(movieId, cinemas) → v1 endpoint → showtimes per cinema per date

Cinema data (vistaId, name, sector) is fetched once via the locations API and
cached in-memory for the run.

API base: https://api-g.cinepolis.com
  v2/billboards/graphql  — movie listings (name, synopsis, poster, rating)
  v1/billboards/graphql  — showtimes per movie+cinema (Billboard query)
  shared-services/locations/graphql — cinema/city metadata

Auth: x-apikey header (hard-coded app key from the Next.js bundle).
Transport: curl_cffi Chrome124 impersonation (bypasses Cloudflare).

Deduplication key (source_url):
    cinepolis:cl:{vista_id}:{session_id}
  — identical to the old ASP.NET scraper so existing DB rows survive migration.

Run:
    python scrapers/cinepolis_scraper.py --dry-run
    python scrapers/cinepolis_scraper.py --max-events 20
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ── API constants ─────────────────────────────────────────────────────────────

_BASE = "https://api-g.cinepolis.com"
_V1   = f"{_BASE}/v1/billboards/graphql"
_V2   = f"{_BASE}/v2/billboards/graphql"
_LOC  = f"{_BASE}/shared-services/locations/graphql"

_API_KEY   = "lQM6Mkvri1iHksKKCfpAiwGXq0YUZA7Nn6XAXRPr4i13LwXo"
_IMG_BASE  = "https://tickets-static-content.cinepolis.com"
_COUNTRY  = "CL"
_TIMEZONE = "America/Santiago"
_WARMUP   = "https://cinepolis.com/cl"

_HEADERS = {
    "Content-Type": "application/json",
    "x-apikey":     _API_KEY,
    "country-id":   _COUNTRY,
    "language":     "ES",
    "Origin":       "https://cinepolis.com",
    "Referer":      "https://cinepolis.com/cl/cartelera",
}

# Santiago sectors — same as the old claveCiudad values
SANTIAGO_SECTORS = [
    "santiago-centro",
    "santiago-oriente",
    "santiago-poniente-y-norte",
    "santiago-sur",
]

REQUEST_DELAY = 0.6  # seconds between billboard calls

# ── GraphQL queries ───────────────────────────────────────────────────────────

_Q_MOVIES = """
query Movies($countryId: String!) {
    movies(countryId: $countryId) {
        edges {
            node {
                id
                name
                originalName
                synopsis
                rating
                length
                formats
                languages
                media {
                    resource
                    type
                    code
                    sizes { large medium small }
                }
            }
        }
    }
}
"""

_Q_CINEMAS = """
query Cinemas($country_id: String!, $city_id: String!) {
    cinemas(country_id: $country_id, city_id: $city_id) {
        edges {
            node {
                id
                name
                vistaId
                cityId
                timezone
            }
        }
    }
}
"""

_Q_BILLBOARD = """
query Billboard(
    $countryId: String!
    $movieId: String!
    $cinemas: String!
    $timezone: String
) {
    billboard(
        countryId: $countryId
        movieId: $movieId
        cinemas: $cinemas
        timezone: $timezone
    ) {
        dates
        schedules {
            cinemaId
            cityId
            movieId
            dates {
                date
                languages {
                    language
                    displayLanguage
                    showtimes {
                        format { name }
                        sessionId
                        datetime
                        cinemaVistaId
                        movieVistaId
                        availability
                    }
                }
            }
        }
    }
}
"""

# ── Scraper ───────────────────────────────────────────────────────────────────

class CinepolisScraper(BaseScraper):
    """Fetches Cinépolis Chile sessions from the new GraphQL API."""

    name = "cinepolis"

    def __init__(
        self,
        sectors: list[str] | None = None,
        max_events: int = 0,
        debug: bool = False,
    ) -> None:
        super().__init__()
        self.sectors   = sectors or SANTIAGO_SECTORS
        self.max_events = max_events
        self.debug     = debug

        try:
            from curl_cffi import requests as _cf
            self._session = _cf.Session(impersonate="chrome124")
        except ImportError:
            raise RuntimeError(
                "curl_cffi is required for the Cinépolis scraper. "
                "Install it: pip install curl_cffi"
            )

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _warmup(self) -> None:
        """Visit cinepolis.com/cl to establish a valid CF session cookie."""
        try:
            self._session.get(_WARMUP, timeout=20)
        except Exception as exc:
            logger.warning("[cinepolis] Warmup GET failed: %s", exc)

    def _gql(self, url: str, query: str, variables: dict) -> dict | None:
        """Execute one GraphQL request. Returns the data dict or None on error."""
        try:
            resp = self._session.post(
                url,
                json={"query": query, "variables": variables},
                headers=_HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.error("[cinepolis] GQL request to %s failed: %s", url, exc)
            return None

        if "errors" in payload:
            for err in payload["errors"]:
                logger.warning("[cinepolis] GQL error: %s", err.get("message"))

        return payload.get("data")

    # ── Data fetchers ─────────────────────────────────────────────────────────

    def _fetch_cinemas(self) -> dict[str, dict]:
        """
        Return a dict mapping cinema string-id → {vistaId, name, sector, timezone}.
        Fetches all Santiago sectors from the locations API.
        """
        cinema_map: dict[str, dict] = {}
        for sector in self.sectors:
            data = self._gql(
                _LOC, _Q_CINEMAS,
                {"country_id": _COUNTRY, "city_id": sector},
            )
            if not data:
                continue
            for edge in (data.get("cinemas") or {}).get("edges") or []:
                node = edge.get("node") or {}
                cid = node.get("id")
                if cid:
                    cinema_map[cid] = {
                        "vistaId":  str(node.get("vistaId") or ""),
                        "name":     node.get("name") or cid,
                        "sector":   sector,
                        "timezone": node.get("timezone") or _TIMEZONE,
                    }
        logger.info("[cinepolis] Loaded %d cinemas across %d sectors", len(cinema_map), len(self.sectors))
        return cinema_map

    def _fetch_movies(self) -> list[dict]:
        """Return a list of movie dicts from the v2 movies query."""
        data = self._gql(_V2, _Q_MOVIES, {"countryId": _COUNTRY})
        if not data:
            return []
        return [
            e["node"] for e in (data.get("movies") or {}).get("edges") or []
            if e.get("node")
        ]

    def _fetch_billboard(self, movie_id: str, cinema_ids: list[str]) -> dict | None:
        """Fetch showtime schedules for one movie across all supplied cinemas."""
        return self._gql(
            _V1, _Q_BILLBOARD,
            {
                "countryId": _COUNTRY,
                "movieId":   movie_id,
                "cinemas":   ",".join(cinema_ids),
                "timezone":  _TIMEZONE,
            },
        )

    # ── Event builder ─────────────────────────────────────────────────────────

    @staticmethod
    def _poster_url(media: list | None) -> str | None:
        """Return the portrait poster URL (720×1022 preferred) from a media list.

        Media paths are relative: "/pimcore/.../resource.jpg".
        Prepend _IMG_BASE to build the full URL.
        """
        def _full(path: str, resource: str) -> str:
            return f"{_IMG_BASE}{path.rstrip('/')}/{resource.lstrip('/')}"

        candidates = []
        for item in media or []:
            if not isinstance(item, dict):
                continue
            sizes    = item.get("sizes") or {}
            resource = item.get("resource") or "resource.jpg"
            for size_key in ("large", "medium", "small"):
                path = sizes.get(size_key)
                if path and isinstance(path, str) and path.startswith("/"):
                    candidates.append((path, resource))
                    break

        if not candidates:
            return None

        # Prefer portrait poster (720×1022) over others
        for path, resource in candidates:
            if "720x1022" in path or "720X1022" in path:
                return _full(path, resource)

        # Fallback: first candidate
        path, resource = candidates[0]
        return _full(path, resource)

    def _build_event(
        self,
        movie:       dict,
        showtime:    dict,
        cinema_info: dict,
        date:        str,
        language:    str,
    ) -> dict[str, Any] | None:
        """Build one event dict from a single showtime row."""
        title = (movie.get("name") or movie.get("originalName") or "").strip()
        if not title:
            return None

        raw_dt = showtime.get("datetime") or ""
        if not raw_dt or "T" not in raw_dt:
            return None
        date_part, time_part = raw_dt.split("T", 1)
        time_start = time_part[:5]  # "HH:MM"

        session_id  = str(showtime.get("sessionId") or "")
        vista_id    = (
            str(showtime.get("cinemaVistaId") or "")
            or cinema_info.get("vistaId", "")
        )
        source_url  = f"cinepolis:cl:{vista_id}:{session_id}" if vista_id and session_id else None
        if not source_url:
            return None

        fmt_name = (showtime.get("format") or {}).get("name") or ""
        lang_suffix = "" if language.upper() in ("ESP", "ES", "") else f" ({language.upper()})"
        proj_tokens  = [t for t in fmt_name.split() if t.upper() not in ("ESP", "SUB", "ESPAÑOL", "SUBTITULADA")]
        proj = " ".join(proj_tokens)
        if proj and proj.upper() != "2D":
            display_name = f"{title} ({proj}){lang_suffix}"
        elif lang_suffix:
            display_name = f"{title}{lang_suffix}"
        else:
            display_name = title

        cinema_name = cinema_info.get("name") or ""
        ticket_url  = f"https://cinepolis.com/cl/detalle?cinema={cinema_info.get('id', '')}&movie={movie['id']}"

        event: dict[str, Any] = {
            "name":             display_name,
            "date":             date_part,
            "time_start":       time_start,
            "source_url":       source_url,
            "url":              ticket_url,
            "venue_name":       cinema_name,
            "category":         "Cine",
            "type":             "Cine",
            "_locked_category": "Cine",
            "kids_friendly":    False,
            "price_range":      [3900.0, 9900.0],
        }

        poster = self._poster_url(movie.get("media"))
        if poster:
            event["image_url"] = poster

        synopsis = (movie.get("synopsis") or "").strip()
        if synopsis:
            event["description"] = synopsis[:1500]
        elif movie.get("length"):
            event["description"] = f"Duración: {movie['length']}"

        return event

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """
        Fetch all upcoming Cinépolis sessions for Santiago.

        Pipeline:
          1. Warmup GET to establish Cloudflare session cookie
          2. Fetch cinema list from locations API (by sector)
          3. Fetch movie list from v2 billboards API
          4. For each movie, fetch showtimes from v1 billboards API
          5. Yield one event dict per showtime

        Returns a flat deduplicated list of event dicts.
        """
        self._warmup()

        cinema_map = self._fetch_cinemas()
        if not cinema_map:
            raise RuntimeError(
                "[cinepolis] No cinemas returned from locations API — "
                "check API key validity and network access."
            )

        cinema_ids = list(cinema_map.keys())
        # Attach string id to each cinema_info for ticket URL building
        for cid, info in cinema_map.items():
            info["id"] = cid

        movies = self._fetch_movies()
        if not movies:
            raise RuntimeError(
                "[cinepolis] No movies returned from v2 billboards API — "
                "check API key and country-id headers."
            )
        logger.info("[cinepolis] %d movies in CL billboard", len(movies))

        all_events: list[dict[str, Any]] = []
        seen_source_urls: set[str] = set()

        for movie in movies:
            movie_id = movie.get("id") or ""
            if not movie_id:
                continue

            logger.info("[cinepolis] Fetching showtimes for %r", movie.get("name"))
            time.sleep(REQUEST_DELAY)

            data = self._fetch_billboard(movie_id, cinema_ids)
            billboard = (data or {}).get("billboard") or {}
            schedules = billboard.get("schedules") or []

            movie_events = 0
            for sched in schedules:
                cinema_id   = sched.get("cinemaId") or ""
                cinema_info = cinema_map.get(cinema_id, {})
                if not cinema_info:
                    continue

                for date_entry in sched.get("dates") or []:
                    date = date_entry.get("date") or ""
                    for lang_entry in date_entry.get("languages") or []:
                        language = lang_entry.get("language") or ""
                        for showtime in lang_entry.get("showtimes") or []:
                            ev = self._build_event(movie, showtime, cinema_info, date, language)
                            if ev is None:
                                continue
                            src = ev["source_url"]
                            if src in seen_source_urls:
                                continue
                            seen_source_urls.add(src)
                            all_events.append(ev)
                            movie_events += 1

                            if self.max_events and len(all_events) >= self.max_events:
                                break
                        if self.max_events and len(all_events) >= self.max_events:
                            break
                    if self.max_events and len(all_events) >= self.max_events:
                        break
                if self.max_events and len(all_events) >= self.max_events:
                    break

            logger.info("[cinepolis] %r → %d session events", movie.get("name"), movie_events)

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[cinepolis] Total events collected: %d", len(all_events))

        if not all_events and not self.max_events:
            raise RuntimeError(
                "[cinepolis] 0 events collected on a full run — "
                "billboard API returned no showtimes. "
                "Check API key, country-id header, and network access."
            )

        return all_events

    # ── Debug helper ──────────────────────────────────────────────────────────

    def _print_debug(self, events: list[dict[str, Any]], n: int = 8) -> None:
        from collections import Counter
        print("\n" + "=" * 70)
        print("DEBUG — CinepolisScraper (GraphQL)")
        print("=" * 70)
        print(f"\nTotal events fetched: {len(events)}")
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

    parser = argparse.ArgumentParser(description="Cinépolis Chile scraper (GraphQL)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print sample events — no DB writes")
    parser.add_argument("--max-events", type=int, default=0, help="Stop after N events (0 = unlimited)")
    args = parser.parse_args()

    scraper = CinepolisScraper(max_events=args.max_events, debug=args.dry_run)
    events  = scraper.fetch_events()

    if args.dry_run:
        scraper._print_debug(events, n=10)
    else:
        from scrapers.base_scraper import make_scraper_session
        from scrapers import classifier, enricher, deduplicator
        from datetime import timezone

        engine, db = make_scraper_session()
        now   = datetime.now(timezone.utc)
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

        print(
            f"\nDone — created={stats['created']}  updated={stats['updated']}  "
            f"skipped={stats['skipped']}  failed={stats['failed']}"
        )
