"""Venue enricher.

Given a venue name string extracted by the scraper, tries to find
the matching Venue row in the database and adds venue_id (and the
venue's type for the classifier) to the event dict.

Match strategy (in order):
  1. Exact match — case-insensitive equality on full scraped name.
  2. DB name is contained in the scraped name (substring check via
     candidates sharing the first significant word).
  3. Scraped name is contained in the DB name.
  4. Normalize: strip " - Commune" suffix PuntoTicket appends, then
     strip common venue-type prefixes (Teatro, Club, Cúpula, …) and
     retry exact + substring matching on the core name.
  5. Any-word: try every word of 5+ characters from the scraped name
     as a SQL search key; accept the first candidate whose DB name is
     a substring of the scraped name (handles prefix mismatches).
  6. Accent-stripped fallback: compare names with accents removed so
     "Teatro Caupolican" matches DB row "Teatro Caupolicán".
  7. Auto-create: if all 6 steps fail, insert a new Venue row with the
     scraped name, venue_type inferred from the event category, and
     coordinates defaulting to Santiago center. A near-exact normalized
     check runs first to avoid creating near-duplicates.
"""
from __future__ import annotations

import logging
import os
import sys
import unicodedata
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Venue-type prefix stripping ───────────────────────────────────────────────

# Common venue-type prefixes PuntoTicket (and seed data) prepend to venue names.
# Stripping these lets "Teatro Caupolicán" match DB row "Caupolicán", etc.
_VENUE_PREFIXES: list[str] = [
    "cúpula ",
    "teatro ",
    "club ",
    "bar ",
    "sala ",
    "centro cultural ",
    "espacio ",
    "centro de eventos ",
    "auditorio ",
    "parque ",
]

# ── Category → venue_type mapping for auto-creation ──────────────────────────

# Keywords (lowercased) mapped to a canonical venue_type string.
# Evaluated in order — first match wins.
_CATEGORY_VENUE_TYPE: list[tuple[str, str]] = [
    ("cine",           "Cine"),
    ("teatro",         "Teatro"),
    ("comedia",        "Comedia"),         # Comedy venues get their own type
    ("familiar",       "Teatro"),
    ("familia",        "Teatro"),          # "Familia" is now the canonical category
    ("música",         "Sala de Concierto"),
    ("musica",         "Sala de Concierto"),
    ("concierto",      "Sala de Concierto"),
    ("electrónica",    "Bar"),             # DJ / club events → Bar, not Sala
    ("electronica",    "Bar"),
    ("vida nocturna",  "Bar"),
    ("nocturna",       "Bar"),
    ("fiesta",         "Bar"),
    ("fiestas",        "Bar"),
    ("club",           "Bar"),
    ("bar",            "Bar"),
    ("sunset",         "Bar"),             # happy hour / rooftop events
    ("museo",          "Museo"),
    ("cultural",       "Museo"),
    ("gam",            "Museo"),
    ("matucana",       "Museo"),
    ("arte",           "Museo"),
    ("exposicion",     "Museo"),
    ("exposición",     "Museo"),
]

# Default venue_type when no category signal matches
_DEFAULT_VENUE_TYPE = "Espacio Cultural"

# Coordinates for Santiago city centre (Plaza de Armas)
# Used as a placeholder when the scraper cannot resolve coordinates.
_SANTIAGO_CENTRE: list[float] = [-33.4372, -70.6506]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    """Return s with all diacritics removed: 'Caupolicán' → 'Caupolican'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _normalize(name: str) -> str:
    """Return a stripped-down version of a scraped venue name.

    1. Remove the " - Commune" suffix PuntoTicket appends to every name
       (e.g. "Cúpula Parque O'Higgins - Santiago Centro" → "Cúpula Parque O'Higgins").
    2. Strip leading venue-type prefix so the remaining core can be
       matched against shorter DB names
       (e.g. "Cúpula Parque O'Higgins" → "Parque O'Higgins").
    """
    if " - " in name:
        name = name.split(" - ")[0].strip()

    lower = name.lower()
    for prefix in _VENUE_PREFIXES:
        if lower.startswith(prefix):
            return name[len(prefix):].strip()

    return name


def _infer_venue_type(event: dict[str, Any]) -> str:
    """Infer a venue_type string from the event's category, type, and name.

    Checks category → type → name fields (in that priority order) against
    _CATEGORY_VENUE_TYPE keyword rules.
    """
    signals = " ".join(
        str(event.get(f, "") or "") for f in ("category", "type", "name")
    ).lower()

    # Also strip accents from the signal string so "música" matches "musica"
    signals_plain = _strip_accents(signals)

    for keyword, venue_type in _CATEGORY_VENUE_TYPE:
        keyword_plain = _strip_accents(keyword)
        if keyword_plain in signals_plain:
            return venue_type

    return _DEFAULT_VENUE_TYPE


def _candidates_for_word(word: str, db: Session, Venue: Any) -> list[Any]:
    """Return all DB venues whose name contains *word* (case-insensitive)."""
    return (
        db.query(Venue)
        .filter(func.lower(Venue.name).contains(word.lower()))
        .all()
    )


def _near_exact_match(name: str, db: Session, Venue: Any) -> Any | None:
    """Check for a near-exact match using accent-stripped comparison.

    Loads all venues whose accent-stripped name matches the accent-stripped
    scraped name. Returns the first match, or None.

    This prevents creating "Teatro Caupolican" alongside "Teatro Caupolicán".
    """
    stripped_target = _strip_accents(name.lower())

    # Use first significant word (5+ chars) to narrow the candidate set
    first_word = next(
        (w for w in stripped_target.split() if len(w) >= 5), ""
    )
    if not first_word:
        return None

    # DB query on first word (accent-insensitive via unaccent would be ideal,
    # but we keep it DB-agnostic by filtering in Python after a broad query)
    candidates = _candidates_for_word(first_word, db, Venue)
    # Also try without first word in case accents differ there
    if not candidates:
        plain_words = _strip_accents(name.lower()).split()
        for w in plain_words:
            if len(w) >= 5:
                candidates = _candidates_for_word(w, db, Venue)
                if candidates:
                    break

    for c in candidates:
        if _strip_accents(c.name.lower()) == stripped_target:
            return c

    return None


def _create_venue(venue_name: str, event: dict[str, Any], db: Session, Venue: Any) -> Any:
    """Insert a new Venue row and return the ORM instance.

    Fields populated:
      name          — scraped venue name (title-cased if all-lowercase)
      venue_type    — inferred from event category/type/name
      coordinates   — from event["coordinates"] if present, else Santiago centre
      city          — "Santiago" (default; all auto-created venues are RM)
      is_verified   — False
      scraped_at    — now (UTC)
      description, cover_image_url, profile_image_url, website_url — NULL

    The caller is responsible for flushing/committing the session.
    """
    venue_type = _infer_venue_type(event)
    coords = event.get("coordinates") or _SANTIAGO_CENTRE

    # Normalise casing: if name is ALL CAPS or all lowercase, title-case it
    if venue_name == venue_name.upper() or venue_name == venue_name.lower():
        display_name = venue_name.title()
    else:
        display_name = venue_name

    venue = Venue(
        name=display_name,
        venue_type=venue_type,
        coordinates=coords,
        city="Santiago",
        is_verified=False,
        scraped_at=datetime.now(timezone.utc),
        description=None,
        cover_image_url=None,
        profile_image_url=None,
        website_url=None,
    )
    db.add(venue)
    db.flush()   # assigns venue.id without committing

    logger.info(
        "Auto-created venue %r (id=%d  type=%s)",
        display_name, venue.id, venue_type,
    )
    return venue


# ── Public API ────────────────────────────────────────────────────────────────

def enrich(event: dict[str, Any], db: Session) -> dict[str, Any]:
    """Resolve venue_name → venue_id and add venue metadata to the event dict.

    Args:
        event:  Mutable event dict.  Uses event["venue_name"] for lookup.
        db:     Active SQLAlchemy session (read + optional write for auto-create).

    Returns:
        The same dict, possibly with "venue_id" and "venue_type" added.

    Match order:
        1 → exact case-insensitive
        2 → DB name substring of scraped name
        3 → scraped name substring of DB name
        4 → normalized (strip suffix + prefix) exact + substring
        5 → any 5+ char word from scraped name
        6 → accent-stripped near-exact
        7 → auto-create new Venue row (with near-exact dedup check)
    """
    from app.db.models import Venue  # noqa: PLC0415

    venue_name: str = (event.get("venue_name") or "").strip()
    if not venue_name:
        return event

    venue_name_lower = venue_name.lower()
    venue = None

    # ── 1. Exact match (case-insensitive) ─────────────────────────────────────
    venue = (
        db.query(Venue)
        .filter(func.lower(Venue.name) == venue_name_lower)
        .first()
    )

    # ── 2. DB name is a substring of the scraped name ─────────────────────────
    if venue is None:
        first_word = next(
            (w for w in venue_name_lower.split() if len(w) >= 4), ""
        )
        if first_word:
            for candidate in _candidates_for_word(first_word, db, Venue):
                if candidate.name.lower() in venue_name_lower:
                    venue = candidate
                    break

    # ── 3. Scraped name is a substring of the DB name ─────────────────────────
    if venue is None:
        venue = (
            db.query(Venue)
            .filter(func.lower(Venue.name).contains(venue_name_lower))
            .first()
        )

    # ── 4. Normalize: strip " - Commune" suffix and venue-type prefix ─────────
    if venue is None:
        normalized = _normalize(venue_name)
        normalized_lower = normalized.lower()

        if normalized_lower != venue_name_lower:
            # 4a. Exact on normalized core name
            venue = (
                db.query(Venue)
                .filter(func.lower(Venue.name) == normalized_lower)
                .first()
            )
            # 4b. DB name contains normalized core name
            if venue is None:
                venue = (
                    db.query(Venue)
                    .filter(func.lower(Venue.name).contains(normalized_lower))
                    .first()
                )
            # 4c. Normalized core name contains DB name
            if venue is None:
                first_norm_word = next(
                    (w for w in normalized_lower.split() if len(w) >= 4), ""
                )
                if first_norm_word:
                    for candidate in _candidates_for_word(first_norm_word, db, Venue):
                        if candidate.name.lower() in normalized_lower:
                            venue = candidate
                            break

    # ── 5. Any-word: try every 5+ char word from the full scraped name ─────────
    if venue is None:
        for word in venue_name_lower.split():
            if len(word) < 5:
                continue
            for candidate in _candidates_for_word(word, db, Venue):
                if candidate.name.lower() in venue_name_lower:
                    venue = candidate
                    break
            if venue:
                break

    # ── 6. Accent-stripped near-exact match ────────────────────────────────────
    if venue is None:
        stripped_scraped = _strip_accents(venue_name_lower)
        first_word = next((w for w in venue_name_lower.split() if len(w) >= 4), "")
        if first_word:
            for candidate in _candidates_for_word(first_word, db, Venue):
                if _strip_accents(candidate.name.lower()) == stripped_scraped:
                    venue = candidate
                    break

    # ── 7. Auto-create ─────────────────────────────────────────────────────────
    if venue is None:
        # Final dedup check: accent-stripped near-exact across the full table
        # (prevents duplicates caused by accentuation differences not caught
        # by step 6 because the first-word query had zero candidates)
        near = _near_exact_match(venue_name, db, Venue)
        if near is not None:
            logger.debug(
                "Near-exact match (accent-stripped) %r → %r (id=%d)",
                venue_name, near.name, near.id,
            )
            venue = near
        else:
            try:
                venue = _create_venue(venue_name, event, db, Venue)
            except Exception as exc:
                logger.warning(
                    "Auto-create venue failed for %r: %s — saving without venue_id",
                    venue_name, exc,
                )

    # ── Result ────────────────────────────────────────────────────────────────
    if venue is not None:
        event["venue_id"] = venue.id
        if not event.get("venue_type"):
            event["venue_type"] = venue.venue_type
        logger.debug(
            "Matched venue %r → id=%d  type=%s",
            venue_name, venue.id, venue.venue_type,
        )
    else:
        logger.debug("No venue match for %r — saving without venue_id", venue_name)

    return event
