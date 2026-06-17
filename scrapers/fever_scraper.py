"""Fever Santiago scraper.

feverup.com/es/santiago/que-hacer serves a ~1MB page that embeds a
600KB JSON blob in a <script> tag keyed
WhatPlanFilterService.getWPFSkeleton.SCL967.  That blob contains all
current Santiago listings with id, name, price, and first session date.

Each plan's detail page /m/{id} returns Event JSON-LD with startDate,
endDate, location (venue name + coordinates), and offers (price).

No Playwright needed — both pages respond correctly to plain requests.

Architecture:
  1. GET listing → parse WPF skeleton JSON → collect unique plans
  2. Filter plans by cultural relevance (name keyword check)
  3. GET /m/{id} for each plan → extract Event JSON-LD
  4. One event dict per plan, using startDate as the event date

Run:
    python scrapers/fever_scraper.py --dry-run
    python scrapers/fever_scraper.py --dry-run --verbose
    python scrapers/fever_scraper.py --max-events 5
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

LISTING_URL     = "https://feverup.com/es/santiago/que-hacer"
DETAIL_URL_TMPL = "https://feverup.com/m/{plan_id}"
REQUEST_DELAY   = 1.5
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://feverup.com/",
}

# ── Category inference ────────────────────────────────────────────────────────

_CATEGORY_RULES: list[tuple[list[str], str]] = [
    (
        ["concierto", "candlelight", "candlelit", "música", "musica", "music",
         "orquesta", "sinfoni", "jazz", "rock ", " pop ", "banda ", "tributo a",
         "tribute"],
        "Música",
    ),
    (
        ["teatro", "obra de teatro", "ballet", "danza", "baile", "dance",
         "circo"],
        "Teatro",
    ),
    (
        ["comedia", "stand up", "stand-up", "monólogo", "monologo",
         "humor", "impro", "sketch"],
        "Comedia",
    ),
    (
        ["nocturna", "nocturno", "fiesta ", " fiesta", "club nocturno",
         "after party", "after-party"],
        "Vida Nocturna",
    ),
    (
        ["familiar", "infantil", "niños", "ninos", "niñ", "kids",
         "bebés", "bebes", "familia"],
        "Familia",
    ),
    (
        ["exposición", "exposicion", "museo", "galería", "galeria",
         "inmersiv", "arte ", " arte", "frida", "vinci", "picasso",
         "monet", "van gogh", "fotografía", "fotografia",
         "dopamine", "harry potter", "experiencia"],
        "Arte",
    ),
]

_EXCLUDE_PATTERNS: list[str] = [
    "tarjeta regalo", "tarjeta de regalo", "gift card",
    "escape room", "sala de escape",
    "tour ", " tours", "excursión", "excursion", "visita guiada",
    "curso ", "taller ", "workshop", "charla ", "congreso", "seminario",
    "deport", "partido ", "campeonato", "maratón", "maraton", "triatlón",
]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _infer_category(name: str) -> str:
    name_l = name.lower()
    for keywords, cat in _CATEGORY_RULES:
        if any(kw in name_l for kw in keywords):
            return cat
    return "Arte"


def _should_include(name: str) -> bool:
    name_l = name.lower()
    return not any(pat in name_l for pat in _EXCLUDE_PATTERNS)


def _parse_iso(raw: str) -> tuple[str, str] | None:
    """'2026-06-17T15:00:00-04:00' → ('2026-06-17', '15:00') or None."""
    try:
        dt = datetime.fromisoformat(raw)
        return dt.date().isoformat(), dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return None


# ── Scraper ───────────────────────────────────────────────────────────────────


class FeverScraper(BaseScraper):
    """Scrapes cultural events from Fever Santiago listing."""

    name = "fever"

    def __init__(self, max_events: int = 0) -> None:
        super().__init__()
        self.max_events = max_events
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── Step 1 ────────────────────────────────────────────────────────────────

    def _fetch_plans(self) -> list[dict]:
        """Return deduplicated plan dicts from the WPF skeleton JSON blob."""
        resp = self.session.get(LISTING_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "lxml")

        wpf_prefix = "WhatPlanFilterService.getWPFSkeleton"
        for sc in soup.find_all("script"):
            txt = sc.string or ""
            if wpf_prefix not in txt or len(txt) < 10_000:
                continue
            try:
                blob = json.loads(txt)
            except json.JSONDecodeError:
                continue
            wpf_key = next((k for k in blob if wpf_prefix in k), None)
            if not wpf_key:
                continue

            skeleton = (blob[wpf_key].get("skeleton")) or []
            seen: set[int] = set()
            plans: list[dict] = []
            for section in skeleton:
                for plan in (section.get("content") or {}).get("plans") or []:
                    pid = plan.get("id")
                    if pid and pid not in seen:
                        seen.add(pid)
                        plans.append(plan)

            logger.info("[fever] %d unique plans in listing (skeleton sections: %d)",
                        len(plans), len(skeleton))
            return plans

        logger.warning("[fever] WPF skeleton JSON blob not found — page structure may have changed")
        return []

    # ── Step 2 ────────────────────────────────────────────────────────────────

    def _fetch_detail(self, plan_id: int, listing_plan: dict) -> dict[str, Any] | None:
        """Fetch /m/{plan_id}, extract Event JSON-LD, return event dict or None."""
        url = DETAIL_URL_TMPL.format(plan_id=plan_id)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("[fever] Detail page failed plan=%d: %s", plan_id, exc)
            return None

        soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "lxml")

        # ── Event JSON-LD ─────────────────────────────────────────────────────
        event_ld: dict = {}
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(sc.string or "")
                if isinstance(d, dict) and d.get("@type") == "Event":
                    event_ld = d
                    break
            except (json.JSONDecodeError, AttributeError):
                continue

        # ── Name ──────────────────────────────────────────────────────────────
        name = (
            event_ld.get("name")
            or listing_plan.get("name")
            or ""
        ).strip()
        if not name:
            return None

        # ── Date + time ───────────────────────────────────────────────────────
        start_raw = (
            event_ld.get("startDate")
            or listing_plan.get("first_active_session_date")
            or ""
        )
        parsed = _parse_iso(start_raw)
        if not parsed:
            logger.info("[fever] No date for plan %d (%r) — skipping", plan_id, name)
            return None
        iso_date, ev_time = parsed
        if date.fromisoformat(iso_date) < date.today():
            logger.info("[fever] Past event plan %d (%r) — skipping", plan_id, name)
            return None

        # ── Venue ─────────────────────────────────────────────────────────────
        location = event_ld.get("location") or {}
        venue_name = (location.get("name") or "").strip() or None

        # ── Description (og:description avoids JSON-LD encoding issues) ───────
        description: str | None = None
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            description = (og_desc.get("content") or "").strip() or None

        # ── Image ─────────────────────────────────────────────────────────────
        image_url: str | None = None
        og_img = soup.find("meta", property="og:image")
        if og_img:
            image_url = (og_img.get("content") or "").strip() or None
        if not image_url:
            image_url = (listing_plan.get("cover_image") or None)

        # ── Price: Event LD offers → fallback to listing price_info ──────────
        price_range: list[float] | None = None
        offers_raw = event_ld.get("offers") or {}
        offers = offers_raw[0] if isinstance(offers_raw, list) else offers_raw
        low  = offers.get("lowPrice")  or offers.get("price")
        high = offers.get("highPrice") or low
        if low is not None:
            try:
                price_range = [float(low), float(high)]
            except (TypeError, ValueError):
                pass
        if price_range is None:
            amount = (listing_plan.get("price_info") or {}).get("amount")
            if amount is not None:
                try:
                    price_range = [float(amount), float(amount)]
                except (TypeError, ValueError):
                    pass

        # ── Assemble ──────────────────────────────────────────────────────────
        category = _infer_category(name)
        source_url = f"fever:{plan_id}:{iso_date}"

        ev: dict[str, Any] = {
            "name":       name,
            "date":       iso_date,
            "time_start": ev_time,
            "source_url": source_url,
            "url":        url,
            "category":   category,
        }
        if venue_name:
            ev["venue_name"] = venue_name
        if description:
            ev["description"] = description
        if image_url:
            ev["image_url"] = image_url
        if price_range:
            ev["price_range"] = price_range

        return ev

    # ── Main entry ────────────────────────────────────────────────────────────

    def fetch_events(self) -> list[dict[str, Any]]:
        logger.info("[fever] Fetching listing: %s", LISTING_URL)
        try:
            plans = self._fetch_plans()
        except requests.RequestException as exc:
            raise RuntimeError(f"[fever] Listing page unreachable: {exc}") from exc

        if not plans:
            raise RuntimeError(
                "[fever] No plans found in listing — "
                "WPF skeleton key may have changed"
            )

        all_events: list[dict[str, Any]] = []

        for plan in plans:
            listing_name = (plan.get("name") or "").strip()
            if not listing_name:
                continue
            if not _should_include(listing_name):
                logger.debug("[fever] Excluded: %r", listing_name)
                continue

            plan_id = plan["id"]
            time.sleep(REQUEST_DELAY)
            ev = self._fetch_detail(plan_id, listing_plan=plan)
            if ev:
                all_events.append(ev)
                logger.debug(
                    "[fever] plan=%d  name=%r  date=%s  venue=%r  cat=%s",
                    plan_id, ev["name"], ev["date"],
                    ev.get("venue_name"), ev.get("category"),
                )

            if self.max_events and len(all_events) >= self.max_events:
                break

        logger.info("[fever] Total events: %d", len(all_events))
        return all_events


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="Fever Santiago scraper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-events", type=int, default=0)
    args = parser.parse_args()

    scraper = FeverScraper(max_events=args.max_events)
    events = scraper.fetch_events()

    print(f"\n── Fever dry-run: {len(events)} events ──────────────────────")
    for ev in events[:20]:
        print(
            f"\n  name      : {ev.get('name')!r}\n"
            f"  date      : {ev.get('date')}\n"
            f"  time_start: {ev.get('time_start')}\n"
            f"  venue     : {ev.get('venue_name')!r}\n"
            f"  category  : {ev.get('category')}\n"
            f"  price     : {ev.get('price_range')}\n"
            f"  source_url: {ev.get('source_url')}"
        )
        if args.verbose:
            print(f"  url       : {ev.get('url')}")
            print(f"  desc      : {str(ev.get('description', ''))[:100]!r}")

    if not args.dry_run:
        from scrapers.base_scraper import make_scraper_session
        from scrapers import classifier, enricher, deduplicator

        engine, db = make_scraper_session()
        now = datetime.now(timezone.utc)
        stats: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        for ev in events:
            try:
                ev = classifier.classify(ev)
                ev = enricher.enrich(ev, db)
                ev.setdefault("scraped_at", now)
                ev.setdefault("is_verified", False)
                result = deduplicator.save_or_update(ev, db)
                stats[result] += 1
            except Exception as exc:
                logger.warning("Failed to save %r: %s", ev.get("name"), exc)
                db.rollback()
                stats["failed"] += 1
        db.commit()
        db.close()
        engine.dispose()
        print(f"\n── Results: {stats}")
