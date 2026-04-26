"""Cinemark Chile scraper — fetches movie sessions from the Vista API.

Calls the Vista Entertainment Solutions REST API behind api.cinemark.cl:

    GET /api/vista/data/billboard?cinema_id={id}
        → Current movies + all scheduled sessions for one cinema.

    GET /api/vista/data/releases
        → Coming-soon movies (no sessions yet, just release dates).

    GET /api/vista/data/getMovie?corporate_film_id={id}
        → Full movie detail: synopsis, poster, cast.

Each scheduled session becomes one Event row in the DB.
Coming-soon movies become one Event row per film (no time_start).

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
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ── API constants ──────────────────────────────────────────────────────────────

API_BASE = "https://api.cinemark.cl/api/vista/data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": "https://www.cinemark.cl/",
    "Origin": "https://www.cinemark.cl",
}

REQUEST_DELAY = 1  # seconds between API calls (lighter than HTML scraping)

# Ticket booking page base URL.
# Constructed as: TICKET_URL_BASE + movie slug + query params for session.
# This is the public movie page URL — it contains a session selector.
MOVIE_PAGE_BASE = "https://www.cinemark.cl/pelicula"

# ── Cinema ID map ─────────────────────────────────────────────────────────────
#
# Maps Cinemark Chile's internal cinema_id (the "tag" URL parameter on
# cinemark.cl/cines) to the human-readable venue name that matches the
# existing Venue rows in seed.sql.
#
# How to find a cinema's tag value:
#   1. Visit https://www.cinemark.cl/cines in a browser.
#   2. Click any cinema — the URL becomes /cine?tag=NNN&cine=slug.
#   3. NNN is the cinema_id; slug is the key column below.
#
# IDs confirmed by visiting cinemark.cl/cine?tag=NNN for each active ID
# and matching the "CINE SELECCIONADO: ..." page title (April 2026).
# Active IDs probed via GET /api/vista/data/billboard?cinema_id=N.
#
# Format:  cinema_id (int) → venue_name matching seed.sql Venue.name

CINEMA_MAP: dict[int, str] = {
    # ── Santiago RM — all confirmed via cinemark.cl/cine?tag=NNN (April 2026) ──
    511: "Cinemark Mallplaza Vespucio",           # La Florida
    512: "Cinemark Alto Las Condes",              # Las Condes
    513: "Cinemark Mallplaza Oeste",              # Cerrillos
    519: "Cinemark Plaza Tobalaba",               # Puente Alto
    572: "Cinemark Plaza Norte",                  # Huecharaba
    2300: "Cinemark Portal Ñuñoa",               # Ñuñoa
    2307: "Cinemark Mid Mall Maipú",             # Maipú
    2310: "Cinemark Espacio Urbano Gran Avenida", # San Miguel
    # ── Non-Santiago (excluded — no matching DB venue rows) ───────────────────
    # 514: Espacio Urbano Viña del Mar  (tag=514)
    # 517: unknown location             (tag=517, active but unidentified)
    # 520: Mallplaza Iquique            (tag=520)
    # 548: Mallplaza Trébol Talcahuano  (tag=548)
    # 570: Mall Marina Viña del Mar     (tag=570)
    # 2301: Portal Osorno               (tag=2301)
    # 2302: Mallplaza Mirador Bío Bío   (tag=2302)
    # 2303: Open Rancagua               (tag=2303)
    # 2304: Open Ovalle                 (tag=2304)
    # 2305: Mallplaza Arica             (tag=2305)
    # 2306: Arauco Coronel              (tag=2306)
    # 2308: Open La Calera              (tag=2308)
    # 2309: Mallplaza La Serena         (tag=2309)
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_showtime(raw: str) -> tuple[str, str] | tuple[None, None]:
    """Parse an ISO-8601 showtime string into (date, time_start).

    Accepts: "2026-04-23T17:15:00"  or  "2026-04-23T17:15:00-04:00"
    Returns: ("2026-04-23", "17:15") or (None, None) on failure.
    """
    if not raw:
        return None, None
    try:
        # Strip timezone suffix for simple parsing
        dt_str = raw[:19]  # "2026-04-23T17:15:00"
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except (ValueError, TypeError):
        logger.debug("Cannot parse showtime %r", raw)
        return None, None


def _parse_release_date(raw: str) -> str | None:
    """Parse a release date string to YYYY-MM-DD.

    Vista may return "2026-04-23", "23/04/2026", or an ISO timestamp.
    """
    if not raw:
        return None
    # ISO date or timestamp prefix
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    # DD/MM/YYYY
    parts = raw.split("/")
    if len(parts) == 3:
        try:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{y:04d}-{m:02d}-{d:02d}"
        except ValueError:
            pass
    return None


def _build_source_url(cinema_id: int, corporate_film_id: str, date: str, time_start: str) -> str:
    """Stable deduplication key for a single showtime session.

    Does NOT use session_id because that can change between scrape runs
    when the booking system is updated. cinema+film+date+time is stable.
    """
    return (
        f"cinemark:cl:{cinema_id}:{corporate_film_id}:{date}:{time_start}"
    )


def _build_release_source_url(corporate_film_id: str) -> str:
    """Deduplication key for a coming-soon movie (no session yet)."""
    return f"cinemark:cl:release:{corporate_film_id}"


def _build_ticket_url(cinema_id: int, corporate_film_id: str, pelicula_slug: str) -> str:
    """Best-effort public URL for the movie page at this cinema.

    The user can navigate from here to select their session and buy tickets.
    Format mirrors what the Cinemark Chile widget builds:
        /pelicula?tag={cinema_id}&corporate_film_id={film_id}&pelicula={slug}
    """
    slug = pelicula_slug or corporate_film_id
    return (
        f"{MOVIE_PAGE_BASE}"
        f"?tag={cinema_id}"
        f"&corporate_film_id={corporate_film_id}"
        f"&pelicula={slug}"
    )


def _extract_poster(film: dict[str, Any]) -> str | None:
    """Return the best available poster image URL from a film dict."""
    for key in ("PosterDynamic", "GraphicUrl", "graphic_url", "Poster", "posterUrl", "imageUrl"):
        val = film.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    return None


def _slug(title: str) -> str:
    """Convert a movie title to a URL-safe slug (lowercase, hyphens)."""
    s = title.lower().strip()
    s = re.sub(r"[áàä]", "a", s)
    s = re.sub(r"[éèë]", "e", s)
    s = re.sub(r"[íìï]", "i", s)
    s = re.sub(r"[óòö]", "o", s)
    s = re.sub(r"[úùü]", "u", s)
    s = re.sub(r"[ñ]", "n", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── Main scraper class ─────────────────────────────────────────────────────────


class CinemarkScraper(BaseScraper):
    """Fetches Cinemark Chile movie sessions via the Vista REST API."""

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
            include_releases: If True, also fetch coming-soon movies and
                              create placeholder events for them.
            max_events:       Stop after this many events (0 = unlimited).
            debug:            Print API responses without writing to DB.
        """
        super().__init__()
        self.cinema_ids = cinema_ids or list(CINEMA_MAP.keys())
        self.include_releases = include_releases
        self.max_events = max_events
        self.debug = debug

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        # Cache: corporate_film_id → detail dict (avoids re-fetching same film
        # across multiple cinema billboards).
        self._film_detail_cache: dict[str, dict[str, Any]] = {}

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

    def _fetch_billboard(self, cinema_id: int) -> Any:
        """GET /billboard?cinema_id={id} → raw JSON."""
        time.sleep(REQUEST_DELAY)
        url = f"{API_BASE}/billboard"
        data = self._get_json(url, params={"cinema_id": cinema_id})
        if data is None:
            logger.warning("[cinemark] Empty billboard for cinema_id=%d", cinema_id)
        else:
            logger.debug("[cinemark] billboard cinema=%d  items=%s", cinema_id,
                         len(data) if isinstance(data, list) else "?")
        return data

    def _fetch_releases(self) -> Any:
        """GET /releases → raw JSON (coming-soon films)."""
        time.sleep(REQUEST_DELAY)
        url = f"{API_BASE}/releases"
        data = self._get_json(url)
        if data is None:
            logger.warning("[cinemark] Empty releases response")
        return data

    def _fetch_film_detail(self, corporate_film_id: str) -> dict[str, Any]:
        """GET /getMovie?corporate_film_id={id} → film detail dict.

        NOTE: As of 2026-04 this endpoint returns 404. The billboard response
        already contains title, synopsis, graphic_url and rating, so we skip
        the network call and return an empty dict to avoid 404 spam.
        """
        return {}

    # ── Billboard → event list ────────────────────────────────────────────────

    def _films_from_billboard(self, raw: Any) -> list[dict[str, Any]]:
        """Normalise the billboard response into a list of film dicts with Sessions.

        Current API returns a date-grouped structure:
          [{date: "2026-04-26", movies: [{..., movie_versions: [{sessions: [...]}]}]}, ...]

        Each movie is deduplicated by corporate_film_id; all sessions across all
        dates and versions are flattened into a "Sessions" list on the film dict.
        A "Format" key is injected into each session from the version title.

        Legacy flat-list format is also handled for backwards compatibility.
        """
        if not isinstance(raw, list) or not raw:
            if isinstance(raw, dict):
                for key in ("PremieresBillboard", "Films", "films", "movies", "Movies", "data", "results"):
                    val = raw.get(key)
                    if isinstance(val, list):
                        return val
                for val in raw.values():
                    if isinstance(val, list):
                        return val
            return []

        # Detect new date-grouped format: first item has a "movies" key
        if isinstance(raw[0], dict) and "movies" in raw[0]:
            films_by_id: dict[str, dict[str, Any]] = {}
            for date_entry in raw:
                for movie in (date_entry.get("movies") or []):
                    film_id = (
                        movie.get("corporate_film_id")
                        or movie.get("CorporateFilmId")
                        or ""
                    )
                    if not film_id:
                        continue
                    if film_id not in films_by_id:
                        films_by_id[film_id] = {**movie, "Sessions": []}
                    for version in (movie.get("movie_versions") or []):
                        # Extract the projection format code from version title.
                        # Version title format: "MOVIE TITLE (2D PRE NT DOB)" → "2D"
                        v_title = version.get("title", "")
                        fmt_match = re.search(r"\(([^)]+)\)", v_title)
                        fmt_tokens = fmt_match.group(1).split() if fmt_match else []
                        fmt_code = fmt_tokens[0].upper() if fmt_tokens else ""
                        for session in (version.get("sessions") or []):
                            films_by_id[film_id]["Sessions"].append(
                                {**session, "Format": fmt_code}
                            )
            return list(films_by_id.values())

        # Legacy: bare list of film dicts
        return raw

    def _get_film_id(self, film: dict[str, Any]) -> str | None:
        """Extract corporate_film_id from a film dict (handles key variants)."""
        for key in (
            "CorporateFilmId", "corporate_film_id",
            "ScheduledFilmId", "FilmId", "filmId", "id",
        ):
            val = film.get(key)
            if val and isinstance(val, str):
                return val
        return None

    def _get_film_title(self, film: dict[str, Any]) -> str | None:
        """Extract title from a film dict."""
        for key in ("Title", "title", "Name", "name", "MovieTitle"):
            val = film.get(key)
            if val and isinstance(val, str):
                return val.strip()
        return None

    def _get_sessions(self, film: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract the sessions list from a film dict."""
        for key in ("Sessions", "sessions", "Showtimes", "showtimes", "Times"):
            val = film.get(key)
            if isinstance(val, list):
                return val
        return []

    def _get_session_showtime(self, session: dict[str, Any]) -> str | None:
        """Extract the ISO showtime string from a session dict."""
        for key in ("Showtime", "showtime", "StartTime", "start_time", "SessionTime"):
            val = session.get(key)
            if val and isinstance(val, str):
                return val
        return None

    def _get_session_format(self, session: dict[str, Any]) -> str:
        """Extract display format (2D, 3D, IMAX, etc.) from a session dict."""
        for key in ("FormatCode", "format_code", "Format", "format", "ScreenType"):
            val = session.get(key)
            if val and isinstance(val, str):
                return val.upper()
        return ""

    def _get_session_price(self, session: dict[str, Any]) -> list[float] | None:
        """Extract price_range from a session dict."""
        for key in ("PriceGroupCode", "Price", "price", "TicketPrice"):
            val = session.get(key)
            if val is None:
                continue
            try:
                amount = float(val)
                if amount > 0:
                    return [amount, amount]
                if amount == 0:
                    return [0.0, 0.0]
            except (ValueError, TypeError):
                pass
        return None

    # ── Event builder ─────────────────────────────────────────────────────────

    def _build_session_event(
        self,
        film: dict[str, Any],
        detail: dict[str, Any],
        session: dict[str, Any],
        cinema_id: int,
        venue_name: str,
    ) -> dict[str, Any] | None:
        """Build one event dict from a single scheduled session.

        Returns None if mandatory fields (name, date) cannot be extracted.
        """
        corporate_film_id = self._get_film_id(film)
        if not corporate_film_id:
            return None

        title = self._get_film_title(film) or self._get_film_title(detail)
        if not title:
            return None

        raw_showtime = self._get_session_showtime(session)
        date, time_start = _parse_showtime(raw_showtime)
        if not date:
            logger.debug("Skipping session — unparseable showtime %r for %r", raw_showtime, title)
            return None

        fmt = self._get_session_format(session)
        # Append format to name when non-standard (3D, IMAX, etc.)
        display_name = f"{title} ({fmt})" if fmt and fmt != "2D" else title

        poster = _extract_poster(film) or _extract_poster(detail)

        synopsis = (
            detail.get("Synopsis")
            or detail.get("synopsis")
            or detail.get("Description")
            or detail.get("description")
            or film.get("Synopsis")
            or film.get("synopsis")
        )
        if synopsis:
            synopsis = synopsis.strip()[:1500]

        price_range = self._get_session_price(session)

        ticket_url = _build_ticket_url(cinema_id, corporate_film_id, _slug(title))
        source_url = _build_source_url(cinema_id, corporate_film_id, date, time_start)

        event: dict[str, Any] = {
            "name": display_name,
            "date": date,
            "time_start": time_start,
            "source_url": source_url,
            "url": ticket_url,
            "venue_name": venue_name,
            # Hard-lock category so classifier never overrides Cine events
            "category": "Cine",
            "type": "Cine",
            "_locked_category": "Cine",
            "kids_friendly": False,
        }

        if poster:
            event["image_url"] = poster
        if synopsis:
            event["description"] = synopsis
        if price_range:
            event["price_range"] = price_range

        return event

    def _build_release_event(
        self,
        film: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Build one event dict for a coming-soon film (no session data).

        Uses release date as the event date; no time_start.
        """
        corporate_film_id = self._get_film_id(film)
        if not corporate_film_id:
            return None

        title = self._get_film_title(film) or self._get_film_title(detail)
        if not title:
            return None

        # Try multiple date key variants Vista may use
        raw_date = (
            film.get("OpeningDate")
            or film.get("ReleaseDate")
            or film.get("release_date")
            or film.get("openingDate")
            or detail.get("OpeningDate")
            or detail.get("ReleaseDate")
        )
        date = _parse_release_date(str(raw_date)) if raw_date else None
        if not date:
            # Default: today + 14 days (better than dropping the record entirely)
            from datetime import timedelta
            date = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")

        poster = _extract_poster(film) or _extract_poster(detail)
        synopsis = (
            detail.get("Synopsis")
            or detail.get("synopsis")
            or detail.get("Description")
            or film.get("Synopsis")
        )
        if synopsis:
            synopsis = synopsis.strip()[:1500]

        source_url = _build_release_source_url(corporate_film_id)
        # Point to the general peliculas page since no cinema is selected yet
        ticket_url = f"{MOVIE_PAGE_BASE}?corporate_film_id={corporate_film_id}&coming_soon=true"

        event: dict[str, Any] = {
            "name": title,
            "date": date,
            "source_url": source_url,
            "url": ticket_url,
            "category": "Cine",
            "type": "Cine",
            "_locked_category": "Cine",
            "kids_friendly": False,
        }

        if poster:
            event["image_url"] = poster
        if synopsis:
            event["description"] = synopsis

        return event

    # ── Public fetch_events ───────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch all sessions from configured cinemas + coming-soon films.

        Pipeline:
          1. For each cinema_id in self.cinema_ids:
               a. GET /billboard?cinema_id={id}
               b. For each film in the billboard:
                  - GET /getMovie?corporate_film_id={id}  (cached)
                  - For each session → build one event dict
          2. If include_releases:
               a. GET /releases
               b. For each film → build one coming-soon event dict

        Returns a flat list of event dicts ready for classifier + enricher.
        """
        all_events: list[dict[str, Any]] = []
        seen_source_urls: set[str] = set()

        # ── 1. Billboard (current sessions) ─────────────────────────────────
        for cinema_id in self.cinema_ids:
            venue_name = CINEMA_MAP.get(cinema_id, f"Cinemark {cinema_id}")
            logger.info("[cinemark] Scraping billboard for %r (cinema_id=%d)", venue_name, cinema_id)

            raw = self._fetch_billboard(cinema_id)
            films = self._films_from_billboard(raw)

            if not films:
                logger.warning("[cinemark] No films found for cinema_id=%d", cinema_id)
                continue

            cinema_events = 0
            for film in films:
                corporate_film_id = self._get_film_id(film)
                if not corporate_film_id:
                    continue

                # Fetch detail once per film (cached across cinemas)
                detail = self._fetch_film_detail(corporate_film_id)

                sessions = self._get_sessions(film)
                if not sessions:
                    logger.debug("[cinemark] Film %r has no sessions at cinema %d",
                                 self._get_film_title(film), cinema_id)
                    continue

                for session in sessions:
                    ev = self._build_session_event(film, detail, session, cinema_id, venue_name)
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

                if self.max_events and len(all_events) >= self.max_events:
                    break

            logger.info("[cinemark] cinema_id=%d  %r → %d session events",
                        cinema_id, venue_name, cinema_events)

            if self.max_events and len(all_events) >= self.max_events:
                break

        # ── 2. Coming-soon releases ──────────────────────────────────────────
        if self.include_releases and not (self.max_events and len(all_events) >= self.max_events):
            logger.info("[cinemark] Fetching coming-soon releases")
            raw_releases = self._fetch_releases()
            release_films = self._films_from_billboard(raw_releases)

            release_events = 0
            for film in release_films:
                corporate_film_id = self._get_film_id(film)
                if not corporate_film_id:
                    continue

                detail = self._fetch_film_detail(corporate_film_id)
                ev = self._build_release_event(film, detail)
                if ev is None:
                    continue

                src = ev["source_url"]
                if src in seen_source_urls:
                    continue
                seen_source_urls.add(src)

                all_events.append(ev)
                release_events += 1

                if self.max_events and len(all_events) >= self.max_events:
                    break

            logger.info("[cinemark] %d coming-soon events added", release_events)

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
                f"  category  : {ev.get('category')}\n"
                f"  source_url: {ev.get('source_url')}\n"
                f"  url       : {ev.get('url')}\n"
                f"  image_url : {ev.get('image_url')}\n"
                f"  price     : {ev.get('price_range')}\n"
                f"  desc      : {str(ev.get('description', ''))[:120]!r}"
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
        db.close()
        engine.dispose()

        print(f"\nDone — created={stats['created']}  updated={stats['updated']}  "
              f"skipped={stats['skipped']}  failed={stats['failed']}")
