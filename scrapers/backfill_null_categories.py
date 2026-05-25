"""Backfill NULL (and garbage) categories on upcoming events.

Runs classify() on every upcoming event that has a NULL or non-canonical
category, then UPDATEs the DB rows in bulk.

Usage:
    venv/bin/python3 scrapers/backfill_null_categories.py [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main(dry_run: bool = False) -> None:
    from scrapers.classifier import CANONICAL_CATEGORIES, classify

    db_url = os.environ.get("DATABASE_URL") or (
        "postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz"
        "@shuttle.proxy.rlwy.net:17408/railway"
    )
    engine = create_engine(db_url, pool_pre_ping=True)
    today = date.today().isoformat()

    with Session(engine) as db:
        rows = db.execute(text("""
            SELECT e.id, e.name, e.description,
                   v.venue_type, v.name AS venue_name,
                   e.category, e.type, e.source_url, e.time_start
            FROM   events e
            LEFT   JOIN venues v ON v.id = e.venue_id
            WHERE  e.date >= :today
              AND  (e.category IS NULL OR e.category NOT IN :canonical)
            ORDER  BY e.id
        """), {"today": today, "canonical": tuple(CANONICAL_CATEGORIES)}).fetchall()

    logger.info("Found %d events with NULL/garbage category to backfill", len(rows))

    updates: list[dict] = []
    skipped = 0

    for row in rows:
        ev: dict = {
            "id":          row.id,
            "name":        row.name or "",
            "description": row.description or "",
            "venue_type":  row.venue_type or "",
            "venue_name":  row.venue_name or "",
            "category":    row.category,
            "type":        row.type,
            "source_url":  row.source_url or "",
            "time_start":  row.time_start,
        }
        result = classify(ev)
        new_cat = result.get("category")
        new_type = result.get("type")

        if new_cat and new_cat != row.category:
            updates.append({
                "eid":     row.id,
                "cat":     new_cat,
                "etype":   new_type,
                "kws":     result.get("keywords") or [],
                "kids":    result.get("kids_friendly", False),
            })
        else:
            skipped += 1
            logger.debug("id=%-7d  no change  (%r)", row.id, row.category)

    logger.info(
        "Will update %d events, skip %d (already correct or still unresolvable)",
        len(updates), skipped,
    )

    if dry_run:
        for u in updates[:20]:
            logger.info("DRY  id=%-7d  → %s / %s", u["eid"], u["cat"], u["etype"])
        if len(updates) > 20:
            logger.info("  … and %d more", len(updates) - 20)
        logger.info("Dry run — no changes written")
        return

    with Session(engine) as db:
        for u in updates:
            db.execute(text("""
                UPDATE events
                SET    category     = :cat,
                       type         = COALESCE(:etype, type),
                       keywords     = :kws,
                       kids_friendly = :kids
                WHERE  id = :eid
            """), {
                "cat":  u["cat"],
                "etype": u["etype"],
                "kws":  u["kws"],
                "kids": u["kids"],
                "eid":  u["eid"],
            })
        db.commit()

    logger.info("Done — updated %d events", len(updates))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
