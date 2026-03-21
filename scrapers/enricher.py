"""Venue enricher.

Given a venue name string extracted by the scraper, tries to find
the matching Venue row in the database and adds venue_id (and the
venue's type for the classifier) to the event dict.

Match strategy (in order):
  1. Exact match — case-insensitive equality.
  2. Partial match — scraped name is contained in the DB name, or vice versa.
  3. No match     — event is saved without a venue_id.
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

    # ── 1. Exact match (case-insensitive) ─────────────────────────────────────
    venue = (
        db.query(Venue)
        .filter(func.lower(Venue.name) == venue_name_lower)
        .first()
    )

    # ── 2. Partial match ──────────────────────────────────────────────────────
    if venue is None:
        # scraped name is a substring of the DB name
        venue = (
            db.query(Venue)
            .filter(func.lower(Venue.name).contains(venue_name_lower))
            .first()
        )

    if venue is None:
        # DB name is a substring of the scraped name (e.g. "Teatro Caupolicán")
        # We can't do this purely in SQL without iterating, so we load candidates
        # that share at least the first significant word.
        first_word = venue_name_lower.split()[0] if venue_name_lower.split() else ""
        if len(first_word) >= 4:
            candidates = (
                db.query(Venue)
                .filter(func.lower(Venue.name).contains(first_word))
                .all()
            )
            for candidate in candidates:
                if candidate.name.lower() in venue_name_lower:
                    venue = candidate
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
