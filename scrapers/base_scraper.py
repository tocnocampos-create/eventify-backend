"""Abstract base class for all Eventify scrapers.

Every scraper inherits from BaseScraper and implements fetch_events().
save_events() handles persistence via deduplicator — it does NOT call
the classifier or enricher; those are the run_all.py pipeline's job.

Usage pattern (from run_all.py):
    scraper = MyScraper()
    raw = scraper.fetch_events()
    enriched = [enricher.enrich(classifier.classify(ev), db) for ev in raw]
    stats = scraper.save_events(enriched, db)
"""
import logging
import os
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

# Allow importing app.* modules when this file is run as part of a standalone script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Scrapers use their own engine so they are not coupled to the FastAPI
# app.db.base engine (which defaults to DB_HOST=db, a Docker service name).
# The app.db.models ORM classes are just table definitions — they work
# with any engine / session.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

logger = logging.getLogger(__name__)

_DEFAULT_DB_URL = "postgresql://eventify:eventify@localhost:5432/eventify"


def _get_database_url() -> str:
    return os.getenv("DATABASE_URL", _DEFAULT_DB_URL)


def make_scraper_session() -> tuple[Any, Session]:
    """Return (engine, session) bound to DATABASE_URL."""
    engine = create_engine(_get_database_url(), pool_pre_ping=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal()


class BaseScraper(ABC):
    """Abstract base class every scraper inherits from."""

    #: Override in subclasses — used in log messages.
    name: str = "base"

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"scraper.{self.name}")

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def fetch_events(self) -> list[dict[str, Any]]:
        """Fetch raw events from the source.

        Returns a list of dicts whose keys map to Event model fields.
        Mandatory keys: name, date.
        Recommended keys: source_url, url, venue_name, time_start,
                          price_range, image_url, description.
        """

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_events(
        self,
        events: list[dict[str, Any]],
        db: Optional[Session] = None,
    ) -> dict[str, int]:
        """Persist events to the DB using deduplicator logic.

        Args:
            events: List of event dicts, already passed through
                    classifier.classify() and enricher.enrich().
            db:     Optional existing Session.  If None, a new session
                    is created and committed/closed here.

        Returns:
            Stats dict with keys: found, created, updated, skipped, failed.
        """
        # Lazy import to avoid circular dependency at module level
        from scrapers.deduplicator import save_or_update  # noqa: PLC0415

        _own_session = db is None
        engine = None
        if _own_session:
            engine, db = make_scraper_session()

        stats: dict[str, int] = {
            "found": len(events),
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
        }

        try:
            for event in events:
                # Stamp every record with scraper metadata
                event.setdefault("scraped_at", datetime.now(timezone.utc))
                event.setdefault("is_verified", False)

                try:
                    result = save_or_update(event, db)
                    stats[result] += 1
                except Exception as exc:
                    self.logger.warning(
                        "Failed to save event %r: %s", event.get("name"), exc
                    )
                    db.rollback()
                    stats["failed"] += 1

            if _own_session:
                db.commit()

        finally:
            if _own_session:
                db.close()
                if engine:
                    engine.dispose()

        self.logger.info(
            "[%s] found=%d  created=%d  updated=%d  skipped=%d  failed=%d",
            self.name,
            stats["found"],
            stats["created"],
            stats["updated"],
            stats["skipped"],
            stats["failed"],
        )
        return stats
