"""Deduplicator — create-or-update logic keyed on source_url.

Before saving an event the deduplicator checks whether the
source_url already exists in the events table:

  • Exists   → update price_range, date, image_url, scraped_at
               Return "updated" if any field changed, "skipped" if nothing changed.
  • Not found → insert a new row.  Return "created".

All callers are responsible for calling db.commit() after the
batch is complete (BaseScraper.save_events handles this).
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Fields that are re-fetched on every scrape run and may legitimately change.
# description/image_url: enriched by TMDB or updated by source.
# venue_id/venue_name: enricher resolves venue_id on first successful match;
#   subsequent runs keep it up-to-date if the venue row changes.
# category/keywords: re-classification may improve these as the classifier rules evolve.
_MUTABLE_FIELDS = (
    "price_range", "date", "image_url", "scraped_at",
    "time_start", "time_end", "url", "is_sold_out",
    "description", "venue_id", "venue_name", "category", "keywords",
)

# All Event model columns that may appear in a scraped event dict.
_ALLOWED_FIELDS = {
    "venue_id", "name", "type", "category", "keywords", "description",
    "price_range", "date", "time_start", "time_end", "image_url", "url",
    "source_url", "is_verified", "is_sold_out", "scraped_at", "kids_friendly",
    "age_restriction",
}


def save_or_update(event: dict[str, Any], db: Session) -> str:
    """Persist one event dict.  Returns "created", "updated", or "skipped"."""
    # Lazy import avoids import-time engine creation from app.db.base
    from app.db.models import Event  # noqa: PLC0415

    # Guard: date is NOT NULL in the schema — skip rather than crash the batch
    if not event.get("date"):
        logger.warning("Skipping event %r — missing date", event.get("name"))
        return "skipped"

    source_url: str | None = event.get("source_url") or event.get("url")

    # ── Try to find an existing row ───────────────────────────────────────────
    existing: Event | None = None
    if source_url:
        existing = (
            db.query(Event)
            .filter(Event.source_url == source_url)
            .first()
        )

    # ── Update path ───────────────────────────────────────────────────────────
    if existing is not None:
        changed = False
        for field in _MUTABLE_FIELDS:
            new_val = event.get(field)
            # Treat empty list as NULL (e.g. price_range=[]) — never store {} in DB
            if isinstance(new_val, list) and len(new_val) == 0:
                new_val = None
            if new_val is not None and getattr(existing, field, None) != new_val:
                setattr(existing, field, new_val)
                changed = True

        if changed:
            db.add(existing)
            logger.debug("Updated event id=%d  name=%r", existing.id, existing.name)
            return "updated"

        logger.debug("Skipped (no change) event id=%d  name=%r", existing.id, existing.name)
        return "skipped"

    # ── Create path ───────────────────────────────────────────────────────────
    payload = {
        k: v for k, v in event.items()
        if k in _ALLOWED_FIELDS
        and v is not None
        and not (isinstance(v, list) and len(v) == 0)  # empty list → NULL
    }
    new_event = Event(**payload)
    db.add(new_event)
    logger.debug("Created event name=%r  source_url=%r", event.get("name"), source_url)
    return "created"
