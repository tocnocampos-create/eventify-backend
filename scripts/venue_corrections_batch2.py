"""venue_corrections_batch2.py — Second batch of venue corrections.

Parts 1-4: deletions, merges, renames/type fixes, Cloudinary image uploads, cinema chains.

Usage:
    source venv/bin/activate
    python scripts/venue_corrections_batch2.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
import traceback

import cloudinary
import cloudinary.uploader
import psycopg2
import psycopg2.extras

DRY_RUN = "--dry-run" in sys.argv

ASSETS = (
    "/Users/antoniocampos/Desktop/eventifye2e/"
    "eventify-frontend-feat-integrate-e2e/assets/venues"
)

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "dkugv4x31"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "539135763925632"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "aVSc6wZGpbeISO7Nw0Y_-_9pw8Q"),
    secure=True,
)

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz@shuttle.proxy.rlwy.net:17408/railway",
)


# ── helpers ────────────────────────────────────────────────────────────────────

def asset(filename: str) -> str | None:
    """Return full path if file exists, else None."""
    p = os.path.join(ASSETS, filename)
    return p if os.path.exists(p) else None


def upload(path: str, public_id: str) -> str:
    result = cloudinary.uploader.upload(
        path,
        public_id=public_id,
        overwrite=True,
        resource_type="image",
    )
    return result["secure_url"]


# ── counters ───────────────────────────────────────────────────────────────────
stats = {
    "deleted_venues": [],
    "merged": [],
    "renamed": [],
    "type_fixed": [],
    "images_ok": [],
    "images_missing": [],
    "images_error": [],
}


def main() -> None:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ══════════════════════════════════════════════════════════════════════════
    # PART 1 — DELETIONS
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 68)
    print("PART 1 — DELETIONS")
    print("=" * 68)

    # Venues to delete outright (delete events first, then venue)
    DELETE_VENUE_IDS = {
        406: "Xgym",
        407: "Zapping Sport Center",
        452: "Polideportivo 1, Parque Estadio",
        238: "Centro de Eventos Múnich - Peñaflor",
        436: "Centro de Extensión UC",
        361: "Club Camce",
        287: "Espacio Urbano La Reina",
        270: "Valle Hermoso",
        241: "Teatro Regional Cervantes",
        351: "Secret Spot",
        319: "RockStar Stage",
        432: "Teatro Colegio San Ignacio El Bosque",
    }

    for vid, vname in DELETE_VENUE_IDS.items():
        cur.execute("SELECT COUNT(*) as n FROM events WHERE venue_id=%s", (vid,))
        n = cur.fetchone()["n"]
        if not DRY_RUN:
            cur.execute("DELETE FROM events WHERE venue_id=%s", (vid,))
            cur.execute("DELETE FROM venues WHERE id=%s", (vid,))
            conn.commit()
        print(f"  DEL id={vid:4d} ({n} events) {vname!r}")
        stats["deleted_venues"].append(vname)

    # Special: Sala de Teatro Agustín Siré (id=316) → reassign 3 events to DETUCH (id=429)
    DETUCH_ID = 429
    SIRE_ID = 316
    cur.execute("SELECT COUNT(*) as n FROM events WHERE venue_id=%s", (SIRE_ID,))
    n_sire = cur.fetchone()["n"]
    if not DRY_RUN:
        cur.execute("UPDATE events SET venue_id=%s WHERE venue_id=%s", (DETUCH_ID, SIRE_ID))
        cur.execute("DELETE FROM venues WHERE id=%s", (SIRE_ID,))
        conn.commit()
    print(f"  REASSIGN id={SIRE_ID} ({n_sire} events) 'Sala de Teatro Agustín Siré' → DETUCH id={DETUCH_ID}")
    print(f"  DEL id={SIRE_ID} 'Sala de Teatro Agustín Siré' (alias added to DETUCH description)")
    stats["merged"].append(("Sala de Teatro Agustín Siré", "DETUCH"))
    stats["deleted_venues"].append("Sala de Teatro Agustín Siré")

    # ══════════════════════════════════════════════════════════════════════════
    # PART 2 — MERGES
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 68)
    print("PART 2 — MERGES")
    print("=" * 68)

    # 2a: Teatro Palermo Puente Alto → check
    cur.execute("SELECT id FROM venues WHERE name ILIKE '%palermo puente alto%'")
    row = cur.fetchone()
    if row:
        palermo_dup_id = row["id"]
        cur.execute("SELECT COUNT(*) as n FROM events WHERE venue_id=%s", (palermo_dup_id,))
        n = cur.fetchone()["n"]
        if not DRY_RUN:
            cur.execute("UPDATE events SET venue_id=11 WHERE venue_id=%s", (palermo_dup_id,))
            cur.execute("DELETE FROM venues WHERE id=%s", (palermo_dup_id,))
            conn.commit()
        print(f"  MERGE id={palermo_dup_id} ({n} events) 'Teatro Palermo Puente Alto' → Palermo Teatro Bar (id=11)")
        stats["merged"].append(("Teatro Palermo Puente Alto", "Palermo Teatro Bar"))
        stats["deleted_venues"].append("Teatro Palermo Puente Alto")
    else:
        print("  SKIP: 'Teatro Palermo Puente Alto' not in DB (already resolved)")

    # 2b: Secreto (id=375) → rename to "Bar Jardín Secreto"
    cur.execute("SELECT id, name FROM venues WHERE id=375")
    row = cur.fetchone()
    if row:
        if not DRY_RUN:
            cur.execute(
                "UPDATE venues SET name=%s, venue_type='Bar' WHERE id=375",
                ("Bar Jardín Secreto",),
            )
            conn.commit()
        print("  RENAME id=375 'Secreto' → 'Bar Jardín Secreto' (type=Bar)")
        stats["renamed"].append((375, "Secreto", "Bar Jardín Secreto"))
    else:
        print("  SKIP: id=375 Secreto not found")

    # ══════════════════════════════════════════════════════════════════════════
    # PART 3 — RENAMES + TYPE FIXES
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 68)
    print("PART 3 — RENAMES + TYPE FIXES")
    print("=" * 68)

    # Format: (id, new_name_or_None, new_type_or_None)
    # None = keep current value
    CHANGES = [
        (428, None,                        "Club"),         # Casona Hilda Parra
        (367, None,                        "Bar"),          # California Cantina
        (359, None,                        None),           # Cementerio General — keep type
        (404, None,                        "Club"),         # Centro De Eventos Manhattan
        (394, None,                        "Club"),         # Club Berlín
        (326, None,                        "Bar"),          # Estudio oculto
        (466, None,                        None),           # Metropolitan Santiago — keep type
        (405, "Sala Crearock",             None),           # Sala Audiovisual Crearock
        (430, "Teatro Sala Tessier",       "Teatro"),       # Sala Tessier
        (448, None,                        "Bar"),          # Sambolio
        (344, None,                        "Club"),         # Spot Freedom
        (43,  None,                        None),           # Teatro Alicia — keep type
        (420, None,                        None),           # Teatro Nacional Chileno — keep type
        (438, None,                        "Teatro"),       # Teatro Sidarte
        (237, "Teatro Teletón",            None),           # Teatro Teletón - Santiago Centro
        (292, None,                        "Arena"),        # Arena Recoleta
        (371, None,                        "Bar"),          # Bendito Black
        (142, None,                        None),           # Jardín Japonés — keep type
        (73,  None,                        None),           # Claro Arena — keep type
        (34,  None,                        None),           # Club Ambar — keep type
        (10,  None,                        None),           # Ambar Restobar — keep type
        (30,  None,                        None),           # Cajacústica — keep type
        (343, None,                        "Club"),         # Sala Pandora
        (467, None,                        None),           # Hotel Sheraton — keep type
        (358, None,                        "Club"),         # Cabaret Piraña
        (468, None,                        "Arena"),        # Cenco Florida
        (395, "Círculo Español",           "Club"),         # Circulo Español (fix accent + type)
        (268, None,                        "Club"),         # Club Ceira
        (431, None,                        "Club"),         # Club de la Unión
        (450, "Club de Teatro",            None),           # Club de Teatro, sala Pedro Orthous
        (373, None,                        "Club"),         # Club Orixas
        (392, None,                        "Club"),         # Clubroom
        (325, None,                        None),           # Ludum Bar — keep type
        (338, None,                        None),           # Tom House Cruising Bar — keep type
        (272, None,                        "Teatro"),       # Teatro Municipal de San Miguel
        (421, None,                        None),           # Teatro Lospleimovil — keep type
        (291, None,                        "Bar"),          # Rockstar Karaoke Stage
        (339, None,                        "Parque"),       # Pueblito De Artesanos De Pirque
        (148, None,                        None),           # Salto de Apoquindo — keep type
        (147, None,                        None),           # Parque Natural Aguas de Ramón — keep
        (328, None,                        "Sala de Concierto"),  # Sala Los Leones
        (178, None,                        None),           # Templo Bahai — keep type
        (22,  None,                        None),           # Honesto Mike Barrio Lastarria — keep
        (23,  None,                        None),           # Honesto Mike Providencia — keep
        (21,  None,                        None),           # Honesto Mike Vitacura — keep
    ]

    for vid, new_name, new_type in CHANGES:
        if new_name is None and new_type is None:
            continue  # nothing to change in DB
        parts = []
        vals = []
        if new_name:
            parts.append("name=%s")
            vals.append(new_name)
        if new_type:
            parts.append("venue_type=%s")
            vals.append(new_type)
        vals.append(vid)
        sql = f"UPDATE venues SET {', '.join(parts)} WHERE id=%s"
        if not DRY_RUN:
            cur.execute(sql, vals)

        tag = []
        if new_name:
            tag.append(f"name→{new_name!r}")
            stats["renamed"].append((vid, "?", new_name))
        if new_type:
            tag.append(f"type→{new_type!r}")
            stats["type_fixed"].append(vid)
        print(f"  UPDATE id={vid:4d}  {', '.join(tag)}")

    if not DRY_RUN:
        conn.commit()

    # ══════════════════════════════════════════════════════════════════════════
    # PART 3 + 2b — IMAGE UPLOADS
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 68)
    print("PART 3 — IMAGE UPLOADS")
    print("=" * 68)

    # (venue_id, cover_filename, profile_filename, note)
    # None = no file for that slot; use actual filenames from disk
    IMG_UPLOADS: list[tuple] = [
        # (vid,  cover_file,                       profile_file,                  note)
        (375,  "jardinsecreto-cover.jpg",          "jardinsecreto-profile.jpg",   "Bar Jardín Secreto"),
        (428,  "hildaparra-cover.jpg",             "hildaparra-profile.jpg",      "Casona Hilda Parra"),
        (367,  "californiacantina-cover.jpg",      "californiacantina-profile.jpg","California Cantina"),
        (359,  "cementeriogeneral-cover.jpg",      "cementeriogeneral-profile.jpg","Cementerio General"),
        (404,  "manhattan-cover.png",              "manhattan-profile.jpg",       "Manhattan"),
        (394,  "clubberlin-cover.webp",            "clubberlin-profile.jpg",      "Club Berlín"),
        (326,  "oculto-cover.jpg",                 "oculto-profile.jpg",          "Estudio oculto"),
        (466,  "metropolitanstgo-cover.jpg",       "metropolitanstgo-profile.jpg","Metropolitan Santiago"),
        (405,  "crearock-cover.jpeg",              "crearock-profile.jpg",        "Sala Crearock"),
        (430,  "tessier-cover.jpg",                "tessier-profile.jpg",         "Teatro Sala Tessier"),
        (448,  "sambolio-cover.jpeg",              "sambolio-profile.jpg",        "Sambolio"),
        (344,  "spotfreedom-cover.jpg",            "spotfreedom-profile.jpg",     "Spot Freedom"),
        (43,   "teatroalicia-cover.jpg",           "teatroalicia-profile.jpg",    "Teatro Alicia"),
        (420,  "teatrochileno-cover.jpg",          "teatrochileno-profile.jpg",   "Teatro Nacional Chileno"),
        (438,  "sidarte-cover.jpg",                "sidarte-profile.jpg",         "Teatro Sidarte"),
        (237,  "teleton-cover.jpg",                "teleton-profile.jpg",         "Teatro Teletón"),
        (292,  "arenarecoleta-cover.jpg",          "arenarecoleta-profile.jpg",   "Arena Recoleta"),
        (371,  "benditoblack-cover.jpeg",          "benditoblack-profile.jpg",    "Bendito Black"),
        (142,  "jardinjapones-cover.jpg",          "jardinjapones-profile.jpg",   "Jardín Japonés"),
        (73,   "claro-cover.png",                  "claro-profile.png",           "Claro Arena"),
        (34,   "clubambar-cover.jpg",              "clubambar-profile.jpg",       "Club Ambar"),
        (10,   "ambarrestobar-cover.png",          "ambarrestobar-profile.jpg",   "Ambar Restobar"),
        (30,   "cajacustica-cover.jpg",            "cajacustica-profile.jpg",     "Cajacústica"),
        (343,  "salapandora-cover.jpg",            "salapandora-profile.jpg",     "Sala Pandora"),
        (467,  "sheraton-cover.avif",              "sheraton-profile.jpg",        "Hotel Sheraton"),
        (358,  "cabaretpiraña-cover.jpeg",         "cabaretpiraña-profile.jpg",   "Cabaret Piraña"),
        (468,  "cencoflorida-cover.jpg",           "cencoflorida-profile.jpg",    "Cenco Florida"),
        (395,  "circuloespañol-cover.webp",        "circuloespañol-profile.jpg",  "Círculo Español"),
        (268,  "clubceira-cover.png",              "clubceira-profile.jpg",       "Club Ceira"),
        (431,  None,                               "clubunion-profile.jpg",       "Club de la Unión (no cover)"),
        (450,  "clubdeteatro-cover.webp",          "clubdeteatro-profile",        "Club de Teatro"),
        (373,  "cluborixas-cover.jpg",             "cluborixas-profile.jpg",      "Club Orixas"),
        (392,  "clubroom-cover.png",               "clubroom-profile.png",        "Clubroom"),
        (325,  "ludumbar-cover.avif",              "ludumbar-profile.jpg",        "Ludum Bar"),
        (338,  "tomhouse-cover.jpg",               "tomhouse-profile.jpg",        "Tom House Cruising Bar"),
        (272,  "teatrosanmiguel-cover.jpg",        "teatrosanmiguel-profile.jpg", "Teatro Mun. San Miguel"),
        (421,  "teatrolospleimovil-cover.jpg",     "teatrolospleimovil-profile.jpg","Teatro Lospleimovil"),
        (291,  "rockstarstage-cover.jpeg",         "rockstarstage-profile.jpg",   "Rockstar Karaoke Stage"),
        (339,  "pueblitopirque-cover.jpg",         "pueblitopirque-profile.jpg",  "Pueblito de Artesanos Pirque"),
        (328,  "salalosleones-cover.jpg",          "salalosleones-profile.jpg",   "Sala Los Leones"),
        (178,  "bahai-cover.jpg",                  "bahai-profile.jpg",           "Templo Bahai"),
        # Shared: Salto + Aguas de Ramón share parquecordillera-profile.jpg
        (148,  "saltodeapoquindo-cover.jpg",       "parquecordillera-profile.jpg","Salto de Apoquindo"),
        (147,  "aguasderamon-cover.png",           "parquecordillera-profile.jpg","Aguas de Ramón"),
        # Shared: 3 Honesto Mike venues
        (22,   "honesto-cover.png",                "honesto-profile.png",         "Honesto Mike Barrio Lastarria"),
        (23,   "honesto-cover.png",                "honesto-profile.png",         "Honesto Mike Providencia"),
        (21,   "honesto-cover.png",                "honesto-profile.png",         "Honesto Mike Vitacura"),
    ]

    # Cache: public_id → uploaded URL (avoid re-uploading shared files)
    url_cache: dict[str, str] = {}

    def upload_and_cache(filepath: str, public_id: str) -> str:
        if public_id in url_cache:
            return url_cache[public_id]
        url = upload(filepath, public_id)
        url_cache[public_id] = url
        return url

    def slug_from_filename(fname: str) -> str:
        """Extract public_id slug from filename (strip extension)."""
        return os.path.splitext(fname)[0]

    for vid, cover_file, profile_file, note in IMG_UPLOADS:
        cover_url: str | None = None
        profile_url: str | None = None

        for slot, fname in (("cover", cover_file), ("profile", profile_file)):
            if fname is None:
                continue
            fpath = asset(fname)
            if fpath is None:
                msg = f"  MISSING  id={vid:4d} {slot} {fname!r} — {note}"
                print(msg)
                stats["images_missing"].append(f"id={vid} {slot}:{fname}")
                continue

            slug = slug_from_filename(fname)
            public_id = f"eventify/venues/{slug}"
            try:
                if DRY_RUN:
                    url = f"<dry-run:{public_id}>"
                    print(f"  [DRY] id={vid:4d} {slot} → {public_id}  ({note})")
                else:
                    url = upload_and_cache(fpath, public_id)
                    print(f"  OK  id={vid:4d} {slot} → {url[:70]}  ({note})")
                if slot == "cover":
                    cover_url = url
                else:
                    profile_url = url
                stats["images_ok"].append(f"id={vid} {slot}")
            except Exception as exc:
                print(f"  ERROR id={vid:4d} {slot}: {exc}")
                stats["images_error"].append(f"id={vid} {slot}:{fname}: {exc}")

        # DB update
        if not DRY_RUN:
            if cover_url and profile_url:
                cur.execute(
                    "UPDATE venues SET cover_image_url=%s, profile_image_url=%s WHERE id=%s",
                    (cover_url, profile_url, vid),
                )
            elif cover_url:
                cur.execute("UPDATE venues SET cover_image_url=%s WHERE id=%s", (cover_url, vid))
            elif profile_url:
                cur.execute("UPDATE venues SET profile_image_url=%s WHERE id=%s", (profile_url, vid))
            conn.commit()

    # ══════════════════════════════════════════════════════════════════════════
    # PART 4 — CINEMA CHAINS (shared images)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 68)
    print("PART 4 — CINEMA CHAINS (shared images)")
    print("=" * 68)

    # Fetch all cinema chains
    cur.execute("""
        SELECT id, name FROM venues
        WHERE name ILIKE '%cinépolis%' OR name ILIKE '%cinepolis%'
           OR name ILIKE '%cinemark%' OR name ILIKE '%cineplanet%'
        ORDER BY name
    """)
    cinema_venues = cur.fetchall()

    CHAIN_FILES = {
        "cinepolis":  ("cinepolis-cover.png",  "cinepolis-profile.png"),
        "cinemark":   ("cinemark-cover.png",   "cinemark-profile.png"),
        "cineplanet": ("cineplanet-cover.png", "cineplanet-profile.png"),
    }

    chain_urls: dict[str, tuple[str | None, str | None]] = {}
    for chain, (cover_file, profile_file) in CHAIN_FILES.items():
        c_path = asset(cover_file)
        p_path = asset(profile_file)
        c_url: str | None = None
        p_url: str | None = None
        if c_path:
            if DRY_RUN:
                c_url = f"<dry-run:eventify/venues/{chain}-cover>"
            else:
                c_url = upload_and_cache(c_path, f"eventify/venues/{chain}-cover")
            print(f"  UPLOADED {cover_file} for chain '{chain}'")
        else:
            print(f"  MISSING  {cover_file}  ⚠️")
        if p_path:
            if DRY_RUN:
                p_url = f"<dry-run:eventify/venues/{chain}-profile>"
            else:
                p_url = upload_and_cache(p_path, f"eventify/venues/{chain}-profile")
            print(f"  UPLOADED {profile_file} for chain '{chain}'")
        else:
            print(f"  MISSING  {profile_file}  ⚠️")
        chain_urls[chain] = (c_url, p_url)

    for row in cinema_venues:
        vid = row["id"]
        vname = row["name"].lower()
        if "cinépolis" in vname or "cinepolis" in vname:
            chain = "cinepolis"
        elif "cinemark" in vname:
            chain = "cinemark"
        elif "cineplanet" in vname:
            chain = "cineplanet"
        else:
            continue
        c_url, p_url = chain_urls.get(chain, (None, None))
        if not DRY_RUN and (c_url or p_url):
            if c_url and p_url:
                cur.execute(
                    "UPDATE venues SET cover_image_url=%s, profile_image_url=%s WHERE id=%s",
                    (c_url, p_url, vid),
                )
            elif c_url:
                cur.execute("UPDATE venues SET cover_image_url=%s WHERE id=%s", (c_url, vid))
            elif p_url:
                cur.execute("UPDATE venues SET profile_image_url=%s WHERE id=%s", (p_url, vid))
        print(f"  {'[DRY] ' if DRY_RUN else ''}id={vid:4d}  chain={chain}  {row['name']!r}")
        stats["images_ok"].append(f"id={vid} cinema-chain={chain}")

    if not DRY_RUN:
        conn.commit()

    # ══════════════════════════════════════════════════════════════════════════
    # PART 5 — FINAL REPORT
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 68)
    print("PART 5 — FINAL REPORT")
    print("=" * 68)
    if DRY_RUN:
        print("  *** DRY RUN — no DB changes written ***\n")

    print(f"  Venues deleted:         {len(stats['deleted_venues'])}")
    for n in stats["deleted_venues"]:
        print(f"    - {n}")

    print(f"\n  Venues merged:          {len(stats['merged'])}")
    for src, dst in stats["merged"]:
        print(f"    {src!r} → {dst!r}")

    print(f"\n  Venues renamed:         {len(stats['renamed'])}")
    for vid, old, new in stats["renamed"]:
        print(f"    id={vid} {old!r} → {new!r}")

    print(f"\n  Type corrections:       {len(stats['type_fixed'])}")
    print(f"  Images uploaded/set:    {len(stats['images_ok'])}")
    print(f"  Images missing:         {len(stats['images_missing'])}")
    if stats["images_missing"]:
        for m in stats["images_missing"]:
            print(f"    ⚠️  {m}")
    print(f"  Image errors:           {len(stats['images_error'])}")
    if stats["images_error"]:
        for e in stats["images_error"]:
            print(f"    ❌ {e}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
