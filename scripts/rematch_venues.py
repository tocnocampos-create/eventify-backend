"""Re-match venues for existing events that have venue_id = NULL.

Because venue_name is not persisted to the events table, this script
re-fetches the PuntoTicket listing pages (listing only — no detail pages)
to rebuild a source_url → venue_name mapping, then applies the improved
enricher logic to update venue_id for any event that can now be matched.

Usage:
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/eventify \
        python scripts/rematch_venues.py
"""
from __future__ import annotations

import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from scrapers.puntoticket_scraper import PuntoTicketScraper
from scrapers import enricher

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/eventify"
)


def main() -> None:
    engine = create_engine(DB_URL)

    # ── Step 1: fetch listing pages to rebuild source_url → venue_name map ────
    print("Fetching PuntoTicket listing pages (no detail pages)…")
    scraper = PuntoTicketScraper(max_pages=5)
    raw_events = scraper.fetch_events()
    print(f"  Scraped {len(raw_events)} events from listings")

    # Build map: source_url (normalised) → venue_name
    url_to_venue: dict[str, str] = {}
    for ev in raw_events:
        url = (ev.get("source_url") or ev.get("url") or "").strip()
        vname = (ev.get("venue_name") or "").strip()
        if url and vname:
            url_to_venue[url] = vname

    print(f"  URLs with venue_name: {len(url_to_venue)}")

    # ── Step 2: find DB events without venue_id that have a matching URL ──────
    with Session(engine) as db:
        rows = db.execute(text("""
            SELECT id, name, source_url, url
            FROM events
            WHERE venue_id IS NULL
              AND (source_url IS NOT NULL OR url IS NOT NULL)
        """)).mappings().all()

        print(f"\nDB events with no venue_id: {len(rows)}")

        matched = 0
        unmatched_names: list[str] = []

        for row in rows:
            # Look up venue_name by source_url, then by url as fallback
            source = (row["source_url"] or "").strip()
            url    = (row["url"] or "").strip()
            vname  = url_to_venue.get(source) or url_to_venue.get(url)

            if not vname:
                unmatched_names.append(row["name"])
                continue

            # Apply improved enricher logic
            ev_dict: dict = {"venue_name": vname}
            enricher.enrich(ev_dict, db)

            venue_id = ev_dict.get("venue_id")
            if venue_id:
                db.execute(
                    text("UPDATE events SET venue_id = :vid WHERE id = :id"),
                    {"vid": venue_id, "id": row["id"]},
                )
                print(
                    f"  Matched [{row['id']:4d}] {row['name'][:45]!r:47s}"
                    f"  venue_name={vname!r:40s} → venue_id={venue_id}"
                )
                matched += 1
            else:
                unmatched_names.append(f"{row['name']} (venue_name={vname!r})")

        db.commit()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n── Results ──────────────────────────────────────────────────────")
    print(f"  Matched and updated : {matched}")
    print(f"  Still unmatched     : {len(unmatched_names)}")
    if unmatched_names:
        print("\n  Still unmatched (venue not in DB or no venue_name scraped):")
        for n in sorted(unmatched_names)[:20]:
            print(f"    • {n}")
        if len(unmatched_names) > 20:
            print(f"    … and {len(unmatched_names) - 20} more")

    # ── Final DB state ─────────────────────────────────────────────────────────
    with Session(engine) as db:
        totals = db.execute(text("""
            SELECT
                COUNT(*) as total,
                COUNT(venue_id) as with_venue,
                COUNT(*) - COUNT(venue_id) as without_venue
            FROM events
        """)).mappings().one()
        print(f"\n── Final DB state ───────────────────────────────────────────────")
        print(f"  Total events      : {totals['total']}")
        print(f"  With venue_id     : {totals['with_venue']}")
        print(f"  Without venue_id  : {totals['without_venue']}")


if __name__ == "__main__":
    main()
