#!/usr/bin/env python3
"""Upload venue images from assets/venues/ to Cloudinary and update the database.

Usage:
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/eventify \
        python scripts/upload_venue_images.py
"""

import os
import sys
import unicodedata
import difflib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cloudinary
import cloudinary.uploader
import psycopg2

# ── Config ────────────────────────────────────────────────────────────────────

cloudinary.config(
    cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key=os.environ["CLOUDINARY_API_KEY"],
    api_secret=os.environ["CLOUDINARY_API_SECRET"],
)

ASSETS_DIR = Path(
    "/Users/antoniocampos/Desktop/eventifye2e"
    "/eventify-frontend-feat-integrate-e2e/assets/venues"
)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/eventify",
)

# ── Manual slug → venue id mapping ───────────────────────────────────────────
# Covers every slug whose name is too abbreviated, misspelled, or ambiguous
# for fuzzy matching to resolve reliably.  None = not in DB (skip quietly).

SLUG_TO_VENUE_ID: Dict[str, Optional[int]] = {
    # Bars / clubs
    "aeronautico":       83,
    "allende":           85,
    "artealameda":      102,
    "backroom":           9,
    "bar-el-clan":        1,
    "barderene":         16,
    "barelbajo":          6,
    "bareltunel":        37,
    "bargrez":            2,
    "barlacapital":       5,
    "barlebajo":          6,   # alternate slug → same venue
    "barnechea":        None,  # not in DB
    "barvictoria":       14,
    "blondie":           31,
    "cachafaz":          27,
    "clubamanda":        36,
    "clubchocolate":     29,
    "clubjazzsantiago":   4,
    "clubmiguel":         7,
    "clubroom":          35,
    "comedy":            20,
    "doblestandup":      24,
    "fibrebar":          26,
    "fortunato":         17,
    "galpon":            12,
    "granrefugio":       18,
    "granrefugio2":      19,
    "granrefugio3":    None,   # not in DB
    "honesto":           21,   # shared image for all Honesto Mike branches
    "illuminati":        38,
    "joanjara":        None,   # not in DB
    "labatuta":           8,
    "laferia":           32,
    "lpa":               15,
    "onaciu":            13,
    "palermo":           11,
    "subte":             33,
    "troma":           None,   # not in DB

    # Theatres
    "camilohenriquez":   58,
    "cariola":           55,
    "caupo":             57,
    "coliseo":           65,
    "finis":             54,
    "ictus":             61,
    "municipalstgo":     53,
    "nacional":          71,
    "sangines":          64,
    "teatrochile":       63,
    "teatrolascondes":   59,
    "teatromemoria":     66,
    "teatronovedades":   67,
    "teatrooriente":     60,
    "teatrouc":          62,
    "teatrozoco":        68,
    "thelonius":          3,
    "thelonius2":         3,   # alternate photo, same venue

    # Stadiums / arenas
    "bicentenario":      69,
    "bicentenariovita":  75,
    "claro":             73,
    "monumental":        72,
    "movistar":          70,
    "santalaura":        74,

    # Museums
    "bellas":            77,
    "bellas2":           77,
    "chascona":          87,
    "cousino":           86,
    "educacion":         92,
    "historianatural":   82,
    "historiconacional": 84,
    "mac":               78,
    "macquinta":         79,
    "mavi":              90,
    "mim":               88,
    "muilascondes":      89,
    "museomemoria":      80,
    "precolombino":      81,
    "telegrafico":       91,
    "violetaparra":      93,

    # Cultural centres
    "casonanemesio":    106,
    "cce":              101,
    "cce2":             101,   # alternate photo, same venue
    "centrolascondes":  105,
    "cnac":             109,
    "corregidor":       108,
    "espacioincluir":    94,
    "gam":               96,
    "lamoneda":          95,
    "mapocho":           97,
    "matucana":          98,
    "montecarmelo":     100,
    "mincap":          None,   # not in DB
    "nunoa":            104,
    "ohiggins":          76,
    "rojasmagallanes":  107,

    # Cinemas
    "biografo":         110,
    "ccc":              117,
    "ceina":            118,
    "cinem100":         116,
    "cinemark":         128,   # shared chain image → Alto Las Condes
    "cinenemesio":      112,
    "cineplanet":       135,   # shared chain image → Costanera Center
    "cinepolis":        120,   # shared chain image → La Reina
    "cinetecanacional": 114,
    "cinetecauchile":   115,
    "cineuc":           119,
    "normandie":        111,

    # Performance / concert halls
    "citylabgam":        50,
    "nescafe":           51,
    "omnilab":           45,
    "riesco":            42,
    "salabella":         48,
    "salaceina":        118,
    "salaegana":         49,
    "salagente":         39,
    "salak":            113,
    "salamaster":        46,
    "salametronomo":     47,
    "salasinfonica":     44,
    "scdegana":          49,

    # Parks / outdoor
    "centroparque":      40,
    "padrehurtado":      41,

    # Other
    "loprado":           52,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize(text: str) -> str:
    """Lowercase, strip accents, keep only alphanumeric + spaces."""
    t = strip_accents(text.lower())
    return "".join(c if c.isalnum() or c == " " else " " for c in t).strip()


def fuzzy_match(slug: str, venues: List[Tuple[int, str]], threshold: float = 0.55) -> Optional[int]:
    """Return venue id of best fuzzy match, or None if below threshold."""
    slug_norm = normalize(slug.replace("-", " "))
    best_id, best_score = None, 0.0
    for vid, vname in venues:
        vname_norm = normalize(vname)
        score = difflib.SequenceMatcher(None, slug_norm, vname_norm).ratio()
        # Also try matching just the first word(s) of the venue name
        short = " ".join(vname_norm.split()[:3])
        score = max(score, difflib.SequenceMatcher(None, slug_norm, short).ratio())
        if score > best_score:
            best_score, best_id = score, vid
    return best_id if best_score >= threshold else None


def parse_image_files(assets_dir: Path) -> List[dict]:
    """Return list of {path, slug, kind} for every PNG in assets_dir."""
    images = []
    for p in sorted(assets_dir.glob("*.png")):
        name = p.stem  # e.g. "bar-el-clan-cover"
        if name.endswith("-cover"):
            slug = name[: -len("-cover")]
            kind = "cover"
        elif name.endswith("-profile"):
            slug = name[: -len("-profile")]
            kind = "profile"
        else:
            print(f"  SKIP  {p.name}  (no -cover/-profile suffix)")
            continue
        images.append({"path": p, "slug": slug, "kind": kind})
    return images


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not ASSETS_DIR.exists():
        sys.exit(f"ERROR: assets dir not found: {ASSETS_DIR}")

    images = parse_image_files(ASSETS_DIR)
    total = len(images)
    print(f"\nFound {total} images in {ASSETS_DIR}\n")

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM venues ORDER BY id")
    all_venues: List[Tuple[int, str]] = cur.fetchall()
    venue_by_id = {vid: vname for vid, vname in all_venues}

    uploaded = 0
    cover_updated = 0
    profile_updated = 0
    skipped_no_match: list[str] = []
    skipped_no_db: list[str] = []

    for i, img in enumerate(images, 1):
        slug = img["slug"]
        kind = img["kind"]
        path = img["path"]

        # ── Resolve venue id ──────────────────────────────────────────────
        if slug in SLUG_TO_VENUE_ID:
            venue_id = SLUG_TO_VENUE_ID[slug]
            if venue_id is None:
                skipped_no_db.append(slug)
                print(f"  [{i:3}/{total}] SKIP   {path.name}  (not in DB)")
                continue
            match_source = "manual"
        else:
            venue_id = fuzzy_match(slug, all_venues)
            if venue_id is None:
                skipped_no_match.append(slug)
                print(f"  [{i:3}/{total}] SKIP   {path.name}  (no fuzzy match)")
                continue
            match_source = "fuzzy"

        vname = venue_by_id[venue_id]

        # ── Upload to Cloudinary ──────────────────────────────────────────
        public_id = f"eventify/venues/{slug}-{kind}"
        try:
            result = cloudinary.uploader.upload(
                str(path),
                public_id=public_id,
                overwrite=True,
                resource_type="image",
            )
            url = result["secure_url"]
            uploaded += 1
        except Exception as e:
            print(f"  [{i:3}/{total}] ERROR  {path.name}  upload failed: {e}")
            continue

        # ── Update DB ─────────────────────────────────────────────────────
        col = "cover_image_url" if kind == "cover" else "profile_image_url"
        cur.execute(
            f"UPDATE venues SET {col} = %s WHERE id = %s",
            (url, venue_id),
        )
        conn.commit()

        if kind == "cover":
            cover_updated += 1
        else:
            profile_updated += 1

        print(
            f"  [{i:3}/{total}] OK     {path.name}"
            f"  →  venue {venue_id} ({vname[:35]})  [{match_source}]"
        )

    cur.close()
    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print(f"  Images uploaded:        {uploaded}/{total}")
    print(f"  Cover URLs updated:     {cover_updated}")
    print(f"  Profile URLs updated:   {profile_updated}")
    print(f"  Skipped (not in DB):    {len(skipped_no_db)}")
    print(f"  Skipped (no match):     {len(skipped_no_match)}")

    if skipped_no_db:
        print(f"\n  Not in DB:  {', '.join(sorted(set(skipped_no_db)))}")
    if skipped_no_match:
        print(f"\n  No match:   {', '.join(sorted(set(skipped_no_match)))}")
    print()


if __name__ == "__main__":
    main()
