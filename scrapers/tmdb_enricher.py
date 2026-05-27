"""TMDB metadata enricher for cinema events.

After a cinema scraper run, call apply_tmdb_to_cinema_events(db) to:
  1. Find all events with category='Cine' that lack a description or
     a YouTube community link.
  2. For each unique base movie title, fetch:
       - Spanish overview (description)
       - YouTube trailer URL
       - Runtime and vote_average (appended to description footer)
  3. Write description back to events.description (if currently empty).
  4. Upsert an EventCommunityLink row with platform='youtube'.

Auth: Bearer token via TMDB_READ_ACCESS_TOKEN env var.
Docs: https://developer.themoviedb.org/docs/getting-started

Run standalone:
    python scrapers/tmdb_enricher.py                  # enrich all Cine events
    python scrapers/tmdb_enricher.py --title "Wicked" # single title dry-run
    python scrapers/tmdb_enricher.py --dry-run        # fetch only, no DB writes
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
import unicodedata
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"
REQUEST_DELAY = 0.3  # seconds between TMDB API calls

# In-memory cache: normalized_title → metadata dict (or None if not found)
_cache: dict[str, dict | None] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _auth_headers() -> dict[str, str]:
    token = os.getenv("TMDB_READ_ACCESS_TOKEN", "")
    if not token:
        raise EnvironmentError(
            "TMDB_READ_ACCESS_TOKEN env var is not set. "
            "Get a free key at https://www.themoviedb.org/settings/api"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _normalize_title(title: str) -> str:
    """Lower-case, accent-stripped title used as cache key."""
    s = unicodedata.normalize("NFD", title.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def strip_format_suffix(title: str) -> str:
    """Remove trailing format/year annotations.

    Handles both Cinépolis-style '(3D SUBT)' and Cinemark-style '[2004]'.
    'MOVIE (3D SUBT)' → 'MOVIE'
    'EL CASTILLO AMBULANTE [2004]' → 'EL CASTILLO AMBULANTE'
    """
    s = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()  # strip (...)
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip()      # strip [...]
    return s


def _build_description(meta: dict) -> str:
    """Compose a description string from TMDB metadata fields.

    Format: {Spanish overview}\n\nDirector: X\nReparto: A, B, C
    """
    parts: list[str] = []
    if meta.get("overview"):
        parts.append(meta["overview"])
    credits_lines: list[str] = []
    if meta.get("director"):
        credits_lines.append(f"Director: {meta['director']}")
    if meta.get("cast"):
        credits_lines.append(f"Reparto: {', '.join(meta['cast'])}")
    if credits_lines:
        parts.append("\n".join(credits_lines))
    return "\n\n".join(parts)


# ── Public fetch ───────────────────────────────────────────────────────────────

def fetch_tmdb_metadata(title: str) -> dict[str, Any] | None:
    """Fetch TMDB metadata for a movie title.

    Returns a dict with keys:
        overview      — Spanish description string (may be empty)
        trailer_url   — YouTube trailer URL or None
        runtime       — int minutes or None
        vote_average  — float or None
        poster_url    — full image URL or None

    Returns None if the movie was not found. Results are cached by
    normalized title so repeated calls for the same film cost nothing.
    """
    base = strip_format_suffix(title)
    cache_key = _normalize_title(base)

    if cache_key in _cache:
        return _cache[cache_key]

    try:
        headers = _auth_headers()
    except EnvironmentError:
        raise

    # ── 1. Search ──────────────────────────────────────────────────────────────
    time.sleep(REQUEST_DELAY)
    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/movie",
            headers=headers,
            params={"query": base, "language": "es-419", "region": "CL"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as exc:
        logger.warning("[tmdb] Search failed for %r: %s", base, exc)
        _cache[cache_key] = None
        return None

    if not results:
        logger.debug("[tmdb] No TMDB results for %r", base)
        _cache[cache_key] = None
        return None

    movie = results[0]
    tmdb_id = movie.get("id")
    overview = (movie.get("overview") or "").strip()
    vote_average: float | None = movie.get("vote_average") or None
    poster_path = movie.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None

    # ── 2. Movie detail (runtime + richer overview) ────────────────────────────
    runtime: int | None = None
    if tmdb_id:
        time.sleep(REQUEST_DELAY)
        try:
            detail_resp = requests.get(
                f"{TMDB_BASE}/movie/{tmdb_id}",
                headers=headers,
                params={"language": "es-419"},
                timeout=10,
            )
            detail_resp.raise_for_status()
            detail = detail_resp.json()
            runtime = detail.get("runtime") or None
            # Prefer detail overview — search result overview can be truncated
            if detail.get("overview"):
                overview = detail["overview"].strip()
        except requests.RequestException as exc:
            logger.warning("[tmdb] Detail fetch failed for id=%s: %s", tmdb_id, exc)

    # ── 3. Videos (YouTube trailer) ────────────────────────────────────────────
    trailer_url: str | None = None
    if tmdb_id:
        for lang in ("es-419", "en-US"):
            time.sleep(REQUEST_DELAY)
            try:
                vid_resp = requests.get(
                    f"{TMDB_BASE}/movie/{tmdb_id}/videos",
                    headers=headers,
                    params={"language": lang},
                    timeout=10,
                )
                vid_resp.raise_for_status()
                yt_trailers = [
                    v for v in vid_resp.json().get("results", [])
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer"
                ]
                if yt_trailers:
                    key = yt_trailers[0].get("key", "")
                    if key:
                        trailer_url = f"https://www.youtube.com/watch?v={key}"
                    break
            except requests.RequestException as exc:
                logger.warning("[tmdb] Videos fetch failed (lang=%s, id=%s): %s",
                               lang, tmdb_id, exc)

    # ── 4. Credits (director + cast) ──────────────────────────────────────────
    director: str | None = None
    cast: list[str] = []
    if tmdb_id:
        time.sleep(REQUEST_DELAY)
        try:
            credits_resp = requests.get(
                f"{TMDB_BASE}/movie/{tmdb_id}/credits",
                headers=headers,
                params={"language": "es-419"},
                timeout=10,
            )
            credits_resp.raise_for_status()
            credits_data = credits_resp.json()
            for member in credits_data.get("crew", []):
                if member.get("job") == "Director":
                    director = member.get("name")
                    break
            cast = [m.get("name") for m in credits_data.get("cast", [])[:3] if m.get("name")]
        except requests.RequestException as exc:
            logger.warning("[tmdb] Credits fetch failed for id=%s: %s", tmdb_id, exc)

    meta: dict[str, Any] = {
        "overview": overview,
        "trailer_url": trailer_url,
        "runtime": runtime,
        "vote_average": vote_average,
        "poster_url": poster_url,
        "director": director,
        "cast": cast,
    }
    _cache[cache_key] = meta
    logger.info(
        "[tmdb] %-40s  trailer=%-3s  runtime=%-7s  rating=%-5s  director=%s",
        repr(base[:38]),
        "yes" if trailer_url else "no",
        f"{runtime}min" if runtime else "?",
        f"{vote_average:.1f}" if vote_average else "?",
        director or "?",
    )
    return meta


# ── DB apply ───────────────────────────────────────────────────────────────────

def apply_tmdb_to_cinema_events(db: Session) -> dict[str, int]:
    """Enrich Cine events in the DB with TMDB metadata.

    Only processes events that are missing at least one of: description,
    image_url, or a YouTube community link.  This keeps each run O(unenriched)
    rather than O(all cinema events), preventing Railway cron timeouts as the
    DB grows.

    Commits the changes and returns stats:
        {"enriched": N, "trailers_added": N, "not_found": N}
    """
    from app.db.models import Event, EventCommunityLink  # noqa: PLC0415

    stats = {"enriched": 0, "trailers_added": 0, "not_found": 0}

    # One bulk query to load all event IDs that already have a YouTube link.
    # Used below for O(1) duplicate checks, replacing a per-event N+1 query.
    existing_yt_ids: set[int] = set(
        row[0] for row in
        db.query(EventCommunityLink.event_id)
        .filter(EventCommunityLink.platform == "youtube")
        .all()
    )

    # Only fetch events that still need at least one enrichment field.
    cinema_events: list[Any] = (
        db.query(Event)
        .filter(Event.category == "Cine")
        .filter(
            Event.description.is_(None)
            | (Event.description == "")
            | Event.image_url.is_(None)
            | ~Event.id.in_(
                db.query(EventCommunityLink.event_id)
                .filter(EventCommunityLink.platform == "youtube")
            )
        )
        .all()
    )

    if not cinema_events:
        logger.info("[tmdb] All Cine events already fully enriched — nothing to do")
        return stats

    # Group by base title so we fetch TMDB once per film
    title_groups: dict[str, list[Any]] = {}
    for ev in cinema_events:
        base = strip_format_suffix(ev.name or "")
        if not base:
            continue
        title_groups.setdefault(base, []).append(ev)

    logger.info(
        "[tmdb] %d unique movie titles across %d Cine events needing enrichment",
        len(title_groups), len(cinema_events),
    )

    for base_title, group in title_groups.items():
        try:
            meta = fetch_tmdb_metadata(base_title)
        except EnvironmentError:
            raise  # propagate missing API key immediately
        except Exception as exc:
            logger.warning("[tmdb] Skipping %r: %s", base_title, exc)
            continue

        if meta is None:
            stats["not_found"] += 1
            continue

        description = _build_description(meta)
        trailer_url = meta.get("trailer_url")
        poster_url = meta.get("poster_url")

        for i, ev in enumerate(group):
            changed = False

            if description and ev.description != description:
                ev.description = description
                changed = True

            if poster_url and ev.image_url != poster_url:
                ev.image_url = poster_url
                changed = True

            if trailer_url and ev.id not in existing_yt_ids:
                db.add(EventCommunityLink(
                    event_id=ev.id,
                    platform="youtube",
                    url=trailer_url,
                ))
                existing_yt_ids.add(ev.id)
                stats["trailers_added"] += 1

            if changed:
                db.add(ev)
                stats["enriched"] += 1

            # Commit every 200 events within a title group so that high-volume
            # titles (e.g. 2000+ showtimes) don't build transactions large enough
            # to hit Railway's proxy idle-connection cutoff.
            if (i + 1) % 200 == 0:
                db.commit()

        # Final commit for the remainder of this title group.
        db.commit()
    logger.info(
        "[tmdb] Finished — enriched=%d  trailers_added=%d  not_found=%d",
        stats["enriched"], stats["trailers_added"], stats["not_found"],
    )
    return stats


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Enrich cinema events with TMDB metadata")
    parser.add_argument("--title", help="Fetch metadata for a single movie title (implies --dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch metadata but do not write to DB")
    args = parser.parse_args()

    if args.title:
        # Single-title lookup — always dry-run
        meta = fetch_tmdb_metadata(args.title)
        if meta:
            print(f"\nTMDB metadata for {args.title!r}:")
            print(f"  overview:     {(meta['overview'] or '')[:200]!r}")
            print(f"  trailer_url:  {meta['trailer_url']}")
            print(f"  runtime:      {meta['runtime']} min")
            print(f"  vote_average: {meta['vote_average']}")
            print(f"  poster_url:   {meta['poster_url']}")
        else:
            print(f"No TMDB results for {args.title!r}")
    elif args.dry_run:
        print("--dry-run requires --title for a specific movie lookup.")
    else:
        from scrapers.base_scraper import make_scraper_session
        engine, db = make_scraper_session()
        try:
            result = apply_tmdb_to_cinema_events(db)
            print(f"\nDone — enriched={result['enriched']}  "
                  f"trailers_added={result['trailers_added']}  "
                  f"not_found={result['not_found']}")
        finally:
            db.close()
            engine.dispose()
