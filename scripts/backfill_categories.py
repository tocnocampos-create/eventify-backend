"""backfill_categories.py — reclassify ALL events using the v4 classifier rules.

Usage:
    cd eventify-backend-feat-integrate-e2e
    source venv/bin/activate
    python scripts/backfill_categories.py [--dry-run]

What it does:
  1. Fetches all events with their venue info from the production DB.
  2. Clears category and type, then reclassifies each event using the
     updated classifier (v4: FIX 1–7).
  3. Updates category, type, and keywords in the DB.
  4. Prints before→after count per category.
  5. Shows 3 sample events per category after backfill.
  6. Flags any category with fewer than 5 events ⚠️.
  7. Confirms zero garbage/unknown categories remain.
"""
from __future__ import annotations

import sys
import os
import json
from collections import Counter, defaultdict

import psycopg2
import psycopg2.extras

# Make scrapers importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scrapers.classifier import classify, _normalize_category, CANONICAL_CATEGORIES

DRY_RUN = "--dry-run" in sys.argv

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz@shuttle.proxy.rlwy.net:17408/railway",
)


def main() -> None:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("Fetching events…")
    cur.execute("""
        SELECT
            e.id,
            e.name,
            e.description,
            e.category   AS old_category,
            e.type        AS old_type,
            e.keywords    AS old_keywords,
            e.kids_friendly,
            e.time_start,
            e.source_url,
            v.venue_type,
            v.name        AS venue_name
        FROM events e
        LEFT JOIN venues v ON e.venue_id = v.id
        ORDER BY e.id
    """)
    rows = cur.fetchall()
    total = len(rows)
    print(f"  {total} events loaded.\n")

    before_counts: Counter[str] = Counter()
    after_counts:  Counter[str] = Counter()
    samples: defaultdict[str, list[dict]] = defaultdict(list)

    updates: list[tuple] = []

    for row in rows:
        old_cat = row["old_category"] or "(none)"
        before_counts[old_cat] += 1

        # Normalize old category (maps garbage → canonical or None).
        # Used as a last-resort fallback if the classifier yields nothing.
        normalized_old = _normalize_category(row["old_category"])

        event: dict = {
            "name":         row["name"] or "",
            "description":  row["description"] or "",
            "venue_type":   row["venue_type"] or "",
            "venue_name":   row["venue_name"] or "",
            "source_url":   row["source_url"] or "",
            "time_start":   row["time_start"],
            "kids_friendly": row["kids_friendly"],
            # Clear category/type so classifier runs from scratch
            "category": None,
            "type":     None,
        }

        classify(event)

        # If the classifier found nothing (no keyword matches, no venue fallback),
        # fall back to the normalized old category rather than leaving it blank.
        if event.get("category") is None and normalized_old:
            event["category"] = normalized_old

        new_cat  = event.get("category") or "(none)"
        new_type = event.get("type")
        new_kws  = event.get("keywords", [])

        after_counts[new_cat] += 1

        if len(samples[new_cat]) < 3:
            samples[new_cat].append({
                "id":    row["id"],
                "name":  (row["name"] or "")[:60],
                "venue": (row["venue_name"] or "")[:35],
                "vtype": row["venue_type"],
                "type":  new_type,
            })

        updates.append((new_cat, new_type, new_kws, row["id"]))

    # ── Apply updates ──────────────────────────────────────────────────────────
    if not DRY_RUN:
        print("Writing updates to DB…")
        psycopg2.extras.execute_batch(
            cur,
            """
            UPDATE events
               SET category = %s,
                   type     = %s,
                   keywords = %s
             WHERE id = %s
            """,
            [(cat, typ, kws, eid) for cat, typ, kws, eid in updates],
            page_size=500,
        )
        conn.commit()
        print("  Done.\n")
    else:
        print("DRY RUN — no DB changes written.\n")

    # ── Before / After report ──────────────────────────────────────────────────
    all_cats = sorted(set(list(before_counts.keys()) + list(after_counts.keys())))

    print("=" * 72)
    print(f"{'CATEGORY':<30}  {'BEFORE':>7}  {'AFTER':>7}  {'DELTA':>7}")
    print("-" * 72)
    for cat in all_cats:
        b = before_counts.get(cat, 0)
        a = after_counts.get(cat, 0)
        delta = a - b
        flag = "  ⚠️ " if a < 5 and a > 0 else ""
        print(f"  {cat:<28}  {b:>7}  {a:>7}  {delta:>+7}{flag}")
    print("-" * 72)
    print(f"  {'TOTAL':<28}  {total:>7}  {sum(after_counts.values()):>7}")
    print("=" * 72)

    # ── Samples ────────────────────────────────────────────────────────────────
    print("\n── 3 sample events per category (after) ──────────────────────────")
    for cat in sorted(after_counts.keys()):
        if cat == "(none)":
            continue
        print(f"\n  [{cat}]")
        for s in samples[cat]:
            print(f"    id={s['id']:<6} {s['name']:<62}")
            print(f"           venue={s['venue']:<37} vtype={s['vtype']!r}  type={s['type']!r}")

    # ── Validation ─────────────────────────────────────────────────────────────
    print("\n── Validation ─────────────────────────────────────────────────────")

    garbage = {c for c in after_counts if c not in CANONICAL_CATEGORIES and c != "(none)"}
    if garbage:
        print(f"  ❌ Garbage categories still present: {garbage}")
    else:
        print("  ✓ Zero garbage/unknown categories remain")

    unclassified = after_counts.get("(none)", 0)
    if unclassified:
        print(f"  ⚠️  {unclassified} events have no category")
    else:
        print("  ✓ All events have a category")

    low = [c for c in after_counts if 0 < after_counts[c] < 5 and c != "(none)"]
    if low:
        for c in low:
            print(f"  ⚠️  '{c}' has only {after_counts[c]} event(s)")
    else:
        print("  ✓ All categories have ≥5 events (or zero)")

    # Spot checks
    cine_after = after_counts.get("Cine", 0)
    print(f"\n  Cine events: {cine_after}  (was {before_counts.get('Cine', 0)})")
    nacional = after_counts.get("Nacional", 0)
    print(f"  Nacional events: {nacional}")
    electr = sum(
        1 for cat, typ, *_ in updates
        if typ == "Electrónica" and cat == "Música"
    )
    print(f"  Electrónica events in Música: {electr}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
