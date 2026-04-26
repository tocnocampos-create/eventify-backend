"""Re-classify all events in the database using the current classifier.

Merge strategy (conservative — avoids regressions):
  1. Fill in blank categories: None → anything is always accepted.
  2. Fix known stale misclassifications:
       Teatro (type=Comedia/Stand-Up) → Comedia
       Teatro → Familia (children's-show keywords)
       Teatro → Música (concert keywords; venue lock)
       Arte / Cine false positives → corrected category
       "Nacional" → corrected category
  3. Never downgrade to None (keeps existing non-null classification when
     the new classifier finds no signal — e.g., artist-name-only events
     whose original URL-hint lock is no longer in the DB).
  4. Never change Música → Vida Nocturna (preserves concert events that
     have no genre keywords but were correctly labelled by the scraper).

Usage:
    python scripts/reclassify_all.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.classifier import classify
from scrapers.base_scraper import _get_database_url
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reclassify_all")


def _should_update(old_cat: str | None, new_cat: str | None, old_type: str | None) -> bool:
    """Decide whether to write the new classification to the DB."""

    # 1. Always fill in missing categories
    if old_cat is None and new_cat is not None:
        return True

    # 2. No change needed
    if old_cat == new_cat:
        return False

    # 3. Never downgrade to None (keeps valid classifications when
    #    keyword signals are absent — e.g., artist-name-only events)
    if new_cat is None:
        return False

    # 4. Never demote Música to Vida Nocturna, Arte, or Cine — that would turn
    #    concerts at Club/Bar venues (no genre keywords) into nightlife, concerts
    #    at arts venues (e.g. "Centro Arte Alameda") into art events, or dance
    #    parties at venues with "Cine" in their name into cinema events.
    if old_cat == "Música" and new_cat in ("Vida Nocturna", "Arte", "Cine"):
        return False

    # 4b. Never downgrade Comedia, Familia, or Cine to Vida Nocturna via
    #     venue-type fallback. Bar/Club venue type should not override an
    #     explicit comedy, family, or cinema classification.
    if old_cat in ("Comedia", "Familia", "Cine") and new_cat == "Vida Nocturna":
        return False

    # 5. Fix stale Teatro misclassifications:
    #    a) Events with comedy type stuck in Teatro
    if old_cat == "Teatro" and old_type in ("Comedia", "Stand Up", "Stand-Up", "Stand-up", "Humor"):
        return True
    #    b) Classifier now produces a better category for a Teatro event
    if old_cat == "Teatro" and new_cat in ("Comedia", "Familia", "Música"):
        return True

    # 6. Fix Arte / Cine false positives (where classifier gives a different signal)
    if old_cat in ("Arte", "Cine") and new_cat is not None:
        return True

    # 7. Fix the stray "Nacional" category
    if old_cat == "Nacional":
        return True

    # 8. General: accept any other non-None improvement
    #    (e.g. Vida Nocturna → Música when venue lock fires)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-classify all DB events")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute new classifications but do not write to DB")
    args = parser.parse_args()

    engine = create_engine(_get_database_url(), pool_pre_ping=True)

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                e.id,
                e.name,
                e.description,
                e.category  AS old_category,
                e.type      AS old_type,
                v.name      AS venue_name,
                v.venue_type
            FROM events e
            LEFT JOIN venues v ON e.venue_id = v.id
            ORDER BY e.id
        """)).fetchall()

    logger.info("Loaded %d events", len(rows))

    before: dict[str, int] = {}
    after: dict[str, int] = {}

    updates: list[dict] = []
    skipped_downgrade = 0

    for row in rows:
        ev = {
            "name":        row.name or "",
            "description": row.description or "",
            "venue_type":  row.venue_type or "",
            "venue_name":  row.venue_name or "",
        }

        old_cat  = row.old_category
        old_type = row.old_type

        before[old_cat or "None"] = before.get(old_cat or "None", 0) + 1

        result = classify(ev)
        new_cat  = result.get("category")
        new_type = result.get("type")
        new_kw   = result.get("keywords") or []

        if _should_update(old_cat, new_cat, old_type):
            final_cat = new_cat
        else:
            final_cat = old_cat
            if old_cat is not None and new_cat is None:
                skipped_downgrade += 1

        after[final_cat or "None"] = after.get(final_cat or "None", 0) + 1

        if final_cat != old_cat or (final_cat == new_cat and new_type != old_type):
            updates.append({
                "id":       row.id,
                "category": final_cat,
                "type":     new_type if final_cat == new_cat else old_type,
                "keywords": new_kw   if final_cat == new_cat else (row[7] if len(row) > 7 else []),
                "old_cat":  old_cat,
                "new_cat":  final_cat,
            })

    logger.info("%d events will be updated", len(updates))
    logger.info("%d events kept existing category (new classifier returned None)", skipped_downgrade)

    if not args.dry_run and updates:
        with engine.connect() as conn:
            for u in updates:
                # keywords is a PostgreSQL text[] column — pass as Python list
                kw_value = u["keywords"] if isinstance(u["keywords"], list) else []
                conn.execute(text("""
                    UPDATE events
                    SET category = :category,
                        type     = :type,
                        keywords = :keywords
                    WHERE id = :id
                """), {
                    "id":       u["id"],
                    "category": u["category"],
                    "type":     u["type"],
                    "keywords": kw_value,
                })
            conn.commit()
        logger.info("Database updated — %d rows written", len(updates))
    elif args.dry_run:
        logger.info("DRY RUN — no writes performed")
        logger.info("Sample of actual changes (first 25):")
        shown = 0
        for u in updates:
            if u["old_cat"] != u["new_cat"] and shown < 25:
                logger.info("  [%s] %r → %r", u["id"], u["old_cat"], u["new_cat"])
                shown += 1

    # ── Before / after distribution ───────────────────────────────────────────
    all_cats = sorted(set(list(before.keys()) + list(after.keys())))
    print("\n" + "━" * 65)
    print(f"{'Category':<25}  {'Before':>8}  {'After':>8}  {'Δ':>8}")
    print("━" * 65)
    for cat in all_cats:
        b = before.get(cat, 0)
        a = after.get(cat, 0)
        delta = a - b
        sign = "+" if delta > 0 else ""
        print(f"{cat:<25}  {b:>8}  {a:>8}  {sign}{delta:>7}")
    print("━" * 65)
    print(f"{'TOTAL':<25}  {sum(before.values()):>8}  {sum(after.values()):>8}")
    print("━" * 65)


if __name__ == "__main__":
    main()
