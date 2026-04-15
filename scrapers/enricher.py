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
  6. No match — event is saved without a venue_id.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

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


def _normalize(name: str) -> str:
    """Return a stripped-down version of a scraped venue name.

    1. Remove the " - Commune" suffix PuntoTicket appends to every name
       (e.g. "Cúpula Parque O'Higgins - Santiago Centro" → "Cúpula Parque O'Higgins").
    2. Strip common venue-type prefixes so the remaining core can be
       matched against shorter DB names
       (e.g. "Cúpula Parque O'Higgins" → "Parque O'Higgins").
    """
    # Strip " - Commune" suffix
    if " - " in name:
        name = name.split(" - ")[0].strip()

    # Strip leading venue-type prefix (first match wins)
    lower = name.lower()
    for prefix in _VENUE_PREFIXES:
        if lower.startswith(prefix):
            return name[len(prefix):].strip()

    return name


def _candidates_for_word(word: str, db: Session, Venue: Any) -> list[Any]:
    """Return all DB venues whose name contains *word* (case-insensitive)."""
    return (
        db.query(Venue)
        .filter(func.lower(Venue.name).contains(word.lower()))
        .all()
    )


def enrich(event: dict[str, Any], db: Session) -> dict[str, Any]:
    """Resolve venue_name → venue_id and add venue metadata to the event dict.

    Args:
        event:  Mutable event dict.  Uses event["venue_name"] for lookup.
        db:     Active SQLAlchemy session (read-only queries).

    Returns:
        The same dict, possibly with "venue_id" and "venue_type" added.
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
    # Load candidates sharing the first significant word (≥4 chars) then check
    # if the full DB name string appears inside the scraped name.
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
            # 4c. Normalized core name contains DB name (substring in normalized)
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
    # Handles cases where none of the above fire because the shared token is
    # not the first word (e.g. "Cúpula Parque O'Higgins" → key="parque").
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

    # ── Result ────────────────────────────────────────────────────────────────
    if venue is not None:
        event["venue_id"] = venue.id
        # Expose venue_type so the classifier can apply VENUE_TYPE_RULES
        if not event.get("venue_type"):
            event["venue_type"] = venue.venue_type
        logger.debug(
            "Matched venue %r → id=%d  type=%s",
            venue_name,
            venue.id,
            venue.venue_type,
        )
    else:
        logger.debug("No venue match for %r — saving without venue_id", venue_name)

    return event
