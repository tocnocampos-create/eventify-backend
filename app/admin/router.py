"""Admin panel FastAPI router.

Mounted at /admin in app/main.py.
All views are server-rendered via Jinja2 templates.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

# Ensure scrapers package is importable when running inside Docker
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.db.base import get_db
from app.db.models import Event, Venue

router = APIRouter()

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# ── Helpers ───────────────────────────────────────────────────────────────────

_PLAZA_ARMAS = (-33.4372, -70.6506)
_COORD_TOLERANCE = 0.001


def _is_auto_created(v: Venue) -> bool:
    """Heuristic: venue was auto-created by the enricher (not seeded/verified)."""
    return (
        v.scraped_at is not None
        and not v.is_verified
        and v.description is None
        and v.source_url is None
    )


def _is_plaza_armas(v: Venue) -> bool:
    if not v.coordinates or len(v.coordinates) < 2:
        return False
    return (
        abs(v.coordinates[0] - _PLAZA_ARMAS[0]) < _COORD_TOLERANCE
        and abs(v.coordinates[1] - _PLAZA_ARMAS[1]) < _COORD_TOLERANCE
    )


def _scraper_from_source_url(source_url: str | None) -> str | None:
    """Infer scraper name from source_url prefix."""
    if not source_url:
        return None
    known = [
        "puntoticket", "cinemark", "cinepolis", "passline",
        "ticketplus", "ticketmaster", "evently", "portaldisc",
    ]
    lower = source_url.lower()
    for k in known:
        if lower.startswith(k) or f"/{k}" in lower or f".{k}." in lower:
            return k
    # Try domain extraction for full HTTP URLs
    if lower.startswith("http"):
        try:
            from urllib.parse import urlparse
            host = urlparse(source_url).netloc.lower()
            for k in known:
                if k in host:
                    return k
        except Exception:
            pass
    return None


def _flash(request: Request, message: str, flash_type: str = "success") -> dict:
    return {"flash": message, "flash_type": flash_type}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    total_events = db.query(func.count(Event.id)).scalar() or 0
    total_venues = db.query(func.count(Venue.id)).scalar() or 0

    no_category = (
        db.query(func.count(Event.id))
        .filter(Event.category.is_(None))
        .scalar() or 0
    )
    auto_venues = sum(1 for v in db.query(Venue).all() if _is_auto_created(v))

    stats = [
        {"label": "Total events", "value": total_events},
        {"label": "Total venues", "value": total_venues},
        {"label": "Uncategorised events", "value": no_category},
        {"label": "Auto-created venues", "value": auto_venues},
    ]

    # Events by category
    by_category = (
        db.query(Event.category, func.count(Event.id).label("count"))
        .group_by(Event.category)
        .order_by(func.count(Event.id).desc())
        .all()
    )

    # Events by scraper (inferred from source_url prefix)
    all_sources = db.query(Event.source_url).all()
    scraper_counts: dict[str, int] = {}
    for (src,) in all_sources:
        key = _scraper_from_source_url(src) or "(unknown)"
        scraper_counts[key] = scraper_counts.get(key, 0) + 1
    by_scraper = sorted(
        [{"scraper": k, "count": v} for k, v in scraper_counts.items()],
        key=lambda x: -x["count"],
    )

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "stats": stats,
            "by_category": [{"category": r.category, "count": r.count} for r in by_category],
            "by_scraper": by_scraper,
        },
    )


# ── Venues ────────────────────────────────────────────────────────────────────

_VENUE_PAGE_SIZE = 50


@router.get("/venues", response_class=HTMLResponse)
def admin_venues(
    request: Request,
    page: int = 1,
    type: str = "",
    auto_created: str = "",
    db: Session = Depends(get_db),
    flash: str = "",
    flash_type: str = "success",
):
    q = db.query(Venue)
    if type:
        q = q.filter(Venue.venue_type == type)

    # venue_types for filter dropdown
    venue_types = [
        r[0] for r in db.query(Venue.venue_type).distinct().order_by(Venue.venue_type).all()
    ]

    all_venues = q.order_by(Venue.id).all()

    # Apply auto_created filter in Python (heuristic)
    if auto_created == "1":
        all_venues = [v for v in all_venues if _is_auto_created(v)]
    elif auto_created == "0":
        all_venues = [v for v in all_venues if not _is_auto_created(v)]

    total = len(all_venues)
    total_pages = max(1, (total + _VENUE_PAGE_SIZE - 1) // _VENUE_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * _VENUE_PAGE_SIZE
    venues_page = all_venues[offset: offset + _VENUE_PAGE_SIZE]

    # Build display dicts
    venue_rows = []
    for v in venues_page:
        event_count = db.query(func.count(Event.id)).filter(Event.venue_id == v.id).scalar() or 0
        venue_rows.append({
            "id": v.id,
            "name": v.name,
            "venue_type": v.venue_type,
            "coordinates": v.coordinates,
            "cover_image_url": v.cover_image_url,
            "website_url": v.website_url,
            "is_auto_created": _is_auto_created(v),
            "is_plaza_armas": _is_plaza_armas(v),
            "event_count": event_count,
        })

    return templates.TemplateResponse(
        "admin/venues.html",
        {
            "request": request,
            "venues": venue_rows,
            "venue_types": venue_types,
            "filters": {"type": type, "auto_created": auto_created},
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "flash": flash,
            "flash_type": flash_type,
        },
    )


@router.post("/venues/{venue_id}", response_class=RedirectResponse)
def admin_venue_update(
    venue_id: int,
    request: Request,
    name: str = Form(""),
    venue_type: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    cover_image_url: str = Form(""),
    website_url: str = Form(""),
    db: Session = Depends(get_db),
):
    venue = db.query(Venue).filter(Venue.id == venue_id).first()
    if not venue:
        return RedirectResponse("/admin/venues?flash=Venue+not+found&flash_type=error", status_code=303)

    if name.strip():
        venue.name = name.strip()
    if venue_type.strip():
        venue.venue_type = venue_type.strip()
    if lat.strip() and lng.strip():
        try:
            venue.coordinates = [float(lat), float(lng)]
        except ValueError:
            pass
    venue.cover_image_url = cover_image_url.strip() or None
    venue.website_url = website_url.strip() or None

    db.commit()
    return RedirectResponse(f"/admin/venues?flash=Venue+{venue_id}+saved", status_code=303)


# ── Events ────────────────────────────────────────────────────────────────────

_EVENT_PAGE_SIZE = 50

_STANDARD_CATEGORIES = [
    "Música", "Teatro", "Comedia", "Cine", "Arte", "Familia",
    "Vida Nocturna", "Feria", "Aire Libre", "City Tour", "Barrios",
]


@router.get("/events", response_class=HTMLResponse)
def admin_events(
    request: Request,
    page: int = 1,
    category: str = "",
    scraper: str = "",
    venue: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    flash: str = "",
    flash_type: str = "success",
):
    query = db.query(Event)
    if category:
        if category == "__none__":
            query = query.filter(Event.category.is_(None))
        else:
            query = query.filter(Event.category == category)
    if q:
        query = query.filter(func.lower(Event.name).contains(q.lower()))
    if venue:
        venue_ids = [
            v.id for v in db.query(Venue).filter(
                func.lower(Venue.name).contains(venue.lower())
            ).all()
        ]
        query = query.filter(Event.venue_id.in_(venue_ids))

    all_events = query.order_by(Event.date.desc(), Event.id.desc()).all()

    # Scraper filter in Python
    if scraper:
        all_events = [e for e in all_events if _scraper_from_source_url(e.source_url) == scraper]

    total = len(all_events)
    total_pages = max(1, (total + _EVENT_PAGE_SIZE - 1) // _EVENT_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * _EVENT_PAGE_SIZE
    events_page = all_events[offset: offset + _EVENT_PAGE_SIZE]

    # Venue name lookup
    venue_map: dict[int, str] = {}
    for eid in {e.venue_id for e in events_page if e.venue_id}:
        v = db.query(Venue).filter(Venue.id == eid).first()
        if v:
            venue_map[eid] = v.name

    event_rows = []
    for e in events_page:
        event_rows.append({
            "id": e.id,
            "name": e.name,
            "date": e.date,
            "category": e.category,
            "type": e.type,
            "url": e.url,
            "source_url": e.source_url,
            "scraper": _scraper_from_source_url(e.source_url),
            "venue_name": venue_map.get(e.venue_id) if e.venue_id else None,
            "price_range": e.price_range,
        })

    # Category list for filter + edit dropdowns
    db_categories = [
        r[0] for r in db.query(Event.category).distinct().order_by(Event.category).all()
    ]
    all_categories = sorted(set(_STANDARD_CATEGORIES) | {c for c in db_categories if c})

    # Scraper list for filter
    all_scrapers_raw = db.query(Event.source_url).all()
    scraper_set: set[str] = set()
    for (src,) in all_scrapers_raw:
        s = _scraper_from_source_url(src)
        if s:
            scraper_set.add(s)
    scrapers = sorted(scraper_set)

    return templates.TemplateResponse(
        "admin/events.html",
        {
            "request": request,
            "events": event_rows,
            "categories": all_categories,
            "scrapers": scrapers,
            "filters": {"category": category, "scraper": scraper, "venue": venue, "q": q},
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "flash": flash,
            "flash_type": flash_type,
        },
    )


@router.post("/events/{event_id}", response_class=RedirectResponse)
def admin_event_update(
    event_id: int,
    category: str = Form(""),
    type: str = Form(""),
    db: Session = Depends(get_db),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        return RedirectResponse("/admin/events?flash=Event+not+found&flash_type=error", status_code=303)

    event.category = category.strip() or None
    event.type = type.strip() or None
    db.commit()
    return RedirectResponse(f"/admin/events?flash=Event+{event_id}+saved", status_code=303)


# ── Classifier tester ─────────────────────────────────────────────────────────

def _build_rules_table() -> list[dict]:
    """Build a display list of all classifier rules."""
    from scrapers.classifier import KEYWORD_RULES, _RULE_PRIORITY, _RULE_TO_CATEGORY, _RULE_TO_TYPE

    rows = []
    for rule in _RULE_PRIORITY:
        rows.append({
            "name": rule,
            "category": _RULE_TO_CATEGORY.get(rule, "—"),
            "type": _RULE_TO_TYPE.get(rule, rule.replace("_", " ")),
            "keywords": KEYWORD_RULES.get(rule, []),
        })
    return rows


@router.get("/classifier", response_class=HTMLResponse)
def admin_classifier_get(request: Request):
    return templates.TemplateResponse(
        "admin/classifier.html",
        {
            "request": request,
            "form": {"name": "", "description": "", "venue_type": "", "category_hint": "", "locked_category": ""},
            "result": None,
            "rules": _build_rules_table(),
        },
    )


@router.post("/classifier", response_class=HTMLResponse)
def admin_classifier_post(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    venue_type: str = Form(""),
    category_hint: str = Form(""),
    locked_category: str = Form(""),
):
    from scrapers.classifier import classify

    event: dict[str, Any] = {
        "name": name.strip(),
        "description": description.strip(),
        "venue_type": venue_type.strip(),
    }
    if category_hint.strip():
        event["_category_hint"] = category_hint.strip()
    if locked_category.strip():
        event["_locked_category"] = locked_category.strip()

    classified = classify(event)

    # Determine source of category
    if locked_category.strip():
        source = "locked"
    elif classified.get("category") and category_hint.strip() and classified["category"] == category_hint.strip():
        source = "hint"
    elif classified.get("category"):
        source = "rule"
    else:
        source = "none"

    result = {
        "category": classified.get("category"),
        "type": classified.get("type"),
        "kids_friendly": classified.get("kids_friendly", False),
        "keywords": classified.get("keywords", []),
        "source": source,
        "raw_json": json.dumps(
            {k: v for k, v in classified.items() if not k.startswith("_")},
            ensure_ascii=False,
            indent=2,
        ),
    }

    return templates.TemplateResponse(
        "admin/classifier.html",
        {
            "request": request,
            "form": {
                "name": name,
                "description": description,
                "venue_type": venue_type,
                "category_hint": category_hint,
                "locked_category": locked_category,
            },
            "result": result,
            "rules": _build_rules_table(),
        },
    )


# ── Duplicates ────────────────────────────────────────────────────────────────

@router.get("/duplicates", response_class=HTMLResponse)
def admin_duplicates(request: Request, db: Session = Depends(get_db)):
    # Find (name, date) pairs with more than one event row
    dup_pairs = (
        db.query(Event.name, Event.date, func.count(Event.id).label("cnt"))
        .group_by(Event.name, Event.date)
        .having(func.count(Event.id) > 1)
        .order_by(func.count(Event.id).desc(), Event.date.desc())
        .all()
    )

    # Load venues for name lookup
    venue_map: dict[int, str] = {v.id: v.name for v in db.query(Venue).all()}

    groups = []
    for name, date, cnt in dup_pairs:
        events = (
            db.query(Event)
            .filter(Event.name == name, Event.date == date)
            .all()
        )
        # Only surface groups that have events from different scrapers
        scraper_set = {_scraper_from_source_url(e.source_url) for e in events}
        if len(scraper_set) < 2:
            continue

        groups.append({
            "name": name,
            "date": date,
            "events": [
                {
                    "id": e.id,
                    "source_url": e.source_url,
                    "scraper": _scraper_from_source_url(e.source_url),
                    "venue_name": venue_map.get(e.venue_id) if e.venue_id else None,
                    "category": e.category,
                    "price_range": e.price_range,
                }
                for e in events
            ],
        })

    return templates.TemplateResponse(
        "admin/duplicates.html",
        {"request": request, "groups": groups},
    )


@router.post("/duplicates/{event_id}/delete", response_class=RedirectResponse)
def admin_duplicate_delete(event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        db.delete(event)
        db.commit()
    return RedirectResponse("/admin/duplicates?flash=Event+deleted", status_code=303)
