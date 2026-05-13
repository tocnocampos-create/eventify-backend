"""
Geocode venues with fallback city-center coordinates using Nominatim (OpenStreetMap).

Strategies (applied in order, first success wins):
  1. Address query  → accept place_rank >= 22
  2. Name query     → accept place_rank >= 28 (POI/amenity only)
  3. Name without common prefix (Bar, Club, Teatro…) → accept place_rank >= 28
  4. Name + "Santiago" without "Chile" → accept place_rank >= 28
  5. Any of the above returning APPROXIMATE within 20 km of Santiago center
     (haversine distance check) — last-resort fallback

Usage (from project root):
    python scripts/geocode_venues.py [--dry-run]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import datetime

import psycopg2
import requests

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz@shuttle.proxy.rlwy.net:17408/railway",
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "Eventify/1.0 coordinates-audit (admin@eventify.cl)"}
REQUEST_DELAY = 1.1  # Nominatim rate limit: 1 req/s

# Santiago Metro Region bounding box
RM_LAT_MIN, RM_LAT_MAX = -34.4, -32.9
RM_LNG_MIN, RM_LNG_MAX = -71.8, -69.8

# Santiago city center for distance fallback
STGO_LAT, STGO_LNG = -33.4372, -70.6506
MAX_APPROX_KM = 20.0

# Nominatim place_rank thresholds
RANK_MIN_ADDRESS = 22   # road / interpolated address / building
RANK_MIN_NAME    = 28   # amenity / building / POI node

COARSE_TYPES = {
    "road", "suburb", "neighbourhood", "city", "town", "village",
    "county", "state", "country", "administrative",
}

SKIP_FRAGMENTS = [
    "por confirmar",
    "secret spot",
    "la dirección será revelada",
]

# Common venue-type prefixes to strip for retry search
STRIP_PREFIXES = [
    "bar y centro cultural ", "bar ", "club ", "teatro ", "espacio ",
    "sala de concierto ", "sala ", "centro cultural ", "centro de eventos ",
    "centro de artes ", "centro ", "restobar ", "restaurante ", "restaurant ",
    "gimnasio ", "estadio ", "hotel ", "auditorio ", "cine ", "disco ",
    "discoteca ", "casa ", "parque ", "circulo ", "círculo ",
]


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def in_rm_bounds(lat: float, lng: float) -> bool:
    return RM_LAT_MIN <= lat <= RM_LAT_MAX and RM_LNG_MIN <= lng <= RM_LNG_MAX


def within_stgo(lat: float, lng: float) -> bool:
    return haversine_km(STGO_LAT, STGO_LNG, lat, lng) <= MAX_APPROX_KM


def geocode(query: str) -> dict | None:
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        results = resp.json()
    except Exception as exc:
        print(f"  ⚠ API error: {exc}")
        return None

    if not results:
        return None

    r = results[0]
    return {
        "lat": float(r["lat"]),
        "lng": float(r["lon"]),
        "place_rank": int(r.get("place_rank", 0)),
        "addresstype": r.get("addresstype", ""),
        "display_name": r.get("display_name", ""),
    }


def strip_prefix(name: str) -> str | None:
    nl = name.lower()
    for prefix in STRIP_PREFIXES:
        if nl.startswith(prefix):
            return name[len(prefix):]
    return None


def clean_address(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.replace("&#39;", "'").replace("&amp;", "&").strip()
    if cleaned in ("", ",", ", ") or cleaned.startswith(",  "):
        return None
    return cleaned


def try_geocode_strategies(name: str, address: str | None) -> tuple[dict | None, str, str]:
    """
    Returns (geo_result, strategy_label, reason_if_none).
    Tries strategies 1-5 in order.
    """
    candidate_approx: dict | None = None
    approx_label: str = ""

    def _try(query: str, rank_min: int, label: str) -> dict | None:
        nonlocal candidate_approx, approx_label
        geo = geocode(query)
        time.sleep(REQUEST_DELAY)
        if geo is None:
            return None
        lat, lng = geo["lat"], geo["lng"]
        if not in_rm_bounds(lat, lng):
            return None
        # Precise enough?
        if geo["place_rank"] >= rank_min and geo["addresstype"] not in COARSE_TYPES:
            return geo
        # Store as approx candidate if within 20 km
        if candidate_approx is None and within_stgo(lat, lng):
            candidate_approx = geo
            approx_label = label
        return None

    # 1. By address
    if address:
        geo = _try(f"{address}, Santiago, Chile", RANK_MIN_ADDRESS, "address")
        if geo:
            return geo, "address", ""

    # 2. By full name
    geo = _try(f"{name}, Santiago, Chile", RANK_MIN_NAME, "name")
    if geo:
        return geo, "name", ""

    # 3. Name without common prefix
    stripped = strip_prefix(name)
    if stripped and stripped.lower() != name.lower():
        geo = _try(f"{stripped}, Santiago, Chile", RANK_MIN_NAME, f"name-stripped({stripped})")
        if geo:
            return geo, f"name-stripped", ""

    # 4. Name + "Santiago" without "Chile"
    geo = _try(f"{name} Santiago", RANK_MIN_NAME, "name-no-chile")
    if geo:
        return geo, "name-no-chile", ""

    # 5. Fallback: best approximate within 20 km
    if candidate_approx:
        return candidate_approx, f"approx({approx_label})", ""

    return None, "", "No geocode result within RM bounds or 20 km of Santiago"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    args = parser.parse_args()

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, venue_type, address,
               coordinates[1]::float AS lat,
               coordinates[2]::float AS lng
        FROM venues
        WHERE coordinates IS NOT NULL
        AND coordinates[1]::float BETWEEN -33.438 AND -33.437
        AND coordinates[2]::float BETWEEN -70.651 AND -70.650
        ORDER BY name;
    """)
    venues = cur.fetchall()
    total = len(venues)
    print(f"Found {total} venues still with fallback coordinates.\n")

    auto_fixed: list[dict] = []
    needs_review: list[dict] = []
    outside_santiago: list[dict] = []

    for idx, (venue_id, name, venue_type, raw_address, old_lat, old_lng) in enumerate(venues, 1):
        print(f"[{idx}/{total}] {name} (id={venue_id})", end="", flush=True)

        name_lower = name.lower()
        if any(frag in name_lower for frag in SKIP_FRAGMENTS):
            print(" → SKIP (placeholder)")
            needs_review.append({"id": venue_id, "name": name, "address": raw_address,
                                  "reason": "Placeholder name"})
            continue

        address = clean_address(raw_address)
        geo, strategy, reason = try_geocode_strategies(name, address)

        if geo is None:
            print(f" → NO RESULT")
            needs_review.append({"id": venue_id, "name": name, "address": raw_address,
                                  "reason": reason or "No geocode result"})
            continue

        lat, lng = geo["lat"], geo["lng"]

        if not in_rm_bounds(lat, lng):
            print(f" → OUTSIDE RM: ({lat:.4f}, {lng:.4f})")
            outside_santiago.append({"id": venue_id, "name": name,
                                     "old_coords": f"[{old_lat}, {old_lng}]",
                                     "new_coords": f"[{lat:.6f}, {lng:.6f}]",
                                     "note": f"Geocoded outside RM (source={strategy})"})
            continue

        if not args.dry_run:
            cur.execute(
                "UPDATE venues SET coordinates = ARRAY[%s, %s] WHERE id = %s",
                (lat, lng, venue_id),
            )
            conn.commit()

        label = "DRY" if args.dry_run else "FIXED"
        rank_info = f"rank={geo['place_rank']},type={geo['addresstype']}"
        print(f" → {label} [{strategy}/{rank_info}] ({lat:.6f}, {lng:.6f})")
        auto_fixed.append({
            "id": venue_id, "name": name,
            "old_coords": f"[{old_lat}, {old_lng}]",
            "new_coords": f"[{lat:.6f}, {lng:.6f}]",
            "place_rank": geo["place_rank"],
            "addresstype": geo["addresstype"],
            "strategy": strategy,
        })

    conn.close()

    # ── Report ────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dry_label = " (DRY RUN)" if args.dry_run else ""
    lines = [
        "# Venue Coordinates Audit — Pass 2",
        f"Generated: {now}{dry_label}",
        f"Venues with fallback coords at start: {total}",
        "",
        f"## A) AUTO-FIXED ({len(auto_fixed)} venues)",
        "",
        "| id | name | new coords | strategy | rank |",
        "|----|------|-----------|----------|------|",
    ]
    for v in auto_fixed:
        lines.append(f"| {v['id']} | {v['name']} | {v['new_coords']} | {v['strategy']} ({v['addresstype']}) | {v['place_rank']} |")

    lines += [
        "",
        f"## B) NEEDS MANUAL REVIEW ({len(needs_review)} venues)",
        "",
        "| id | name | address | reason |",
        "|----|------|---------|--------|",
    ]
    for v in needs_review:
        addr = str(v.get("address") or "—").replace("|", "\\|")
        lines.append(f"| {v['id']} | {v['name']} | {addr} | {v['reason']} |")

    lines += [
        "",
        f"## C) OUTSIDE SANTIAGO / RM ({len(outside_santiago)} venues)",
        "",
        "| id | name | geocoded coords | note |",
        "|----|------|----------------|------|",
    ]
    for v in outside_santiago:
        lines.append(f"| {v['id']} | {v['name']} | {v['new_coords']} | {v['note']} |")

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinates_audit_pass2.md")
    with open(report_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n{'='*60}")
    print(f"  Auto-fixed      : {len(auto_fixed)}")
    print(f"  Needs review    : {len(needs_review)}")
    print(f"  Outside Santiago: {len(outside_santiago)}")
    print(f"  Report saved    : {report_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
