"""Daily scraper runner — scrapers only, no TMDB enrichment.

Runs all scrapers and cleans up expired events. Designed to complete in ~2h
so it fits comfortably within the Railway cron window.

TMDB enricher runs manually or weekly — not part of daily cron.
To run: python scrapers/tmdb_enricher.py

Orchestrates the pipeline per scraper:
    fetch_events()  →  classify()  →  enrich(venue)  →  save_events()

Run manually:
    python scrapers/run_scrapers_only.py
    python scrapers/run_scrapers_only.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from psycopg2 import OperationalError as Psycopg2OpError
from sqlalchemy.exc import OperationalError as SAOpError

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
from scrapers.museo_scraper import MuseoScraper
from scrapers.biografo_scraper import BiografoScraper
from scrapers.normandie_scraper import NormandieScraper
from scrapers.cineteca_scraper import CinetecaScraper
from scrapers.clubdejazz_scraper import ClubDeJazzScraper
from scrapers.thelonious_scraper import TheloniousScraper

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_scrapers_only")


# ── Pipeline helpers ─────────────────────────────────────────────────────────

def _run_pipeline(
    scraper: BaseScraper,
    db,
    dry_run: bool = False,
    verbose: bool = False,
    Session=None,
) -> dict[str, int]:
    """Run fetch → classify → enrich(venue) → (optionally) save for one scraper."""
    stats = {"found": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}

    raw_events = scraper.fetch_events()
    stats["found"] = len(raw_events)
    logger.info("[%s] fetched %d events", scraper.name, stats["found"])

    if not raw_events:
        return stats

    # Enrich in batches of 500 with a fresh session each batch so no single
    # session stays open long enough for Railway to kill it (~40 min timeout).
    ENRICH_BATCH = 500
    processed: list[dict] = []
    for enrich_start in range(0, len(raw_events), ENRICH_BATCH):
        enrich_chunk = raw_events[enrich_start : enrich_start + ENRICH_BATCH]
        with Session() as enrich_db:
            for ev in enrich_chunk:
                try:
                    ev = classifier.classify(ev)
                    ev = enricher.enrich(ev, enrich_db)
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

    COMMIT_EVERY = 200
    now = datetime.now(timezone.utc)

    for ev in processed:
        ev.setdefault("scraped_at", now)
        ev.setdefault("is_verified", False)

    for batch_start in range(0, len(processed), COMMIT_EVERY):
        batch = processed[batch_start : batch_start + COMMIT_EVERY]
        try:
            with Session() as save_db:
                for ev in batch:
                    try:
                        sp = save_db.begin_nested()
                        result = deduplicator.save_or_update(ev, save_db)
                        save_db.flush()
                        sp.commit()
                        stats[result] += 1
                    except (Psycopg2OpError, SAOpError) as exc:
                        # Connection-level failure: the session is unusable.
                        # Rollback and abandon the rest of this batch so we
                        # don't cascade "Can't reconnect" for every event.
                        logger.warning("Save failed for %r: %s", ev.get("name"), exc)
                        stats["failed"] += 1
                        try:
                            save_db.rollback()
                        except Exception:
                            pass
                        logger.warning(
                            "[%s] Connection dropped — abandoning batch %d–%d",
                            scraper.name, batch_start + 1, batch_start + len(batch),
                        )
                        break
                    except Exception as exc:
                        logger.warning("Save failed for %r: %s", ev.get("name"), exc)
                        try:
                            sp.rollback()
                        except Exception as sp_exc:
                            logger.warning(
                                "Savepoint rollback failed (%s) — abandoning batch "
                                "%d–%d for %s",
                                sp_exc,
                                batch_start + 1,
                                batch_start + len(batch),
                                scraper.name,
                            )
                            stats["failed"] += 1
                            break
                        stats["failed"] += 1
                save_db.commit()
                logger.debug(
                    "[%s] Batch %d–%d committed",
                    scraper.name, batch_start + 1, batch_start + len(batch),
                )
        except Exception as exc:
            logger.warning(
                "[%s] Batch session failed (events %d–%d): %s",
                scraper.name, batch_start + 1, batch_start + len(batch), exc,
            )

    events_with_links = [ev for ev in processed if ev.get("_trailer_url")]
    if events_with_links:
        try:
            with Session() as link_db:
                link_count = sum(
                    deduplicator.upsert_event_links(ev, link_db)
                    for ev in events_with_links
                )
                if link_count:
                    link_db.commit()
                    logger.info("[%s] Added %d trailer link(s)", scraper.name, link_count)
        except Exception as exc:
            logger.warning("[%s] Community link upsert failed: %s", scraper.name, exc)

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

_SCRAPER_SOURCE_PATTERNS: dict[str, str] = {
    "cinemark":     "cinemark:cl:%",
    "cinepolis":    "cinepolis:cl:%",
    "cineplanet":   "cineplanet:cl:%",
    "puntoticket":  "%puntoticket%",
    "comedypass":   "%comedypass%",
    "passline":     "%passline%",
    "ticketplus":   "%ticketplus%",
    "ticketmaster": "%ticketmaster%",
    "evently":      "%evently%",
    "portaldisc":   "%portaldisc%",
    "museo":        "museo:%",
    "biografo":     "biografo:%",
    "normandie":    "https://www.flow.cl/%",
    "cineteca":     "https://cinetecanacional.gob.cl/%",
    "clubdejazz":   "clubdejazz:%",
    "thelonious":   "thelonious:%",
}


def _check_stale_scrapers(
    db,
    stale_days: int = 2,
    failed_names: list[str] | None = None,
) -> list[str]:
    from sqlalchemy import text  # noqa: PLC0415

    stale = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    names_to_check = failed_names if failed_names is not None else list(_SCRAPER_SOURCE_PATTERNS.keys())

    for name in names_to_check:
        pattern = _SCRAPER_SOURCE_PATTERNS.get(name)
        if not pattern:
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
            stale.append(name)
            logger.error("[stale-check] Scraper %r has NO events in DB — marking stale", name)
        else:
            if last_saved.tzinfo is None:
                from datetime import timezone as _tz  # noqa: PLC0415
                last_saved = last_saved.replace(tzinfo=_tz.utc)
            if last_saved < cutoff:
                stale.append(name)
                logger.error(
                    "[stale-check] Scraper %r last saved %s UTC — stale for >%d days",
                    name, last_saved.strftime("%Y-%m-%d %H:%M"), stale_days,
                )
            else:
                logger.info(
                    "[stale-check] Scraper %r last saved %s UTC — within threshold",
                    name, last_saved.strftime("%Y-%m-%d %H:%M"),
                )

    return stale


# ── TMDB incremental enrichment ──────────────────────────────────────────────

def _run_tmdb_enrichment_incremental(db, since_hours: int = 24) -> None:
    """Enrich only Cine events scraped in the last N hours with TMDB metadata.

    Runs in ~5-10 min (vs 20+ hours for full enrichment) because it only
    processes new films introduced in today's scraper run.
    """
    try:
        from scrapers.tmdb_enricher import apply_tmdb_to_cinema_events  # noqa: PLC0415
        stats = apply_tmdb_to_cinema_events(db, since_hours=since_hours)
        logger.info(
            "[tmdb] incremental enriched=%d  trailers_added=%d  not_found=%d",
            stats["enriched"], stats["trailers_added"], stats["not_found"],
        )
    except EnvironmentError as exc:
        logger.warning("[tmdb] Skipping enrichment — %s", exc)
    except Exception as exc:
        logger.error("[tmdb] Enrichment failed: %s", exc, exc_info=True)


# ── Expired event cleanup ─────────────────────────────────────────────────────

def _cleanup_expired_events(db, dry_run: bool = False) -> dict[str, int]:
    from sqlalchemy import text  # noqa: PLC0415

    cutoff_cine  = (datetime.now(timezone.utc) - timedelta(days=14)).date()
    cutoff_other = (datetime.now(timezone.utc) - timedelta(days=60)).date()

    stats = {"cine_deleted": 0, "other_deleted": 0}

    if dry_run:
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
    parser = argparse.ArgumentParser(description="Eventify scraper runner (scrapers only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse events but do NOT write to the database")
    parser.add_argument("--max-pages", type=int, default=10,
                        help="Maximum listing pages per scraper (default: 10)")
    parser.add_argument("--max-events", type=int, default=0,
                        help="Stop after this many events per scraper (0 = unlimited)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print price and description for each sample event")
    parser.add_argument("--enrich", action="store_true",
                        help="Run museum Phase A venue enrichment before scraping (monthly)")
    args = parser.parse_args()

    run_start = datetime.now(timezone.utc)
    logger.info("━" * 50)
    logger.info("SCRAPER RUN STARTED%s — %s UTC",
                " (DRY RUN)" if args.dry_run else "",
                run_start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("━" * 50)

    engine = create_engine(_get_database_url(), pool_pre_ping=True)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    if args.enrich:
        logger.info("━━━ Running museum venue enrichment (Phase A) ━━━")
        museo = MuseoScraper()
        museo.run_venue_enrichment()

    totals = {"found": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0}
    scraper_ok: list[str] = []
    scraper_err: list[str] = []

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
        MuseoScraper(max_events=args.max_events),
        BiografoScraper(max_events=args.max_events),
        NormandieScraper(max_events=args.max_events),
        CinetecaScraper(max_events=args.max_events),
        ClubDeJazzScraper(max_events=args.max_events),
        TheloniousScraper(max_events=args.max_events),
    ]

    for scraper in scrapers:
        scraper_start = datetime.now(timezone.utc)
        logger.info("━━━ Running scraper: %s ━━━", scraper.name)
        try:
            with Session() as db:
                try:
                    from sqlalchemy import text as _text  # noqa: PLC0415
                    db.execute(_text("SELECT 1"))
                except Exception:
                    db.rollback()
                stats = _run_pipeline(scraper, db, dry_run=args.dry_run, verbose=args.verbose, Session=Session)
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

    # ── Post-pipeline: incremental TMDB enrichment (last 24h only) ───────────
    if not args.dry_run:
        logger.info("━━━ Running TMDB enrichment (incremental — last 24h) ━━━")
        with Session() as db:
            _run_tmdb_enrichment_incremental(db, since_hours=24)

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

    logger.info("━━━ Checking for stale scrapers ━━━")
    with Session() as db:
        stale = _check_stale_scrapers(db, stale_days=2)
    if stale:
        logger.error(
            "ALERT: %d scraper(s) have been stale for >%d days: %s — exiting with code 1",
            len(stale), 2, ", ".join(stale),
        )
        sys.exit(1)
    elif scraper_err and not scraper_ok:
        logger.error("All %d scrapers failed — exiting with code 1", len(scraper_err))
        sys.exit(1)
    else:
        logger.info("All scrapers within freshness threshold — run complete.")


if __name__ == "__main__":
    main()
