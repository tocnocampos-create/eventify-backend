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
from datetime import datetime, timedelta, timezone

# Allow importing both app.* and scrapers.* when run as a standalone script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import classifier, deduplicator, enricher
from scrapers.base_scraper import BaseScraper, _get_database_url
from scrapers.cinemark_scraper import CinemarkScraper
from scrapers.comedypass_scraper import ComedyPassScraper
from scrapers.evently_scraper import EventlyScraper
from scrapers.cineplanet_scraper import CineplanetScraper
from scrapers.cinepolis_scraper import CinepolisScraper
from scrapers.passline_scraper import PasslineScraper
from scrapers.portaldisc_scraper import PortalDiscScraper
from scrapers.puntoticket_scraper import PuntoTicketScraper
from scrapers.ticketmaster_scraper import TicketmasterScraper
from scrapers.ticketplus_scraper import TicketPlusScraper
# MuseoScraper is a two-phase scraper:
#   Phase A (--enrich): venue enrichment — run once / monthly (hardcoded metadata + light scraping)
#   Phase B (default):  exposition events — run weekly via the normal scrapers list
from scrapers.museo_scraper import MuseoScraper

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
    scraper: BaseScraper,
    db,
    dry_run: bool = False,
    verbose: bool = False,
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
        _print_sample(processed, verbose=verbose)
        return stats

    # 3. Save (deduplication inside)
    now = datetime.now(timezone.utc)
    for ev in processed:
        ev.setdefault("scraped_at", now)
        ev.setdefault("is_verified", False)
        try:
            # Use a savepoint so a per-event failure only rolls back that
            # event, not all previously successful inserts in the batch.
            sp = db.begin_nested()
            result = deduplicator.save_or_update(ev, db)
            db.flush()
            sp.commit()
            stats[result] += 1
        except Exception as exc:
            logger.warning("Save failed for %r: %s", ev.get("name"), exc)
            sp.rollback()
            stats["failed"] += 1

    db.commit()
    return stats


def _print_sample(events: list[dict], n: int = 5, verbose: bool = False) -> None:
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
        if verbose:
            price = ev.get("price_range")
            desc = ev.get("description", "")
            time_s = ev.get("time_start", "")
            print(f"    price_range : {price}")
            print(f"    time_start  : {time_s!r}")
            print(f"    description : {(desc[:200] + '…') if len(desc) > 200 else desc!r}")


# ── Stale scraper detection ───────────────────────────────────────────────────

# Maps scraper.name → SQL LIKE pattern for source_url in the events table.
# Used to find the last successful save date for a scraper without needing
# a dedicated tracking table.
_SCRAPER_SOURCE_PATTERNS: dict[str, str] = {
    "cinemark":    "cinemark:cl:%",
    "cinepolis":   "cinepolis:cl:%",
    "cineplanet":  "cineplanet:cl:%",
    "puntoticket": "%puntoticket%",
    "comedypass":  "%comedypass%",
    "passline":    "%passline%",
    "ticketplus":  "%ticketplus%",
    "ticketmaster": "%ticketmaster%",
    "evently":     "%evently%",
    "portaldisc":  "%portaldisc%",
    "museo":       "museo:%",
}


def _check_stale_scrapers(
    failed_names: list[str],
    db,
    stale_days: int = 2,
) -> list[str]:
    """Return scraper names that have been failing for more than stale_days.

    For each failed scraper, queries MAX(scraped_at) from events where
    source_url matches the known prefix pattern.  If the last successful
    save is older than stale_days (or no events exist at all), the scraper
    is considered persistently stale.
    """
    from sqlalchemy import text  # noqa: PLC0415

    stale = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    for name in failed_names:
        pattern = _SCRAPER_SOURCE_PATTERNS.get(name)
        if not pattern:
            # Unknown pattern — cannot determine staleness; skip
            logger.warning("[stale-check] No source_url pattern for scraper %r", name)
            continue

        try:
            row = db.execute(
                text("SELECT MAX(scraped_at) FROM events WHERE source_url LIKE :pat"),
                {"pat": pattern},
            ).fetchone()
            last_saved = row[0] if row and row[0] else None
        except Exception as exc:
            logger.warning("[stale-check] DB query failed for %r: %s", name, exc)
            continue

        if last_saved is None:
            # No events at all — treat as stale
            stale.append(name)
            logger.error(
                "[stale-check] Scraper %r has NO events in DB — marking stale", name
            )
        else:
            # last_saved may be timezone-naive (PostgreSQL stores UTC without tz)
            if last_saved.tzinfo is None:
                from datetime import timezone as _tz  # noqa: PLC0415
                last_saved = last_saved.replace(tzinfo=_tz.utc)
            if last_saved < cutoff:
                stale.append(name)
                logger.error(
                    "[stale-check] Scraper %r last saved %s UTC — stale for >%d days",
                    name,
                    last_saved.strftime("%Y-%m-%d %H:%M"),
                    stale_days,
                )
            else:
                logger.info(
                    "[stale-check] Scraper %r last saved %s UTC — within threshold",
                    name, last_saved.strftime("%Y-%m-%d %H:%M"),
                )

    return stale


# ── Post-pipeline steps ───────────────────────────────────────────────────────

def _run_tmdb_enrichment(db) -> None:
    """Enrich all Cine events with TMDB descriptions, posters, and trailer links.

    Called after all cinema scrapers have run so new sessions created in this
    run are enriched in the same pipeline execution.  Skipped gracefully when
    TMDB_READ_ACCESS_TOKEN is not set (e.g. local dev without the token).
    """
    try:
        from scrapers.tmdb_enricher import apply_tmdb_to_cinema_events  # noqa: PLC0415
        stats = apply_tmdb_to_cinema_events(db)
        logger.info(
            "[tmdb] enriched=%d  trailers_added=%d  not_found=%d",
            stats["enriched"], stats["trailers_added"], stats["not_found"],
        )
    except EnvironmentError as exc:
        logger.warning("[tmdb] Skipping enrichment — %s", exc)
    except Exception as exc:
        logger.error("[tmdb] Enrichment failed: %s", exc, exc_info=True)


def _cleanup_expired_events(db, dry_run: bool = False) -> dict[str, int]:
    """Delete events whose dates have passed beyond the retention window.

    Retention policy:
      - Cine events (session-level):  delete if date < today - 14 days
      - All other events:             delete if date < today - 60 days

    Cinema sessions are short-lived (1 showtime per row) so a 14-day window
    is generous.  Live/theatre/music events may span weeks, so 60 days.

    Returns {"cine_deleted": N, "other_deleted": N}.
    """
    from sqlalchemy import text  # noqa: PLC0415

    cutoff_cine  = (datetime.now(timezone.utc) - timedelta(days=14)).date()
    cutoff_other = (datetime.now(timezone.utc) - timedelta(days=60)).date()

    stats = {"cine_deleted": 0, "other_deleted": 0}

    if dry_run:
        # Count-only mode — log what would be deleted without touching the DB
        res_cine = db.execute(
            text("SELECT COUNT(*) FROM events WHERE category = 'Cine' AND date < :cutoff"),
            {"cutoff": str(cutoff_cine)},
        ).scalar()
        res_other = db.execute(
            text("SELECT COUNT(*) FROM events WHERE category != 'Cine' AND date < :cutoff"),
            {"cutoff": str(cutoff_other)},
        ).scalar()
        logger.info(
            "[cleanup] DRY-RUN — would delete %d Cine events (before %s) "
            "and %d other events (before %s)",
            res_cine or 0, cutoff_cine, res_other or 0, cutoff_other,
        )
        return {"cine_deleted": res_cine or 0, "other_deleted": res_other or 0}

    res_cine = db.execute(
        text("DELETE FROM events WHERE category = 'Cine' AND date < :cutoff"),
        {"cutoff": str(cutoff_cine)},
    )
    stats["cine_deleted"] = res_cine.rowcount

    res_other = db.execute(
        text("DELETE FROM events WHERE category != 'Cine' AND date < :cutoff"),
        {"cutoff": str(cutoff_other)},
    )
    stats["other_deleted"] = res_other.rowcount

    db.commit()
    logger.info(
        "[cleanup] Deleted %d expired Cine events (cutoff %s) "
        "and %d other expired events (cutoff %s)",
        stats["cine_deleted"], cutoff_cine,
        stats["other_deleted"], cutoff_other,
    )
    return stats


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
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="Stop after this many events per scraper (0 = unlimited)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print price and description for each sample event",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Run museum Phase A venue enrichment before scraping (monthly)",
    )
    args = parser.parse_args()

    run_start = datetime.now(timezone.utc)
    logger.info("━" * 50)
    logger.info("SCRAPER RUN STARTED%s — %s UTC",
                " (DRY RUN)" if args.dry_run else "",
                run_start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("━" * 50)

    engine = create_engine(_get_database_url(), pool_pre_ping=True)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # ── Museum Phase A: venue enrichment (monthly, --enrich flag) ─────────────
    if args.enrich:
        logger.info("━━━ Running museum venue enrichment (Phase A) ━━━")
        museo = MuseoScraper()
        museo.run_venue_enrichment()  # uses DATABASE_URL env var, opens own psycopg2 connection

    totals = {"found": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}
    scraper_ok: list[str] = []    # names of scrapers that completed without crash
    scraper_err: list[str] = []   # names of scrapers that raised an unhandled exception

    scrapers = [
        PuntoTicketScraper(max_pages=args.max_pages, max_events=args.max_events),
        ComedyPassScraper(max_events=args.max_events),
        CinemarkScraper(max_events=args.max_events),
        CineplanetScraper(max_events=args.max_events),
        CinepolisScraper(max_events=args.max_events),
        PasslineScraper(max_events=args.max_events),
        TicketPlusScraper(max_pages=args.max_pages, max_events=args.max_events),
        TicketmasterScraper(max_pages=args.max_pages, max_events=args.max_events),
        EventlyScraper(max_pages=args.max_pages, max_events=args.max_events),
        PortalDiscScraper(max_pages=args.max_pages, max_events=args.max_events),
        # Phase B: exposition events (weekly). Phase A runs separately with --enrich.
        MuseoScraper(max_events=args.max_events),
    ]

    with Session() as db:
        for scraper in scrapers:
            scraper_start = datetime.now(timezone.utc)
            logger.info("━━━ Running scraper: %s ━━━", scraper.name)
            try:
                stats = _run_pipeline(scraper, db, dry_run=args.dry_run, verbose=args.verbose)
                for key in totals:
                    totals[key] += stats.get(key, 0)
                elapsed = (datetime.now(timezone.utc) - scraper_start).seconds
                logger.info(
                    "[%s] done in %ds — found=%d created=%d updated=%d failed=%d",
                    scraper.name, elapsed,
                    stats.get("found", 0), stats.get("created", 0),
                    stats.get("updated", 0), stats.get("failed", 0),
                )
                scraper_ok.append(scraper.name)
            except Exception as exc:
                logger.error("Scraper %s crashed: %s", scraper.name, exc, exc_info=True)
                scraper_err.append(scraper.name)

    # ── Post-pipeline: TMDB enrichment ───────────────────────────────────────
    if not args.dry_run:
        logger.info("━━━ Running TMDB enrichment ━━━")
        with Session() as db:
            _run_tmdb_enrichment(db)

    # ── Post-pipeline: expired event cleanup ──────────────────────────────────
    logger.info("━━━ Running expired event cleanup ━━━")
    with Session() as db:
        cleanup = _cleanup_expired_events(db, dry_run=args.dry_run)

    engine.dispose()

    # ── Final summary ─────────────────────────────────────────────────────────
    run_end = datetime.now(timezone.utc)
    duration = run_end - run_start
    duration_str = f"{duration.seconds // 60}m {duration.seconds % 60}s"

    print("\n" + "━" * 50)
    print("SCRAPER RUN COMPLETE" + (" (DRY RUN)" if args.dry_run else ""))
    print("━" * 50)
    print(f"  Started : {run_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Finished: {run_end.strftime('%Y-%m-%d %H:%M:%S')} UTC  ({duration_str})")
    print(f"  Scrapers: {len(scraper_ok)} ok, {len(scraper_err)} crashed"
          + (f"  [{', '.join(scraper_err)}]" if scraper_err else ""))
    print(f"  Found:   {totals['found']}")
    print(f"  Created: {totals['created']}")
    print(f"  Updated: {totals['updated']}")
    print(f"  Skipped: {totals['skipped']}")
    print(f"  Failed:  {totals['failed']}")
    print(f"  Cleaned: {cleanup['cine_deleted']} Cine + {cleanup['other_deleted']} other expired")
    print("━" * 50)

    # ── Stale scraper alert ────────────────────────────────────────────────────
    # Exit 1 when:
    #   a) ALL scrapers crashed (existing behaviour), OR
    #   b) ANY scraper has been failing for > 2 consecutive days (new behaviour).
    # Railway marks exit-1 cron runs as failed and sends a notification,
    # alerting the team before days of stale data accumulate unnoticed.
    if scraper_err and not scraper_ok:
        logger.error("All %d scrapers failed — exiting with code 1", len(scraper_err))
        sys.exit(1)

    if scraper_err:
        logger.info("━━━ Checking for persistently stale scrapers ━━━")
        with Session() as db:
            stale = _check_stale_scrapers(scraper_err, db, stale_days=2)
        if stale:
            logger.error(
                "ALERT: %d scraper(s) have been failing for >2 days: %s — exiting with code 1",
                len(stale),
                ", ".join(stale),
            )
            sys.exit(1)
        else:
            logger.info(
                "Partial failures detected but all failed scrapers saved data recently "
                "(within 2 days) — treating run as success."
            )


if __name__ == "__main__":
    main()
