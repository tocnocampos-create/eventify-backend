"""Event classifier.

Uses the same keyword rules already present in
scripts/populate_keywords.py.  Given an event dict with name,
description, venue_name, and (optionally) venue_type, assigns:

    category    – one of: Música, Teatro, Comedia, Arte, Cine, Familia, Vida Nocturna
    type        – subtype string (Jazz, Rock, Electrónica, …)
    keywords    – list[str] built from every matching rule
    kids_friendly – True when Teatro_Familiar keywords fire
"""
from __future__ import annotations

import re
from typing import Any

# ── Keyword rules ─────────────────────────────────────────────────────────────
# v3 changes vs v2:
#  - _match_rules now uses word-boundary matching for single-word keywords,
#    preventing "arte" from matching "cuarteto"/"Martes", "obra" from matching
#    "obras", "escena" from matching "escenarios", "musical" from "musicales".
#  - Teatro_Drama: removed "musical" (adjective in music descriptions) and
#                  "escena" (→ "escena nacional", "escenarios")
#  - Teatro_Familiar: removed "magia" (→ "la magia de X"); added "show de magia"
#  - Comedia: removed "improvisación" (→ jazz improvisation descriptions)
#  - Cine: removed "proyección" (→ "proyección internacional" = reach/scope);
#          added "proyección de cine"
#  - Museo: removed "historia" (→ "mi historia musical", event story descriptions)
#  - Feria: removed standalone "mercado" (→ venue names like "Mercado París
#           Londres"); kept only compound forms

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
        "minimal", "boliche", "disco", "after hours",
        "trap", "perreo",
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
        "vida nocturna", "open bar", "VIP",
        "4 pistas de baile", "pista de baile",
    ],
    "Teatro_Familiar": [
        "familiar", "infantil", "niños", "kids",
        "familia", "teatro infantil", "circo", "títeres", "marionetas",
        "show de magia", "ilusionismo", "show infantil", "todas las edades",
        "apto para niños", "show familiar", "para toda la familia",
        "espectáculo infantil",
    ],
    "Teatro_Drama": [
        "obra de teatro", "obra teatral", "obra",
        "dramaturgia", "actuación", "puesta en escena",
        "drama", "tragicomedia", "monólogo", "danza", "ballet",
    ],
    "Comedia": [
        "comedia", "stand up", "stand-up", "humor",
        "comediante", "show de humor", "comedia en vivo",
        "improvisación", "impro", "sketch", "comedy",
        "open mic", "humorada", "cómico", "cómica",
    ],
    "Arte": [
        "arte", "exposición", "galería", "museo", "cultura",
        "instalación", "arte contemporáneo", "pintura", "escultura",
        "fotografía", "arte urbano", "muralismo", "vernissage",
        "inauguración",
    ],
    "Cine": [
        "cine", "película", "film", "cineclube",
        "ciclo de cine", "cortometraje", "documental", "estreno",
        "cine arte", "cine mudo", "proyección de cine", "sesión de cine",
    ],
    "Sunset": [
        "sunset", "atardecer", "happy hour", "after office",
        "cóctel", "terraza", "rooftop", "vista panorámica", "sundowner",
    ],
    "Feria": [
        "feria", "mercado de pulgas", "feria artesanal",
        "mercado artesanal", "feria de diseño", "bazar",
        "feria gastronómica", "food market",
    ],
    "Aire_Libre": [
        "aire libre", "outdoor", "parque", "plaza",
        "al fresco", "jardín",
        "anfiteatro", "exterior", "naturaleza",
    ],
    "Barrios": [
        "barrio italia", "lastarria", "bellavista",
        "brasil", "yungay", "concha y toro", "barrio matta",
        "barrio franklin", "patronato", "paris-londres",
        "patrimonio", "ruta cultural",
    ],
    "City_Tour": [
        "city tour", "patrimonio", "turismo", "visita guiada",
        "centro histórico", "santiago histórico", "ruta patrimonial",
        "cerro santa lucía", "plaza de armas", "la moneda",
    ],
    "Museo": [
        "museo", "colección", "exposición permanente",
        "arqueología", "arte precolombino", "memorial",
        "archivo histórico",
    ],
}

# Pre-built lowercase lookup — each keyword stored as (text, is_single_word)
_RULES_LOWER: list[tuple[str, list[tuple[str, bool]]]] = [
    (
        rule,
        [(kw.lower(), " " not in kw.lower()) for kw in kws],
    )
    for rule, kws in KEYWORD_RULES.items()
]

# Compiled word-boundary patterns cache
_WB_CACHE: dict[str, re.Pattern] = {}


def _wb(kw: str) -> re.Pattern:
    """Return a compiled word-boundary pattern for a single-word keyword."""
    if kw not in _WB_CACHE:
        _WB_CACHE[kw] = re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE | re.UNICODE)
    return _WB_CACHE[kw]


# ── Venue-type → rule (hard signal, always applied) ───────────────────────────
# "museo" kept — venues typed as Museo strongly suggest art/museum content.
# "arena", "club", "bar" moved to VENUE_TYPE_FALLBACK (applied only when
# keyword classification yields no category), preventing venue type from
# overriding explicit music keyword signals.
VENUE_TYPE_RULES: dict[str, str] = {
    "museo": "Museo",
}

# ── Venue-type → category (soft fallback, only when keywords yield nothing) ───
VENUE_TYPE_FALLBACK: dict[str, tuple[str, str | None]] = {
    "club":              ("Vida Nocturna", "Vida Nocturna"),
    "bar":               ("Vida Nocturna", "Vida Nocturna"),
    "arena":             ("Música",        None),
    "sala de concierto": ("Música",        None),
}

# ── Known venue name → locked category ────────────────────────────────────────
# Substring match (lowercase) on venue_name. Applied as a hard override.
LOCKED_VENUE_CATEGORIES: dict[str, str] = {
    "teatro municipal":         "Teatro",
    "movistar arena":           "Música",
    "estadio nacional":         "Música",
    "estadio bicentenario":     "Música",
    "estadio monumental":       "Música",
    "estadio san carlos":       "Música",
}

# ── Category / type mapping ───────────────────────────────────────────────────
_RULE_PRIORITY: list[str] = [
    "Cine",
    "Jazz",           # before Comedia: "jazz + improvisación" → Jazz wins over improv-comedy
    "Comedia",
    "Teatro_Familiar",
    "Teatro_Drama",
    "Rock",
    "Pop",
    "Indie",
    "Folclore",
    "Latina",
    "Electrónica",
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
    "Cine":            "Cine",
    "Comedia":         "Comedia",
    "Teatro_Familiar": "Familia",
    "Teatro_Drama":    "Teatro",
    "Jazz":            "Música",
    "Rock":            "Música",
    "Pop":             "Música",
    "Indie":           "Música",
    "Electrónica":     "Vida Nocturna",
    "Folclore":        "Música",
    "Latina":          "Música",
    "Arte":            "Arte",
    "Museo":           "Arte",
    "Vida Nocturna":   "Vida Nocturna",
    "Sunset":          "Vida Nocturna",
    "Feria":           "Arte",
    "Aire_Libre":      None,
    "Barrios":         None,
    "City_Tour":       None,
}

_RULE_TO_TYPE: dict[str, str] = {
    "Cine":            "Cine",
    "Comedia":         "Stand Up",
    "Teatro_Familiar": "Familiar",
    "Teatro_Drama":    "Drama",
    "Jazz":            "Jazz",
    "Rock":            "Rock",
    "Pop":             "Pop",
    "Indie":           "Indie",
    "Electrónica":     "Electrónica",
    "Folclore":        "Folclore",
    "Latina":          "Latina",
    "Arte":            "Exposición",
    "Museo":           "Exposición",
    "Vida Nocturna":   "Vida Nocturna",
    "Sunset":          "Sunset",
    "Feria":           "Feria",
    "Aire_Libre":      "Aire Libre",
    "Barrios":         "Barrios",
    "City_Tour":       "City Tour",
}


# ── Public API ────────────────────────────────────────────────────────────────

def _match_rules(name: str, description: str, venue_type: str) -> set[str]:
    """Return the set of rule names matched by the text fields.

    Single-word keywords use word-boundary matching to prevent false positives
    like "arte" matching "cuarteto" or "Martes", "obra" matching "obras", etc.
    Multi-word keywords use plain substring matching.
    """
    combined = f"{name} {description}".lower()
    matched: set[str] = set()

    for rule_name, kw_pairs in _RULES_LOWER:
        for kw, is_single_word in kw_pairs:
            if is_single_word:
                if _wb(kw).search(combined):
                    matched.add(rule_name)
                    break
            else:
                if kw in combined:
                    matched.add(rule_name)
                    break

    # Hard venue-type rules (only "museo" now)
    venue_rule = VENUE_TYPE_RULES.get(venue_type.lower())
    if venue_rule:
        matched.add(venue_rule)

    return matched


def build_keywords(
    name: str = "",
    description: str = "",
    venue_type: str = "",
) -> list[str]:
    """Return a sorted, deduplicated keyword list."""
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

    Classification priority (highest → lowest):
      1. _locked_category sentinel from scraper URL hint
      2. LOCKED_VENUE_CATEGORIES match on venue_name
      3. Keyword-based rules (KEYWORD_RULES via _RULE_PRIORITY)
      4. VENUE_TYPE_FALLBACK (only when keywords yield no category)
      5. _category_hint from scraper

    Args:
        event: dict with at least "name". Optional: "description",
               "venue_type", "venue_name".
    """
    name = event.get("name") or ""
    description = event.get("description") or ""
    venue_type = event.get("venue_type") or ""
    venue_name = event.get("venue_name") or ""

    matched = _match_rules(name, description, venue_type)

    # keywords — always recompute
    kws: set[str] = set()
    for rule in matched:
        kws.update(KEYWORD_RULES[rule])
    event["keywords"] = sorted(kws)

    # kids_friendly
    if "Teatro_Familiar" in matched:
        event["kids_friendly"] = True
    elif not event.get("kids_friendly"):
        event["kids_friendly"] = False

    # Pop sentinels
    locked_category: str | None = event.pop("_locked_category", None)
    category_hint: str | None = event.pop("_category_hint", None)

    # --- Category resolution ---
    category: str | None = event.get("category") or None
    etype: str | None = event.get("type") or None

    # 1. Scraper URL lock (highest priority)
    if locked_category:
        event["category"] = locked_category
        category = locked_category

    # 2. Known venue name lock (hard override when no scraper lock)
    if not locked_category and venue_name:
        vn_lower = venue_name.lower()
        for substr, locked_cat in LOCKED_VENUE_CATEGORIES.items():
            if substr in vn_lower:
                category = locked_cat
                break

    # 3. Keyword-based classification
    for rule in _RULE_PRIORITY:
        if rule not in matched:
            continue
        if locked_category:
            rule_cat = _RULE_TO_CATEGORY.get(rule)
            if rule_cat != locked_category:
                continue
        if category is None:
            category = _RULE_TO_CATEGORY.get(rule)
        if etype is None:
            etype = _RULE_TO_TYPE.get(rule)
        if category is not None and etype is not None:
            break

    # 4. Venue-type fallback — only when keywords produced no category yet
    if category is None and not locked_category:
        fallback = VENUE_TYPE_FALLBACK.get(venue_type.lower())
        if fallback:
            category = fallback[0]
            if etype is None and fallback[1] is not None:
                etype = fallback[1]

    # 5. Scraper category hint (last resort)
    if category is None and category_hint and not locked_category:
        category = category_hint

    if category is not None:
        event["category"] = category
    if etype is not None:
        event["type"] = etype

    return event
