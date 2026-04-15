"""
Fix empty keywords and category for scraped events.

Steps:
  1. Re-classify events with empty keywords using name + description + venue_type.
     Updates both keywords AND category (when category is still empty).
  2. Any event still missing a category after classification gets category = "Música"
     (PuntoTicket default — the platform is primarily music/entertainment).

Usage:
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/eventify \
        python scripts/fix_empty_classifications.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from scrapers.classifier import build_keywords, _match_rules, _RULE_PRIORITY, _RULE_TO_CATEGORY, _RULE_TO_TYPE

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/eventify")


def classify_row(row) -> dict:
    """Return dict of fields to update for one event row."""
    name        = row["name"] or ""
    description = row["description"] or ""
    venue_type  = row["venue_type"] or ""

    matched = _match_rules(name, description, venue_type)

    # keywords — always rebuild from matched rules
    from scrapers.classifier import KEYWORD_RULES
    kws: set[str] = set()
    for rule in matched:
        kws.update(KEYWORD_RULES[rule])
    keywords = sorted(kws)

    # category / type — only fill if currently empty
    category = row["category"] or None
    etype    = row["type"] or None
    for rule in _RULE_PRIORITY:
        if rule in matched:
            if category is None:
                category = _RULE_TO_CATEGORY.get(rule)
            if etype is None:
                etype = _RULE_TO_TYPE.get(rule)
            if category is not None and etype is not None:
                break

    return {"keywords": keywords, "category": category, "type": etype}


def main():
    engine = create_engine(DB_URL)

    with Session(engine) as session:
        # ── Step 1: classify events with empty keywords ───────────────────────
        rows = session.execute(text("""
            SELECT
                e.id, e.name, e.description, e.type, e.category, e.keywords,
                v.venue_type
            FROM events e
            LEFT JOIN venues v ON v.id = e.venue_id
            WHERE e.keywords IS NULL OR e.keywords = '{}'
        """)).mappings().all()

        print(f"Events with empty keywords: {len(rows)}")

        kw_updated = 0
        cat_updated_via_rules = 0

        for row in rows:
            result = classify_row(row)
            old_kws  = list(row["keywords"] or [])
            old_cat  = row["category"]

            kws_changed = result["keywords"] != old_kws
            cat_changed = result["category"] and result["category"] != old_cat

            if not kws_changed and not cat_changed:
                continue

            session.execute(
                text("""
                    UPDATE events
                    SET keywords = :kws,
                        category = COALESCE(NULLIF(category, ''), :cat),
                        type     = COALESCE(NULLIF(type, ''), :etype)
                    WHERE id = :id
                """),
                {
                    "kws":   result["keywords"],
                    "cat":   result["category"],
                    "etype": result["type"],
                    "id":    row["id"],
                },
            )
            if kws_changed:
                kw_updated += 1
            if cat_changed:
                cat_updated_via_rules += 1

        session.commit()
        print(f"  → keywords updated via rules : {kw_updated}")
        print(f"  → category set via rules     : {cat_updated_via_rules}")

        # ── Step 1b: fix PuntoTicket events misclassified as Arte ────────────
        # PuntoTicket is a live-entertainment ticketing platform — it does not
        # sell tickets to museum exhibitions or gallery openings.  Events whose
        # classifier matched generic words ("historia", "fotografías", "arte" in
        # a tour name) and ended up with category=Arte / type=Exposición are
        # concerts that must be reclassified as Música.
        # Exception: venues with type Museo or Galería may host genuine art events.
        ART_VENUE_TYPES = ("Museo", "Galería")
        rows_arte = session.execute(text("""
            SELECT e.id, e.name, e.category, e.type, v.venue_type
            FROM events e
            LEFT JOIN venues v ON v.id = e.venue_id
            WHERE e.source_url LIKE '%puntoticket%'
              AND e.category = 'Arte'
        """)).mappings().all()

        print(f"\nPuntoTicket events misclassified as Arte: {len(rows_arte)}")
        arte_fixed = 0
        for row in rows_arte:
            if row["venue_type"] in ART_VENUE_TYPES:
                continue  # genuine art event at an art venue — leave as-is
            session.execute(
                text("UPDATE events SET category = 'Música', type = NULL WHERE id = :id"),
                {"id": row["id"]},
            )
            print(f"  Fixed [{row['id']}] {row['name'][:50]!r:53s}"
                  f"  venue_type={row['venue_type']!r}  {row['category']}/{row['type']} → Música")
            arte_fixed += 1

        session.commit()
        print(f"  → Arte → Música fixes: {arte_fixed}")

        # ── Step 2: default category = "Música" for still-uncategorized ───────
        result = session.execute(text("""
            UPDATE events
            SET category = 'Música'
            WHERE category IS NULL OR category = ''
        """))
        default_cat_updated = result.rowcount
        session.commit()
        print(f"  → category defaulted to Música: {default_cat_updated}")

        # ── Summary ───────────────────────────────────────────────────────────
        totals = session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE keywords IS NULL OR keywords = '{}') as still_empty_kw,
                COUNT(*) FILTER (WHERE category IS NULL OR category = '')   as still_no_cat,
                COUNT(*) as total
            FROM events
        """)).mappings().one()

        print(f"\n── Final state ──────────────────────────────────────────────")
        print(f"  Total events            : {totals['total']}")
        print(f"  Still empty keywords    : {totals['still_empty_kw']}")
        print(f"  Still missing category  : {totals['still_no_cat']}")

        # ── 5 sample events that were just fixed ──────────────────────────────
        samples = session.execute(text("""
            SELECT id, name, date, category, keywords
            FROM events
            WHERE source_url LIKE '%puntoticket%'
              AND (keywords IS NULL OR keywords = '{}' OR cardinality(keywords) > 0)
            ORDER BY id DESC
            LIMIT 10
        """)).mappings().all()

        print(f"\n── 10 recent PuntoTicket events (after fix) ─────────────────")
        for r in samples:
            kws_preview = list(r["keywords"] or [])[:5]
            print(f"  [{r['id']}] {r['name'][:45]:<45} | {r['date']} | {r['category']} | kw: {kws_preview}")


if __name__ == "__main__":
    main()
