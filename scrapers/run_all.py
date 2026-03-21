"""Main scraper runner.

Orchestrates the full pipeline:
    fetch_events()  →  classify()  →  enrich()  →  save_events()

Run manually:
    # From the project root, with the venv activated:
    python scrapers/run_all.py

    # Dry-run (fetch + parse, no DB writes):
    python scrapers/run_all.py --dry-run

Later this file will be called by a cron job.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

# Allow importing both app.* and scrapers.* when run as a standalone script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import classifier, deduplicator, enricher
from scrapers.puntoticket_scraper import PuntoTicketScraper
from scrapers.base_scraper import _get_database_url

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_all")


# ── Pipeline helpers ─────────────────────────────────────────────────────────

def _run_pipeline(
    scraper: PuntoTicketScraper,
    db,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run fetch → classify → enrich → (optionally) save for one scraper."""
    stats = {"found": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}

    # 1. Fetch
    raw_events = scraper.fetch_events()
    stats["found"] = len(raw_events)
    logger.info("[%s] fetched %d events", scraper.name, stats["found"])

    if not raw_events:
        return stats

    # 2. Classify + Enrich
    processed: list[dict] = []
    for ev in raw_events:
        try:
            # Classify first (sets category, type, keywords, kids_friendly)
            ev = classifier.classify(ev)
            # Enrich (resolves venue_name → venue_id; may add venue_type
            # which lets classify() pick up VENUE_TYPE_RULES if called again)
            ev = enricher.enrich(ev, db)
            # Re-classify now that venue_type might be known
            if ev.get("venue_type") and not ev.get("category"):
                ev = classifier.classify(ev)
            processed.append(ev)
        except Exception as exc:
            logger.warning("Pipeline error for %r: %s", ev.get("name"), exc)
            stats["failed"] += 1

    logger.info("[%s] classified+enriched %d events", scraper.name, len(processed))

    if dry_run:
        logger.info("[%s] DRY-RUN — skipping DB writes", scraper.name)
        stats["skipped"] = len(processed)
        _print_sample(processed)
        return stats

    # 3. Save (deduplication inside)
    now = datetime.now(timezone.utc)
    for ev in processed:
        ev.setdefault("scraped_at", now)
        ev.setdefault("is_verified", False)
        try:
            result = deduplicator.save_or_update(ev, db)
            # Flush immediately so any constraint violation is caught
            # per-event rather than rolling back the whole batch at commit.
            db.flush()
            stats[result] += 1
        except Exception as exc:
            logger.warning("Save failed for %r: %s", ev.get("name"), exc)
            db.rollback()
            stats["failed"] += 1

    db.commit()
    return stats


def _print_sample(events: list[dict], n: int = 5) -> None:
    print(f"\n── Sample events (first {min(n, len(events))}) ─────────────────────────────────")
    for ev in events[:n]:
        print(
            f"  • {ev.get('name', '?')!r:<40s}  "
            f"date={ev.get('date', '?')}  "
            f"venue={ev.get('venue_name', '?')!r}  "
            f"cat={ev.get('category', '?')}  "
            f"type={ev.get('type', '?')}  "
            f"venue_id={ev.get('venue_id')}"
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Eventify scraper runner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse events but do NOT write to the database",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum listing pages per scraper (default: 10)",
    )
    args = parser.parse_args()

    engine = create_engine(_get_database_url(), pool_pre_ping=True)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    totals = {"found": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}

    scrapers = [
        PuntoTicketScraper(max_pages=args.max_pages),
        # Add more scrapers here as they are implemented:
        # FoobarScraper(),
    ]

    with Session() as db:
        for scraper in scrapers:
            logger.info("━━━ Running scraper: %s ━━━", scraper.name)
            try:
                stats = _run_pipeline(scraper, db, dry_run=args.dry_run)
                for key in totals:
                    totals[key] += stats.get(key, 0)
            except Exception as exc:
                logger.error("Scraper %s crashed: %s", scraper.name, exc, exc_info=True)

    engine.dispose()

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n" + "━" * 50)
    print("SCRAPER RUN COMPLETE" + (" (DRY RUN)" if args.dry_run else ""))
    print("━" * 50)
    print(f"  Found:   {totals['found']}")
    print(f"  Created: {totals['created']}")
    print(f"  Updated: {totals['updated']}")
    print(f"  Skipped: {totals['skipped']}")
    print(f"  Failed:  {totals['failed']}")
    print("━" * 50)


if __name__ == "__main__":
    main()
