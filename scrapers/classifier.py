"""Event classifier.

Uses keyword rules to classify events.  Given an event dict with name,
description, venue_name, venue_type, source_url, and (optionally) time_start,
assigns:

    category     – one of the canonical categories
    type         – subtype string (Jazz, Rock, Electrónica, …)
    keywords     – list[str] built from every matching rule
    kids_friendly – True when Teatro_Familiar keywords fire

Canonical categories:
    Música, Teatro, Comedia, Arte, Cine, Familia, Vida Nocturna,
    Nacional, Ferias, Festivales, Al Aire Libre

Classification priority (highest → lowest):
  1. _locked_category sentinel from scraper URL hint
     (Cine lock cancelled at non-cinema venues — FIX 1)
  2. LOCKED_VENUE_CATEGORIES match on venue_name
  2b. SCD venue or portaldisc source → Nacional  (FIX 6)
  3. Keyword-based rules (KEYWORD_RULES via _RULE_PRIORITY)
  4. VENUE_TYPE_FALLBACK (only when keywords yield no category)
     (Museo moved here from hard VENUE_TYPE_RULES — FIX 2)
  5. _category_hint from scraper
     (Cine hint cancelled at non-cinema venues — FIX 1)

v4 changes vs v3:
  - FIX 1: Cine requires venue_type Cine/Cineteca or strong cinema keywords;
           Cine lock/_category_hint cancelled at non-cinema venues
  - FIX 2: "museo" moved from VENUE_TYPE_RULES (hard) to VENUE_TYPE_FALLBACK (soft)
           so music events at museo venues get Música when keywords match
  - FIX 3: CATEGORY_NORMALIZE maps garbage scraper categories to canonical ones
  - FIX 4: Electrónica → Música (not Vida Nocturna); exception: Club/Discoteca ≥22:00
  - FIX 5: "cultura" removed from Arte keywords (too broad, fires on unrelated events)
  - FIX 6: Nacional category; SCD venue name + portaldisc source trigger it;
           blocked at Cine/Museo venue types
  - FIX 7: Ferias → "Ferias" category (was Arte); Festivales new rule;
           Al Aire Libre → "Al Aire Libre" (was None); outdoor venue_type fallbacks added;
           Jazz + Sunset keywords expanded
"""
from __future__ import annotations

import re
from typing import Any

# ── Canonical categories ───────────────────────────────────────────────────────
CANONICAL_CATEGORIES: frozenset[str] = frozenset([
    "Música", "Teatro", "Comedia", "Arte", "Cine", "Familia",
    "Vida Nocturna", "Nacional", "Ferias", "Festivales", "Al Aire Libre",
])

# ── FIX 3: Garbage → canonical normalization map ──────────────────────────────
# Applied to _category_hint, _locked_category, and existing event["category"].
# None = reset — let keyword rules decide.
CATEGORY_NORMALIZE: dict[str, str | None] = {
    "Alternative Rock":                           "Música",
    "Festival de Metal":                          "Música",
    "Discoteca":                                  "Vida Nocturna",
    "Death Metal":                                "Música",
    "Bass Subgenres":                             "Música",
    "Drum & Bass":                                "Música",
    "Cumbia":                                     "Música",
    "80S Music":                                  "Música",
    "Concert":                                    "Música",
    "Concert Tours":                              "Música",
    "Conciertos y Festivales":                    "Música",
    "Entradas y Eventos":                         "Música",
    "Arte & Cultura":                             "Arte",
    "Humor / Stand Up Comedy":                    "Comedia",
    "Evento Familiar":                            "Familia",
    "Baile":                                      "Vida Nocturna",
    "Fiesta":                                     "Vida Nocturna",
    "Rock":                                       "Música",
    "Festival":                                   "Festivales",
    "Entretención":                               None,
    "Speed Dating":                               None,
    "Eventos Deportivos":                         None,
    "Atracciones/Tours y visitas turísticas":     None,
    "Encuentros":                                 None,
    "Evento Sportivo":                            None,
    "Cursos / Talleres":                          None,
}


def _normalize_category(cat: str | None) -> str | None:
    """Map a raw/garbage category to a canonical one.

    Returns the canonical value if already canonical, the mapped value if in
    CATEGORY_NORMALIZE, or None (unknown → let keyword rules decide).
    """
    if cat is None:
        return None
    if cat in CANONICAL_CATEGORIES:
        return cat
    return CATEGORY_NORMALIZE.get(cat, None)


# ── Keyword rules ─────────────────────────────────────────────────────────────
# v4 changes:
#  - Nacional (new): Chilean music identity keywords; higher priority than Rock/Folclore
#  - Arte: removed "cultura" (FIX 5)
#  - Jazz: added "soul", "funk" (FIX 7)
#  - Sunset: added "afterwork", "coffee party" (FIX 7)
#  - Cine: added "muestra de cine", "festival de cine", "cineclub" (FIX 1 strong keywords)
#  - Feria: added "feria del libro", "festival gastronómico", "gastro",
#           "beerfest", "comicon", "expocafé", "expo" (FIX 7)
#  - Festivales (new): specific large-festival signals only (FIX 7)
#  - Aire_Libre: added "picnic", "ciclovía", "ciclo recreo" (FIX 7)

KEYWORD_RULES: dict[str, list[str]] = {
    # ── FIX 6: Nacional — Chilean music identity ──────────────────────────────
    # Checked before Rock/Folclore in _RULE_PRIORITY so Chilean signals win.
    # Hard-blocked at Cine/Museo venue_types in classify().
    "Nacional": [
        "música chilena", "música nacional", "rock chileno",
        "banda chilena", "artista chileno", "artista nacional",
        "cantautor chileno", "cumbia chilena", "pop chileno",
        "nueva canción chilena", "tropical chileno",
        "folclore", "folklore", "cueca", "tonada", "chicha",
        "trova", "latin folk",
    ],
    # Folclore kept for keyword generation; Nacional handles the category
    "Folclore": [
        "folclore", "folklore", "cueca", "tonada",
        "nueva canción chilena", "cumbia chilena", "chicha",
        "tropical chileno", "latin folk", "trova",
    ],
    "Latina": [
        "latina", "tropical", "cumbia", "salsa", "merengue",
        "bachata", "reggaeton", "música latina", "ritmos latinos",
    ],
    # ── FIX 4: Electrónica maps to Música; Club/Discoteca ≥22:00 exception ───
    "Electrónica": [
        "electrónica", "DJ", "dj set", "techno", "house",
        "minimal", "boliche", "disco", "after hours",
        "trap", "perreo",
    ],
    # ── FIX 7: Jazz — soul + funk added ──────────────────────────────────────
    "Jazz": [
        "jazz", "blues", "swing", "bossa nova", "jazz fusión",
        "big band", "jazz en vivo", "bebop", "soul jazz", "latin jazz",
        "soul", "funk",
    ],
    "Rock": [
        "rock", "rock en vivo", "rock nacional", "punk", "metal",
        "heavy metal", "grunge", "indie rock", "post rock", "hard rock",
        "garage rock", "alternativo",
        "experience", "tributo", "tributo a", "the legend of", "homenaje a",
        "black dog",
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
        "comedia", "stand up", "stand-up", "stan up", "humor",
        "comediante", "show de humor", "comedia en vivo",
        "improvisación", "impro", "sketch", "comedy",
        "open mic", "humorada", "cómico", "cómica",
    ],
    # ── FIX 5: "cultura" removed (too broad) ─────────────────────────────────
    "Arte": [
        "arte", "exposición", "galería", "museo",
        "instalación", "arte contemporáneo", "pintura", "escultura",
        "fotografía", "arte urbano", "muralismo", "vernissage",
        "inauguración",
    ],
    # ── FIX 1: Cine — venue_type constraint enforced in classify() ────────────
    "Cine": [
        "cine", "película", "film", "cineclub", "cineclube",
        "ciclo de cine", "cortometraje", "documental", "estreno",
        "cine arte", "cine mudo", "proyección de cine", "sesión de cine",
        "muestra de cine", "festival de cine",
    ],
    # ── FIX 7: Sunset — afterwork + coffee party added ────────────────────────
    "Sunset": [
        "sunset", "atardecer", "happy hour", "after office", "afterwork",
        "cóctel", "terraza", "rooftop", "vista panorámica", "sundowner",
        "coffee party",
    ],
    # ── FIX 7: Feria — expanded keywords ─────────────────────────────────────
    "Feria": [
        "feria", "mercado de pulgas", "feria artesanal",
        "mercado artesanal", "feria de diseño", "bazar",
        "feria gastronómica", "food market", "feria del libro",
        "festival gastronómico", "gastro", "beerfest", "comicon",
        "expocafé", "expo",
    ],
    # ── FIX 7: Festivales — new rule (specific large-festival signals only) ───
    "Festivales": [
        "lollapalooza", "fauna primavera", "creamfields", "ultra chile",
        "tomorrowland", "lineup", "cartel de artistas",
    ],
    # ── FIX 7: Aire_Libre ────────────────────────────────────────────────────
    # "parque", "plaza", "jardín", "anfiteatro", "exterior", "naturaleza"
    # removed — too common in venue names (e.g. "Centro Parque", "Plaza Mayor")
    # causing false positives.  Outdoor park venues are handled by venue_type
    # fallback instead.  Only keep unambiguous outdoor-activity keywords here.
    "Aire_Libre": [
        "aire libre", "al fresco", "outdoor",
        "picnic", "ciclovía", "ciclo recreo",
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
# FIX 2: "museo" removed — moved to VENUE_TYPE_FALLBACK (soft).
# "cine"/"cineteca" kept as hard signals: all events at cinema venues are Cine
# by default (highest priority rule), preventing Teatro_Familiar or other rules
# from hijacking family films at cinemas.  The FIX 1 post-check still cancels
# Cine category when keywords fire at a non-cinema venue.
VENUE_TYPE_RULES: dict[str, str] = {
    "cine":      "Cine",
    "cineteca":  "Cine",
}

# ── Venue-type → category (soft fallback, only when keywords yield nothing) ───
# FIX 2: "museo" added here.
# FIX 7: outdoor venue types → Al Aire Libre.
VENUE_TYPE_FALLBACK: dict[str, tuple[str, str | None]] = {
    "museo":             ("Arte",          "Exposición"),
    "club":              ("Vida Nocturna", "Vida Nocturna"),
    "bar":               ("Vida Nocturna", "Vida Nocturna"),
    "arena":             ("Música",        None),
    "sala de concierto": ("Música",        None),
    # FIX 8: missing venue types that produced 162 NULL-category events
    "teatro":            ("Teatro",        None),
    "comedia":           ("Comedia",       "Stand Up"),
    "espacio cultural":  ("Música",        None),
    # FIX 7: outdoor venue types
    "parque":            ("Al Aire Libre", "Aire Libre"),
    "cerro":             ("Al Aire Libre", "Aire Libre"),
    "bosque":            ("Al Aire Libre", "Aire Libre"),
    "santuario":         ("Al Aire Libre", "Aire Libre"),
    "monumento natural": ("Al Aire Libre", "Aire Libre"),
    "parque nacional":   ("Al Aire Libre", "Aire Libre"),
    "salto":             ("Al Aire Libre", "Aire Libre"),
}

# ── Known venue name → locked category ────────────────────────────────────────
LOCKED_VENUE_CATEGORIES: dict[str, str] = {
    "movistar arena":              "Música",
    "estadio nacional":            "Música",
    "estadio bicentenario":        "Música",
    "estadio monumental":          "Música",
    "estadio san carlos":          "Música",
    "teatro palermo":              "Música",
    "teatro caupolicán":           "Música",
    "teatro oriente":              "Música",
    "teatro nescafé de las artes": "Música",
    "teatro nescafe de las artes": "Música",
}

# ── Priority order ────────────────────────────────────────────────────────────
# FIX 6: Nacional added before Rock (Chilean identity wins over genre).
# FIX 7: Festivales added after Teatro_Drama.
_RULE_PRIORITY: list[str] = [
    "Cine",
    "Jazz",           # before Comedia: jazz+improvisación → Jazz wins
    "Comedia",
    "Teatro_Familiar",
    "Teatro_Drama",
    "Festivales",     # FIX 7: large festival signals before genre rules
    "Nacional",       # FIX 6: Chilean identity before Rock/Pop/Folclore
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

_RULE_TO_CATEGORY: dict[str, str | None] = {
    "Cine":            "Cine",
    "Comedia":         "Comedia",
    "Teatro_Familiar": "Familia",
    "Teatro_Drama":    "Teatro",
    "Jazz":            "Música",
    "Rock":            "Música",
    "Pop":             "Música",
    "Indie":           "Música",
    "Electrónica":     "Música",        # FIX 4: was "Vida Nocturna"
    "Folclore":        "Música",
    "Nacional":        "Nacional",      # FIX 6
    "Latina":          "Música",
    "Arte":            "Arte",
    "Museo":           "Arte",
    "Vida Nocturna":   "Vida Nocturna",
    "Sunset":          "Vida Nocturna",
    "Feria":           "Ferias",        # FIX 7: was "Arte"
    "Festivales":      "Festivales",    # FIX 7
    "Aire_Libre":      "Al Aire Libre", # FIX 7: was None
    # Barrios: NOT classified from keywords (per spec: separate venue lookup).
    # Rule kept in KEYWORD_RULES only for keyword generation.
    "Barrios":         None,
    "City_Tour":       "Arte",
}

_RULE_TO_TYPE: dict[str, str | None] = {
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
    "Nacional":        "Nacional",
    "Latina":          "Latina",
    "Arte":            "Exposición",
    "Museo":           "Exposición",
    "Vida Nocturna":   "Vida Nocturna",
    "Sunset":          "Sunset",
    "Feria":           "Feria",
    "Festivales":      "Festival",
    "Aire_Libre":      "Aire Libre",
    "Barrios":         "Barrios",
    "City_Tour":       "City Tour",
}

# ── FIX 1: Cine venue constraint ──────────────────────────────────────────────
_CINE_VENUE_TYPES: frozenset[str] = frozenset(["cine", "cineteca"])

# Keywords that unambiguously indicate cinema content even at non-Cine venues
_STRONG_CINE_KEYWORDS: frozenset[str] = frozenset([
    "película", "film", "cineclub", "cineclube", "cineteca",
    "muestra de cine", "festival de cine", "proyección de cine",
    "sesión de cine", "ciclo de cine", "cortometraje",
])

# ── FIX 6: Nacional constants ─────────────────────────────────────────────────
_NACIONAL_BLOCKED_VENUE_TYPES: frozenset[str] = frozenset(["cine", "museo"])
_SCD_SUBSTRINGS: tuple[str, ...] = ("scd egaña", "scd bellavista", "sala scd", " scd", "scd ")

# ── FIX 8: Ferias venue exclusions ────────────────────────────────────────────
# Nightclubs whose name contains "feria" but are not markets/fairs.
# Prevents the Feria keyword rule from firing when the venue name appears in
# the event name (e.g. "OXIA - LA FERIA CLUB 26" → should be Vida Nocturna).
FERIAS_VENUE_EXCLUSIONS: tuple[str, ...] = ("la feria club", "feria club")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_night_time(time_start: Any) -> bool:
    """Return True if time_start represents 22:00 or later."""
    if not time_start:
        return False
    if hasattr(time_start, "hour"):
        return time_start.hour >= 22  # type: ignore[union-attr]
    if isinstance(time_start, str):
        try:
            return int(time_start.split(":")[0]) >= 22
        except (ValueError, IndexError):
            return False
    return False


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

    # Hard venue-type rules (FIX 2: now empty — museo moved to fallback)
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
    Always recomputes "keywords".

    See module docstring for full priority description.

    Args:
        event: dict with at least "name". Optional keys: "description",
               "venue_type", "venue_name", "source_url", "time_start".
    """
    name = event.get("name") or ""
    description = event.get("description") or ""
    venue_type = event.get("venue_type") or ""
    venue_name = event.get("venue_name") or ""
    source_url = event.get("source_url") or ""
    time_start = event.get("time_start")

    vt_lower = venue_type.lower()

    matched = _match_rules(name, description, venue_type)

    # FIX 6: Nacional is blocked at Cine/Museo venues
    if "Nacional" in matched and vt_lower in _NACIONAL_BLOCKED_VENUE_TYPES:
        matched.discard("Nacional")

    # FIX 8: Ferias exclusion for nightclubs whose name contains "feria"
    if "Feria" in matched:
        vn_lower = venue_name.lower()
        if any(exc in vn_lower for exc in FERIAS_VENUE_EXCLUSIONS):
            matched.discard("Feria")

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

    # Pop sentinels from scraper
    locked_category: str | None = event.pop("_locked_category", None)
    category_hint: str | None = event.pop("_category_hint", None)

    # FIX 1: Cancel Cine lock at non-cinema venues
    if locked_category == "Cine" and vt_lower not in _CINE_VENUE_TYPES:
        locked_category = None

    # FIX 3: Normalize locked_category in case scraper sent a garbage value
    if locked_category:
        locked_category = _normalize_category(locked_category) or locked_category

    # FIX 3: Normalize existing category (garbage values become None so keyword
    # rules can override; canonical values are preserved as-is)
    existing_raw = event.get("category") or None
    category: str | None = _normalize_category(existing_raw)
    etype: str | None = event.get("type") or None

    # --- Category resolution ---

    # 1. Scraper URL lock (highest priority)
    if locked_category:
        event["category"] = locked_category
        category = locked_category

    # 2. Known venue name lock
    if not locked_category and venue_name:
        vn_lower = venue_name.lower()
        for substr, locked_cat in LOCKED_VENUE_CATEGORIES.items():
            if substr in vn_lower:
                category = locked_cat
                break

    # 2b. FIX 6: SCD venue or portaldisc source → Nacional
    if not locked_category and category is None:
        vn_lower = venue_name.lower()
        is_scd = any(s in vn_lower for s in _SCD_SUBSTRINGS)
        is_portaldisc = "portaldisc" in source_url.lower()
        if (is_scd or is_portaldisc) and vt_lower not in _NACIONAL_BLOCKED_VENUE_TYPES:
            category = "Nacional"

    # 3. Keyword-based classification
    for rule in _RULE_PRIORITY:
        if rule not in matched:
            continue
        rule_cat = _RULE_TO_CATEGORY.get(rule)
        # Respect any category already determined (scraper lock or step 2/2b):
        # skip rules that map to a different category so they can't hijack etype.
        # Example: "bellavista" in name fires Barrios (cat=None) for a Nacional
        # event; with this guard, Barrios cannot override Nacional's etype.
        cat_lock = locked_category or category
        if cat_lock is not None and rule_cat != cat_lock:
            continue
        if category is None:
            category = rule_cat
        if etype is None:
            etype = _RULE_TO_TYPE.get(rule)
        if category is not None and etype is not None:
            break

    # Default etype for Nacional when no genre-specific keyword matched
    if category == "Nacional" and etype is None:
        etype = "Nacional"

    # FIX 1: Cine at non-Cine venue — require strong cinema keywords
    if category == "Cine" and vt_lower not in _CINE_VENUE_TYPES:
        text = f"{name} {description}".lower()
        has_strong = any(kw in text for kw in _STRONG_CINE_KEYWORDS)
        if not has_strong:
            category = None
            etype = None

    # FIX 4: Electrónica at Club/Discoteca ≥22:00 → Vida Nocturna
    if category == "Música" and etype == "Electrónica":
        if vt_lower in ("club", "discoteca") and _is_night_time(time_start):
            category = "Vida Nocturna"

    # 4. Venue-type fallback — only when keywords produced no category yet
    if category is None and not locked_category:
        fallback = VENUE_TYPE_FALLBACK.get(vt_lower)
        if fallback:
            category = fallback[0]
            if etype is None and fallback[1] is not None:
                etype = fallback[1]

    # 4b. Last-resort: Cine venues are already locked; for everything else,
    # default to Música rather than leaving category NULL.  Scraper sources
    # (Passline, Evently, etc.) only surface cultural events, so Música is a
    # safe fallback when no keyword or venue-type rule matched.
    if category is None and not locked_category and vt_lower not in _CINE_VENUE_TYPES:
        category = "Música"

    # 5. Scraper category hint (last resort)
    if category is None and category_hint and not locked_category:
        normalized_hint = _normalize_category(category_hint)
        # FIX 1: Cine hint only at Cine venues
        if normalized_hint == "Cine" and vt_lower not in _CINE_VENUE_TYPES:
            normalized_hint = None
        if normalized_hint:
            category = normalized_hint

    if category is not None:
        event["category"] = category
    if etype is not None:
        event["type"] = etype

    return event
