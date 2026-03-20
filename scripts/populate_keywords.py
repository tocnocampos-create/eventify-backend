"""
Standalone script: populate keywords for all events based on their name, type, and category.

Usage (from repo root with Docker running):
    docker-compose exec api-dev python scripts/populate_keywords.py

Or with a direct DB URL:
    DATABASE_URL=postgresql://eventify:eventify@localhost:5432/eventify python scripts/populate_keywords.py
"""
import os
import sys

# Allow running from repo root or scripts/ directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# ── Keyword rules ──────────────────────────────────────────────────────────────

KEYWORD_RULES = {
  "Folclore": ["folclore","folklore","cueca","tonada",
    "banda chilena","música chilena","música nacional",
    "artista chileno","trova","nueva canción chilena",
    "cumbia chilena","cumbia","chicha","tropical chileno",
    "latin folk"],
  "Latina": ["latina","tropical","cumbia","salsa","merengue",
    "bachata","reggaeton","música latina","ritmos latinos"],
  "Electrónica": ["electrónica","DJ","dj set","techno","house",
    "minimal","after","club","boliche","antro","disco","fiesta",
    "nocturno","after hours","trap","perreo"],
  "Jazz": ["jazz","blues","swing","bossa nova","jazz fusión",
    "big band","jazz en vivo","bebop","soul jazz","latin jazz"],
  "Rock": ["rock","rock en vivo","rock nacional","punk","metal",
    "heavy metal","grunge","indie rock","post rock","hard rock",
    "garage rock","alternativo"],
  "Pop": ["pop","música en vivo","pop rock","pop latino"],
  "Indie": ["indie","alternativo","indie rock","post rock",
    "experimental"],
  "Vida Nocturna": ["vida nocturna","noche","nocturno","after",
    "club","boliche","DJ","fiesta","open bar","VIP"],
  "Teatro_Familiar": ["familiar","infantil","niños","kids",
    "familia","teatro infantil","circo","títeres","marionetas",
    "magia","show infantil","todas las edades",
    "apto para niños"],
  "Teatro_Drama": ["teatro","obra","dramaturgia","escena",
    "actuación","puesta en escena","drama","tragicomedia",
    "monólogo","performance"],
  "Comedia": ["comedia","stand up","stand-up","humor",
    "comediante","show de humor","comedia en vivo",
    "improvisación","impro","sketch"],
  "Arte": ["arte","exposición","galería","museo","cultura",
    "instalación","arte contemporáneo","pintura","escultura",
    "fotografía","arte urbano","muralismo","vernissage",
    "inauguración"],
  "Cine": ["cine","película","film","proyección","cineclube",
    "ciclo de cine","cortometraje","documental","estreno",
    "cine arte","cine mudo"],
  "Sunset": ["sunset","atardecer","happy hour","after office",
    "cóctel","terraza","rooftop","vista panorámica",
    "sundowner"],
  "Feria": ["feria","mercado","feria artesanal",
    "mercado de pulgas","feria de diseño","bazar",
    "feria gastronómica","food market"],
  "Aire_Libre": ["aire libre","outdoor","parque","plaza",
    "festival","al fresco","jardín","terraza","rooftop",
    "anfiteatro","exterior","naturaleza"],
  "Barrios": ["barrio italia","lastarria","bellavista",
    "brasil","yungay","concha y toro","barrio matta",
    "barrio franklin","patronato","paris-londres",
    "patrimonio","ruta cultural"],
  "City_Tour": ["city tour","tour","patrimonio","historia",
    "turismo","visita guiada","centro histórico",
    "santiago histórico","ruta patrimonial",
    "cerro santa lucía","plaza de armas","la moneda"],
  "Museo": ["museo","colección","exposición permanente",
    "arqueología","historia","arte precolombino","memorial",
    "archivo histórico"]
}

# Pre-build lowercase lookup for name matching:
# list of (rule_name, [lowercase_kw, ...])
_RULES_LOWER = [
    (rule_name, [kw.lower() for kw in kws])
    for rule_name, kws in KEYWORD_RULES.items()
]

# Venue-type → rule name mapping
VENUE_TYPE_RULES = {
    "club": "Vida Nocturna",
    "bar":  "Vida Nocturna",
    "museo": "Museo",
    "arena": "Aire_Libre",
}


def build_keywords(event, venue_type=None):
    """Return a sorted, deduplicated list of keyword strings for an event row."""
    name     = (event.get('name')     or '').lower()
    etype    = (event.get('type')     or '').lower()
    category = (event.get('category') or '').lower()
    vtype    = (venue_type or '').lower()

    matched_rules = set()

    # 1. Match event.type against rule keys (case-insensitive)
    for rule_name, _ in _RULES_LOWER:
        if etype == rule_name.lower():
            matched_rules.add(rule_name)

    # 2. Match event.category against rule keys (case-insensitive)
    for rule_name, _ in _RULES_LOWER:
        if category == rule_name.lower():
            matched_rules.add(rule_name)

    # 3. Match event.name against any keyword in any rule
    for rule_name, kws_lower in _RULES_LOWER:
        if any(kw in name for kw in kws_lower):
            matched_rules.add(rule_name)

    # 4. Venue-type rules
    venue_rule = VENUE_TYPE_RULES.get(vtype)
    if venue_rule:
        matched_rules.add(venue_rule)

    # 5. Combine all keywords from matched rules, deduplicate
    kws = set()
    for rule_name in matched_rules:
        kws.update(KEYWORD_RULES[rule_name])

    return sorted(kws)


# ── DB connection ──────────────────────────────────────────────────────────────

def get_engine():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        db_url = 'postgresql://eventify:eventify@localhost:5432/eventify'
    return create_engine(db_url)


def main():
    engine = get_engine()
    updated = 0
    skipped = 0
    samples = []

    with Session(engine) as session:
        rows = session.execute(text("""
            SELECT
                e.id,
                e.name,
                e.type,
                e.category,
                e.keywords,
                v.venue_type
            FROM events e
            LEFT JOIN venues v ON v.id = e.venue_id
        """)).mappings().all()

        print(f"Found {len(rows)} events to process.")

        for row in rows:
            existing = list(row['keywords'] or [])
            new_kws  = build_keywords(dict(row), venue_type=row['venue_type'])

            if new_kws == existing:
                skipped += 1
                continue

            session.execute(
                text("UPDATE events SET keywords = :kws WHERE id = :id"),
                {"kws": new_kws, "id": row['id']},
            )
            updated += 1

            if len(samples) < 5:
                samples.append({
                    "id":       row['id'],
                    "name":     row['name'],
                    "type":     row['type'],
                    "category": row['category'],
                    "keywords": new_kws,
                })

        session.commit()

    print(f"\nDone. Updated: {updated} | Already correct / skipped: {skipped}")

    if samples:
        print("\n── 5 sample events with new keywords ──────────────────────────")
        for s in samples:
            print(f"\n  [{s['id']}] {s['name']}")
            print(f"       type={s['type']}  category={s['category']}")
            print(f"       keywords: {s['keywords']}")

    # If fewer than 5 were updated, pull samples from DB anyway
    if len(samples) < 5:
        with Session(engine) as session:
            extra = session.execute(text("""
                SELECT id, name, type, category, keywords
                FROM events
                WHERE keywords IS NOT NULL AND cardinality(keywords) > 0
                LIMIT 5
            """)).mappings().all()
        if extra:
            print("\n── 5 sample events from DB ─────────────────────────────────")
            for r in extra:
                print(f"\n  [{r['id']}] {r['name']}")
                print(f"       type={r['type']}  category={r['category']}")
                print(f"       keywords: {list(r['keywords'])}")


if __name__ == '__main__':
    main()
