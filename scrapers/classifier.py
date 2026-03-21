"""Event classifier.

Uses the same keyword rules already present in
scripts/populate_keywords.py.  Given an event dict with name,
description, venue_name, and (optionally) venue_type, assigns:

    category    – one of: Música, Teatro, Comedia, Arte, Cine
    type        – subtype string (Jazz, Rock, Electrónica, …)
    keywords    – list[str] built from every matching rule
    kids_friendly – True when Teatro_Familiar keywords fire
"""
from __future__ import annotations

from typing import Any

# ── Keyword rules (kept identical to scripts/populate_keywords.py) ─────────

KEYWORD_RULES: dict[str, list[str]] = {
    "Folclore": [
        "folclore", "folklore", "cueca", "tonada",
        "banda chilena", "música chilena", "música nacional",
        "artista chileno", "trova", "nueva canción chilena",
        "cumbia chilena", "cumbia", "chicha", "tropical chileno",
        "latin folk",
    ],
    "Latina": [
        "latina", "tropical", "cumbia", "salsa", "merengue",
        "bachata", "reggaeton", "música latina", "ritmos latinos",
    ],
    "Electrónica": [
        "electrónica", "DJ", "dj set", "techno", "house",
        "minimal", "after", "club", "boliche", "antro", "disco", "fiesta",
        "nocturno", "after hours", "trap", "perreo",
    ],
    "Jazz": [
        "jazz", "blues", "swing", "bossa nova", "jazz fusión",
        "big band", "jazz en vivo", "bebop", "soul jazz", "latin jazz",
    ],
    "Rock": [
        "rock", "rock en vivo", "rock nacional", "punk", "metal",
        "heavy metal", "grunge", "indie rock", "post rock", "hard rock",
        "garage rock", "alternativo",
    ],
    "Pop": ["pop", "música en vivo", "pop rock", "pop latino"],
    "Indie": ["indie", "alternativo", "indie rock", "post rock", "experimental"],
    "Vida Nocturna": [
        "vida nocturna", "noche", "nocturno", "after",
        "club", "boliche", "DJ", "fiesta", "open bar", "VIP",
    ],
    "Teatro_Familiar": [
        "familiar", "infantil", "niños", "kids",
        "familia", "teatro infantil", "circo", "títeres", "marionetas",
        "magia", "show infantil", "todas las edades", "apto para niños",
    ],
    "Teatro_Drama": [
        "teatro", "obra", "dramaturgia", "escena",
        "actuación", "puesta en escena", "drama", "tragicomedia",
        "monólogo", "performance",
    ],
    "Comedia": [
        "comedia", "stand up", "stand-up", "humor",
        "comediante", "show de humor", "comedia en vivo",
        "improvisación", "impro", "sketch",
    ],
    "Arte": [
        "arte", "exposición", "galería", "museo", "cultura",
        "instalación", "arte contemporáneo", "pintura", "escultura",
        "fotografía", "arte urbano", "muralismo", "vernissage",
        "inauguración",
    ],
    "Cine": [
        "cine", "película", "film", "proyección", "cineclube",
        "ciclo de cine", "cortometraje", "documental", "estreno",
        "cine arte", "cine mudo",
    ],
    "Sunset": [
        "sunset", "atardecer", "happy hour", "after office",
        "cóctel", "terraza", "rooftop", "vista panorámica", "sundowner",
    ],
    "Feria": [
        "feria", "mercado", "feria artesanal",
        "mercado de pulgas", "feria de diseño", "bazar",
        "feria gastronómica", "food market",
    ],
    "Aire_Libre": [
        "aire libre", "outdoor", "parque", "plaza",
        "festival", "al fresco", "jardín", "terraza", "rooftop",
        "anfiteatro", "exterior", "naturaleza",
    ],
    "Barrios": [
        "barrio italia", "lastarria", "bellavista",
        "brasil", "yungay", "concha y toro", "barrio matta",
        "barrio franklin", "patronato", "paris-londres",
        "patrimonio", "ruta cultural",
    ],
    "City_Tour": [
        "city tour", "tour", "patrimonio", "historia",
        "turismo", "visita guiada", "centro histórico",
        "santiago histórico", "ruta patrimonial",
        "cerro santa lucía", "plaza de armas", "la moneda",
    ],
    "Museo": [
        "museo", "colección", "exposición permanente",
        "arqueología", "historia", "arte precolombino", "memorial",
        "archivo histórico",
    ],
}

# Pre-built lowercase lookup
_RULES_LOWER: list[tuple[str, list[str]]] = [
    (rule, [kw.lower() for kw in kws])
    for rule, kws in KEYWORD_RULES.items()
]

# Venue-type → rule mapping (same as populate_keywords.py)
VENUE_TYPE_RULES: dict[str, str] = {
    "club": "Vida Nocturna",
    "bar": "Vida Nocturna",
    "museo": "Museo",
    "arena": "Aire_Libre",
}

# ── Category / type mapping ───────────────────────────────────────────────────
# Priority order: more specific rules override generic ones.
# First rule that matches (in this order) wins for category / type.

_RULE_PRIORITY: list[str] = [
    "Cine",
    "Comedia",
    "Teatro_Familiar",
    "Teatro_Drama",
    "Jazz",
    "Rock",
    "Pop",
    "Indie",
    "Electrónica",
    "Folclore",
    "Latina",
    "Arte",
    "Museo",
    "Vida Nocturna",
    "Sunset",
    "Feria",
    "Aire_Libre",
    "Barrios",
    "City_Tour",
]

_RULE_TO_CATEGORY: dict[str, str] = {
    "Cine": "Cine",
    "Comedia": "Comedia",
    "Teatro_Familiar": "Teatro",
    "Teatro_Drama": "Teatro",
    "Jazz": "Música",
    "Rock": "Música",
    "Pop": "Música",
    "Indie": "Música",
    "Electrónica": "Música",
    "Folclore": "Música",
    "Latina": "Música",
    "Arte": "Arte",
    "Museo": "Arte",
    "Vida Nocturna": "Música",
    "Sunset": "Música",
    "Feria": "Arte",
    "Aire_Libre": None,
    "Barrios": None,
    "City_Tour": None,
}

_RULE_TO_TYPE: dict[str, str] = {
    "Cine": "Cine",
    "Comedia": "Stand Up",
    "Teatro_Familiar": "Familiar",
    "Teatro_Drama": "Drama",
    "Jazz": "Jazz",
    "Rock": "Rock",
    "Pop": "Pop",
    "Indie": "Indie",
    "Electrónica": "Electrónica",
    "Folclore": "Folclore",
    "Latina": "Latina",
    "Arte": "Exposición",
    "Museo": "Exposición",
    "Vida Nocturna": "Vida Nocturna",
    "Sunset": "Sunset",
    "Feria": "Feria",
    "Aire_Libre": "Aire Libre",
    "Barrios": "Barrios",
    "City_Tour": "City Tour",
}


# ── Public API ────────────────────────────────────────────────────────────────

def _match_rules(name: str, description: str, venue_type: str) -> set[str]:
    """Return the set of rule names matched by the text fields."""
    combined = f"{name} {description}".lower()
    matched: set[str] = set()

    for rule_name, kws_lower in _RULES_LOWER:
        if any(kw in combined for kw in kws_lower):
            matched.add(rule_name)

    venue_rule = VENUE_TYPE_RULES.get(venue_type.lower())
    if venue_rule:
        matched.add(venue_rule)

    return matched


def build_keywords(
    name: str = "",
    description: str = "",
    venue_type: str = "",
) -> list[str]:
    """Return a sorted, deduplicated keyword list (same logic as populate_keywords.py)."""
    matched = _match_rules(name, description, venue_type)
    kws: set[str] = set()
    for rule in matched:
        kws.update(KEYWORD_RULES[rule])
    return sorted(kws)


def classify(event: dict[str, Any]) -> dict[str, Any]:
    """Classify an event dict in-place and return it.

    Sets (or overwrites) event["category"], event["type"],
    event["keywords"], and event["kids_friendly"].
    Does NOT overwrite fields that already have a value unless
    the field is "keywords" (always recomputed).

    Args:
        event: dict with at least "name".  Optional: "description",
               "venue_type" (the resolved venue's type string).
    """
    name = event.get("name") or ""
    description = event.get("description") or ""
    venue_type = event.get("venue_type") or ""

    matched = _match_rules(name, description, venue_type)

    # keywords — always recompute
    kws: set[str] = set()
    for rule in matched:
        kws.update(KEYWORD_RULES[rule])
    event["keywords"] = sorted(kws)

    # kids_friendly
    event["kids_friendly"] = "Teatro_Familiar" in matched

    # category and type — pick first match in priority order
    category: str | None = event.get("category") or None
    etype: str | None = event.get("type") or None

    for rule in _RULE_PRIORITY:
        if rule in matched:
            if category is None:
                category = _RULE_TO_CATEGORY.get(rule)
            if etype is None:
                etype = _RULE_TO_TYPE.get(rule)
            if category is not None and etype is not None:
                break

    if category is not None:
        event["category"] = category
    if etype is not None:
        event["type"] = etype

    return event
