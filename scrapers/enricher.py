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
  7. Accent-stripped near-exact (final dedup guard): runs a full accent-stripped
     comparison across the DB. If still no match, the event saves with
     venue_id=NULL — auto-create is permanently disabled.
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

# ── Known venue-name overrides ────────────────────────────────────────────────
# Applied to the scraped venue_name BEFORE any DB matching.  Maps lowercase
# scraped variants (with or without accents) to the canonical display name
# stored in the DB.  Add entries here when a scraper consistently produces a
# name that differs from the canonical DB name by more than accent alone.

VENUE_NAME_OVERRIDES: dict[str, str] = {
    "bar de rené":  "Bar de René",
    "bar de rene":  "Bar de René",
    "scd egaña":    "Sala SCD Plaza Egaña",
    "scd egana":    "Sala SCD Plaza Egaña",
    # Sala Master variants
    "salamaster":   "Sala Master",
    "sala master":  "Sala Master",
    # Museo Nacional Bellas Artes variants
    "museo nacional de bellas artes": "Museo Nacional Bellas Artes",
    "museo bellas artes":             "Museo Nacional Bellas Artes",
    "mnba":                           "Museo Nacional Bellas Artes",
    # Estadio Bicentenario La Florida
    "estadio bicentenario de la florida": "Estadio Bicentenario La Florida",
    # Auditorio UAC variant with street number
    "auditorio 641, universidad autónoma de chile": "Auditorio Universidad Autónoma de Chile",
    "auditorio 641, universidad autonoma de chile": "Auditorio Universidad Autónoma de Chile",
    # Granja Educativa spelling variant
    "ex restaurant granja educativa": "Ex Restaurante Granja Educativa",
    # Estadio Barnechea
    "estadio barnechea": "Estadio Lo Barnechea",
    # Merged duplicates
    "restaurant el 7":                                 "Restaurante El 7",
    "manhattan club":                                  "Club Manhatan",
    "club manhatan":                                   "Club Manhatan",
    "hotel pullman vitacura":                          "Hotel Pullman Santiago Vitacura",
    "montañita las vizcachas":                         "Montañita Restobar Las Vizcachas",
    "subterraneo":                                     "Club Subterráneo",
    "museo de arte contemporaneo mac quinta normal":   "MAC – Quinta Normal",
    "mac quinta normal":                               "MAC – Quinta Normal",
    # Cinépolis Parque Arauco — prefix-strip in step 4 would reduce this to
    # "Parque Arauco", which then substring-matches Teatro Mori Parque Arauco.
    "cinépolis parque arauco": "Cinépolis Parque Arauco",
    "cinepolis parque arauco": "Cinépolis Parque Arauco",
    # La Puerta Amarilla — slug variant merged into the canonical venue (id=459)
    "lapuertaamarilla":           "Bar La Puerta Amarilla",
    "la puerta amarilla":         "Bar La Puerta Amarilla",
    # MAC – Parque Forestal — park and museum share near-identical names/coords;
    # force all variants to the museum so events never land on the park.
    "parque forestal":         "MAC – Parque Forestal",
    "mac parque forestal":     "MAC – Parque Forestal",
}

# ── Source-aware venue overrides ──────────────────────────────────────────────
# When a scraper's identity is known via source_url, bypass ALL fuzzy matching
# and force a specific canonical venue name.  This prevents proximity/substring
# collisions between venues that share words or near-identical coordinates.
#
# Keys: "<scraper_prefix>:<partial_venue_name_fragment>" (both lowercased).
# The scraper prefix is the first colon-delimited segment of source_url
# (e.g. "cinepolis" from "cinepolis:cl:708:5206").
# Matched as a substring of "<scraper_prefix>:<venue_name_lower>".

SOURCE_VENUE_OVERRIDES: dict[str, str] = {
    # Cinépolis Parque Arauco events must never land on Teatro Mori Parque Arauco.
    # "parque arauco" alone is ambiguous; the scraper prefix makes it unambiguous.
    "cinepolis:parque arauco": "Cinépolis Parque Arauco",
    "cinepolis:mall parque":   "Cinépolis Parque Arauco",
}


def get_source_venue_override(source_url: str | None, scraped_venue_name: str) -> str | None:
    """Return a canonical venue name when scraper + venue name matches a known override.

    Applied before any fuzzy/proximity matching so scraper-identified events are
    always routed to the correct venue regardless of coordinate proximity or
    shared name fragments.  Returns None when no override applies.
    """
    scraper = source_url.split(":")[0].lower() if source_url else ""
    key = f"{scraper}:{scraped_venue_name.lower()}"
    for pattern, canonical in SOURCE_VENUE_OVERRIDES.items():
        if pattern in key:
            return canonical
    return None


def apply_overrides(name: str) -> str:
    """Return the canonical DB name for well-known scraper name variants."""
    return VENUE_NAME_OVERRIDES.get(name.lower().strip(), name)


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


def _candidates_for_stripped_word(word: str, db: Session, Venue: Any) -> list[Any]:
    """Return DB venues whose unaccent(lower(name)) contains *word*.

    Uses PostgreSQL's unaccent() so 'rene' matches 'René', 'caupolican'
    matches 'Caupolicán', etc.  Falls back to _candidates_for_word when
    the DB result is empty (guards against missing unaccent extension).
    """
    try:
        return (
            db.query(Venue)
            .filter(func.unaccent(func.lower(Venue.name)).contains(word.lower()))
            .all()
        )
    except Exception:
        return _candidates_for_word(word, db, Venue)


def _near_exact_match(name: str, db: Session, Venue: Any) -> Any | None:
    """Check for a near-exact match using accent-stripped comparison.

    Loads all venues whose accent-stripped name matches the accent-stripped
    scraped name. Returns the first match, or None.

    This prevents creating "Teatro Caupolican" alongside "Teatro Caupolicán"
    and "Bar de Rene" alongside "Bar de René".
    """
    stripped_target = _strip_accents(name.lower())

    # Use first significant word (4+ chars) from the stripped name to narrow
    # the candidate set.  Use accent-insensitive DB search so "rene" finds
    # DB rows containing "rené".
    first_word = next(
        (w for w in stripped_target.split() if len(w) >= 4), ""
    )
    if not first_word:
        return None

    candidates = _candidates_for_stripped_word(first_word, db, Venue)
    if not candidates:
        for w in stripped_target.split():
            if len(w) >= 4:
                candidates = _candidates_for_stripped_word(w, db, Venue)
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
        address=event.get("address") or None,
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

    # If venue_id is already set (e.g. by museum scraper), trust it and only
    # backfill venue_type — do NOT run name-matching which would override it.
    if event.get("venue_id"):
        venue = db.query(Venue).filter(Venue.id == event["venue_id"]).first()
        if venue and not event.get("venue_type"):
            event["venue_type"] = venue.venue_type
        return event

    venue_name: str = apply_overrides((event.get("venue_name") or "").strip())
    if not venue_name:
        return event

    # ── 0. Source-aware override (highest priority) ───────────────────────────
    # When source_url identifies the scraper, bypass all fuzzy/proximity matching
    # and resolve directly to the canonical venue name.  Prevents collisions
    # between venues that share name fragments or near-identical coordinates
    # (e.g. Cinépolis Parque Arauco vs Teatro Mori Parque Arauco).
    source_override = get_source_venue_override(event.get("source_url"), venue_name)
    if source_override:
        venue_name = source_override

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
    # Uses unaccent-aware DB search so "rene" finds "René", etc.
    if venue is None:
        stripped_scraped = _strip_accents(venue_name_lower)
        first_word = next((w for w in stripped_scraped.split() if len(w) >= 4), "")
        if first_word:
            for candidate in _candidates_for_stripped_word(first_word, db, Venue):
                if _strip_accents(candidate.name.lower()) == stripped_scraped:
                    venue = candidate
                    break

    # ── 7. Accent-stripped near-exact (final dedup guard) ─────────────────────
    # Auto-create is intentionally disabled: unknown venues save with venue_id=NULL
    # and venue_name_raw only. No new venue profiles are created automatically.
    if venue is None:
        near = _near_exact_match(venue_name, db, Venue)
        if near is not None:
            logger.debug(
                "Near-exact match (accent-stripped) %r → %r (id=%d)",
                venue_name, near.name, near.id,
            )
            venue = near
        else:
            logger.info(
                "No venue match for %r — saving without venue_id (auto-create disabled)",
                venue_name,
            )

    # ── Result ────────────────────────────────────────────────────────────────
    if venue is not None:
        event["venue_id"] = venue.id
        if not event.get("venue_type"):
            event["venue_type"] = venue.venue_type
        # Backfill address on the venue row if we have new data and it was blank
        if not venue.address and event.get("address"):
            venue.address = event["address"]
        # Use venue cover as fallback image when the scraper found none
        if not event.get("image_url") and venue.cover_image_url:
            event["image_url"] = venue.cover_image_url
        logger.debug(
            "Matched venue %r → id=%d  type=%s",
            venue_name, venue.id, venue.venue_type,
        )
    else:
        logger.debug("No venue match for %r — saving without venue_id", venue_name)

    return event
