"""Museo scraper — two-phase pipeline for Santiago museum venues.

PHASE A — Venue enrichment (run once / monthly):
  Updates each museum's venue row with: opening_hours, admission_info,
  website_url, ticket_url, instagram_url, description, permanent_collection.
  Data comes from hardcoded constants + light web scraping.

PHASE B — Exposition events (run weekly):
  Scrapes each museum's cartelera/exposiciones page.
  Creates one Event per active/upcoming exposition.
  Deduplicates by source_url so re-runs only update changed fields.

Run:
    python scrapers/museo_scraper.py --dry-run          # Phase B only, no DB
    python scrapers/museo_scraper.py --enrich           # Phase A (venue data)
    python scrapers/museo_scraper.py --enrich --dry-run # Phase A dry-run
    python scrapers/museo_scraper.py                    # Phase B, writes events
"""
from __future__ import annotations

import calendar
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
}
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 1.5

TODAY = date.today().isoformat()

# ── Museum master data ─────────────────────────────────────────────────────────
#
# Each entry is the authoritative source for Phase A (venue enrichment).
# cartelera_url → Phase B scraping target; None = enrichment only.

MUSEUMS: list[dict] = [
    {
        "venue_id": 77,
        "slug": "mnba",
        "venue_name": "Museo Nacional de Bellas Artes",
        "website_url": "https://www.mnba.gob.cl",
        "cartelera_url": "https://www.mnba.gob.cl/cartelera",
        "ticket_url": None,
        "instagram_url": "https://www.instagram.com/mnbachile/",
        "opening_hours": "Mar a Dom: 10:00–18:30 | Lunes cerrado | Último acceso 18:00",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 78,
        "slug": "mac-forestal",
        "venue_name": "Museo de Arte Contemporáneo MAC Parque Forestal",
        "website_url": "https://mac.uchile.cl",
        "cartelera_url": "https://mac.uchile.cl/periodo/actuales/",
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Sáb: 11:00–17:30 | Dom: 11:00–17:30 | Lunes cerrado",
        "admission_info": "Gratuito",
        "cartelera_filter": "PF",  # only expositions at this venue
    },
    {
        "venue_id": 79,
        "slug": "mac-quinta",
        "venue_name": "Museo de Arte Contemporáneo MAC Quinta Normal",
        "website_url": "https://mac.uchile.cl",
        "cartelera_url": "https://mac.uchile.cl/periodo/actuales/",
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Sáb: 11:00–17:30 | Dom: 11:00–17:30 | Lunes cerrado",
        "admission_info": "Gratuito",
        "cartelera_filter": "QN",
    },
    {
        "venue_id": 80,
        "slug": "mmdh",
        "venue_name": "Museo de la Memoria y los Derechos Humanos",
        "website_url": "https://mmdh.cl",
        "cartelera_url": "https://mmdh.cl/exposiciones/temporales",
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Lunes cerrado | Último acceso 17:30",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 81,
        "slug": "precolombino",
        "venue_name": "Museo Chileno de Arte Precolombino",
        "website_url": "https://museo.precolombino.cl",
        "cartelera_url": "https://museo.precolombino.cl/noticias/",
        "ticket_url": "https://visit.precolombino.cl/services/buy/?id=3&service_type=21&step=0",
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Lunes cerrado | Último acceso 17:15",
        "admission_info": "Con costo — ver ticket_url",
    },
    {
        "venue_id": 82,
        "slug": "mnhn",
        "venue_name": "Museo Nacional de Historia Natural",
        "website_url": "https://www.mnhn.gob.cl",
        "cartelera_url": "https://www.mnhn.gob.cl/cartelera/proximos",
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Sáb: 10:00–17:30 | Dom: 11:00–17:30 | Lunes cerrado",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 83,
        "slug": "aeronautico",
        "venue_name": "Museo Nacional Aeronáutico y del Espacio",
        "website_url": "https://museoaeronautico.dgac.gob.cl",
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–16:30 | Lunes cerrado",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 84,
        "slug": "mhn",
        "venue_name": "Museo Histórico Nacional",
        "website_url": "https://www.mhn.gob.cl",
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–17:30 | Lunes cerrado | Último acceso 17:00",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 85,
        "slug": "mssa",
        "venue_name": "Museo de la Solidaridad Salvador Allende",
        "website_url": "https://www.mssa.cl",
        "cartelera_url": "https://www.mssa.cl/exposiciones/",
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Último acceso 17:30",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 86,
        "slug": "cousino",
        "venue_name": "Museo Palacio Cousiño",
        "website_url": "https://www.santiagocultura.cl/palacio-cousino/",
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Jue: 09:30–13:30 | Vie: 09:30–13:00 | Sáb-Dom: 10:00–13:00",
        "admission_info": "Con costo — solo visita guiada",
    },
    {
        "venue_id": 87,
        "slug": "chascona",
        "venue_name": "Casa Museo La Chascona",
        "website_url": "https://www.fundacionneruda.org/la-chascona/",
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Último acceso 17:15",
        "admission_info": "Con costo — entrada por orden de llegada",
    },
    {
        "venue_id": 88,
        "slug": "mim",
        "venue_name": "Museo Interactivo Mirador MIM",
        "website_url": "https://mim.cl",
        "cartelera_url": "https://mim.cl/eventos",
        "ticket_url": "https://compras.mim.cl/compra/entrada",
        "instagram_url": None,
        "opening_hours": "Mar: 09:30–13:30 | Mié-Vie: 09:30–17:30 | Sáb-Dom: 10:00–18:00",
        "admission_info": "Con costo — ticket online",
    },
    {
        "venue_id": 89,
        "slug": "mui",
        "venue_name": "Museo Interactivo Las Condes MUI",
        "website_url": "https://www.mui.cl",
        "cartelera_url": "https://www.mui.cl/eventos/",
        "ticket_url": "https://www.mui.cl/planea-tu-visita/",
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Último acceso 17:00",
        "admission_info": "Con costo — funciona por recorridos",
    },
    {
        "venue_id": 90,
        "slug": "mavi",
        "venue_name": "Museo de Artes Visuales MAVI",
        "website_url": "https://mavi.uc.cl",
        "cartelera_url": "https://mavi.uc.cl/exposiciones-actuales/",
        "ticket_url": "https://mavi.uc.cl/entradas/",
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Último acceso 17:30",
        "admission_info": "Con costo — gratuito menores de 12",
    },
    {
        "venue_id": 91,
        "slug": "telegrafico",
        "venue_name": "Museo Postal y Telegráfico",
        "website_url": "https://www.correos.cl",
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Lun a Vie: 09:00–18:00 | Último acceso 17:30",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 92,
        "slug": "educacion",
        "venue_name": "Museo de la Educación",
        "website_url": None,
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Lun a Vie: 10:00–17:00 | Sáb: 11:00–16:00 | Domingo cerrado",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 93,
        "slug": "violeta-parra",
        "venue_name": "Museo Violeta Parra",
        "website_url": "https://www.museovioletaparra.cl",
        "cartelera_url": "https://www.museovioletaparra.cl/actividades/agenda/",
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00 | Último acceso 17:30",
        "admission_info": "Gratuito",
    },
    {
        "venue_id": 106,
        "slug": "nemesio",
        "venue_name": "Casona Nemesio Antúnez",
        "website_url": None,
        "cartelera_url": None,
        "ticket_url": None,
        "instagram_url": None,
        "opening_hours": "Mar a Dom: 10:00–18:00",
        "admission_info": "Gratuito",
    },
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _opening_time(opening_hours: str) -> str | None:
    """Extract first opening time (HH:MM) from opening_hours string."""
    m = re.search(r"(\d{1,2}):(\d{2})", opening_hours)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _closing_time(opening_hours: str) -> str | None:
    """Extract closing time — the last HH:MM in the opening_hours string before 'Último'."""
    segment = re.sub(r"Último.*", "", opening_hours)
    matches = re.findall(r"(\d{1,2}):(\d{2})", segment)
    if len(matches) >= 2:
        h, m = matches[-1]
        return f"{int(h):02d}:{m}"
    return None


def _slug_from_title(title: str) -> str:
    """Make a stable URL slug from an exposition title."""
    s = title.lower().strip()
    for src, dst in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ñ","n"),("ü","u")]:
        s = s.replace(src, dst)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:80]


def _get_html(url: str) -> BeautifulSoup | None:
    """Fetch URL and return parsed BeautifulSoup, or None on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        logger.warning("Fetch failed %s: %s", url, exc)
        return None


def _clean(text: str) -> str:
    """Strip whitespace and collapse internal spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _first_sentences(text: str, n: int = 3) -> str:
    """Return first n sentences from a block of text."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:n]).strip()


# ── Phase A — Venue enrichment scrapers ───────────────────────────────────────

def _scrape_mnba_about() -> dict:
    soup = _get_html("https://www.mnba.gob.cl/sobre-mnba/")
    if not soup:
        return {}
    paras = soup.select("article p, .entry-content p, .content p")
    text = " ".join(_clean(p.get_text()) for p in paras[:5] if len(p.get_text().strip()) > 50)
    return {"description": _first_sentences(text, 3) if text else None}


def _scrape_mac_about() -> dict:
    soup = _get_html("https://mac.uchile.cl/historia/")
    if not soup:
        return {}
    paras = soup.select(".entry-content p, article p, .content p")
    text = " ".join(_clean(p.get_text()) for p in paras[:5] if len(p.get_text().strip()) > 50)
    return {"description": _first_sentences(text, 3) if text else None,
            "permanent_collection": "Colección de más de 4.000 obras de arte contemporáneo chileno e iberoamericano del siglo XX."}


def _scrape_mmdh_about() -> dict:
    soup = _get_html("https://mmdh.cl/sobre-el-museo/")
    if not soup:
        return {}
    paras = soup.select("p")
    text = " ".join(_clean(p.get_text()) for p in paras if len(p.get_text().strip()) > 60)
    return {"description": _first_sentences(text, 3) if text else
            "El Museo de la Memoria y los Derechos Humanos recuerda a las víctimas de violaciones a los derechos humanos ocurridas durante la dictadura militar entre 1973 y 1990."}


def _scrape_precolombino_about() -> dict:
    soup = _get_html("https://museo.precolombino.cl/el-museo/")
    if not soup:
        return {}
    paras = soup.select("p")
    text = " ".join(_clean(p.get_text()) for p in paras[:5] if len(p.get_text().strip()) > 60)
    return {"description": _first_sentences(text, 3) if text else
            "El Museo Chileno de Arte Precolombino exhibe arte y cultura de las civilizaciones indígenas de América precolombina.",
            "permanent_collection": "Más de 3.000 objetos de 15 culturas indígenas americanas, desde México hasta la Patagonia."}


def _scrape_mnhn_about() -> dict:
    return {"description": "El Museo Nacional de Historia Natural de Chile es el museo de ciencias naturales más antiguo del país, fundado en 1830. Exhibe colecciones de zoología, botánica, geología, paleontología y antropología.",
            "permanent_collection": "Más de 4 millones de especímenes en ciencias naturales, incluyendo la ballena azul y el meteorito Imilac."}


def _scrape_mssa_about() -> dict:
    soup = _get_html("https://www.mssa.cl/sobre-el-museo/")
    if not soup:
        return {}
    paras = soup.select("p")
    text = " ".join(_clean(p.get_text()) for p in paras[:5] if len(p.get_text().strip()) > 60)
    return {"description": _first_sentences(text, 3) if text else
            "El Museo de la Solidaridad Salvador Allende nació en 1972 como acto de solidaridad internacional con la Revolución Chilena.",
            "permanent_collection": "Obras donadas por artistas de todo el mundo en solidaridad con el proyecto de la Unidad Popular."}


def _scrape_mavi_about() -> dict:
    return {"description": "El Museo de Artes Visuales MAVI exhibe arte contemporáneo chileno e internacional en el corazón del Barrio Lastarria. Fundado por la Corporación Amigos del MAVI.",
            "permanent_collection": "Colección privada de arte chileno contemporáneo de los siglos XX y XXI."}


def _scrape_violeta_about() -> dict:
    soup = _get_html("https://www.museovioletaparra.cl/sobre-nosotros/")
    if not soup:
        return {}
    paras = soup.select("p")
    text = " ".join(_clean(p.get_text()) for p in paras[:4] if len(p.get_text().strip()) > 60)
    return {"description": _first_sentences(text, 3) if text else
            "El Museo Violeta Parra preserva y difunde la obra y legado de Violeta Parra, artista, folclorista y compositora chilena."}


# Map venue_id → about scraper function
ABOUT_SCRAPERS: dict[int, Any] = {
    77:  _scrape_mnba_about,
    78:  _scrape_mac_about,
    79:  _scrape_mac_about,
    80:  _scrape_mmdh_about,
    81:  _scrape_precolombino_about,
    82:  _scrape_mnhn_about,
    85:  _scrape_mssa_about,
    90:  _scrape_mavi_about,
    93:  _scrape_violeta_about,
}


# ── Phase B — Cartelera scrapers ───────────────────────────────────────────────

def _parse_mnba_cartelera(museum: dict) -> list[dict]:
    """MNBA: https://www.mnba.gob.cl/cartelera"""
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    # MNBA uses WordPress-style event cards
    cards = soup.select(".tribe-event-card, article.tribe_events_cat-exposicion, .type-tribe_events")
    if not cards:
        # Fallback: look for any article cards
        cards = soup.select("article, .event-card, .card")
    for card in cards:
        title_el = card.select_one("h2, h3, .tribe-event-name, .entry-title, a")
        if not title_el:
            continue
        title = _clean(title_el.get_text())
        if not title or len(title) < 4:
            continue
        # Date
        date_el = card.select_one("time, .tribe-event-date-start, .date")
        date_str = TODAY
        if date_el:
            dt_attr = date_el.get("datetime", "")
            if dt_attr:
                date_str = dt_attr[:10]
            else:
                date_str = _parse_spanish_date(date_el.get_text()) or TODAY
        # Description
        desc_el = card.select_one("p, .tribe-event-description, .entry-summary")
        description = _clean(desc_el.get_text()) if desc_el else ""
        # Image
        img_el = card.select_one("img")
        img_url = img_el.get("src") or img_el.get("data-src") if img_el else None
        # Link
        link_el = card.select_one("a")
        link = link_el.get("href") if link_el else museum["website_url"]
        # End date
        date_end_el = card.select_one(".tribe-event-end-date, .tribe-event-date-end, time[itemprop='endDate']")
        if date_end_el:
            dt_end = date_end_el.get("datetime", "")
            date_end = dt_end[:10] if dt_end else _parse_end_date(date_end_el.get_text())
        else:
            date_end = _parse_end_date(card.get_text())

        events.append(_make_event(museum, title, date_str, description, img_url, link, date_end=date_end))
    return events


def _parse_mac_cartelera(museum: dict) -> list[dict]:
    """MAC UChile: https://mac.uchile.cl/periodo/actuales/
    Articles use class type-exposiciones; sede is indicated by .sede-circle span
    text: 'QN' = Quinta Normal, 'PF' = Parque Forestal.
    """
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    venue_filter = museum.get("cartelera_filter", "")  # "QN" or "PF"
    for card in soup.select("article.type-exposiciones"):
        # Sede filter
        sede_el = card.select_one(".sede-circle span, [class*='circle']")
        sede = sede_el.get_text(strip=True) if sede_el else ""
        if venue_filter and venue_filter not in sede:
            continue
        # Title: heading link inside the article
        link_el = card.select_one("h3 a, h2 a, .entry-title a, .title-exposicion a")
        if not link_el:
            continue
        title = _clean(link_el.get_text())
        if not title or len(title) < 4:
            continue
        desc_el = card.select_one("p, .excerpt, .entry-summary")
        description = _clean(desc_el.get_text()) if desc_el else ""
        img_el = card.select_one("img")
        img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        link = link_el.get("href", museum["website_url"])
        date_end = _parse_end_date(card.get_text())
        events.append(_make_event(museum, title, TODAY, description, img_url, link, date_end=date_end))
    return events


def _parse_mmdh_cartelera(museum: dict) -> list[dict]:
    """MMDH: Vue/Quasar SPA — content rendered client-side, not scrapable via requests.
    Returns empty list; expositions must be added manually or via Playwright.
    """
    logger.debug("[museo] MMDH is a Vue SPA — skipping Phase B (client-side rendered)")
    return []


def _parse_precolombino_noticias(museum: dict) -> list[dict]:
    """Precolombino: news items that may include expositions."""
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    cards = soup.select("article, .noticia, .post, .entry")
    for card in cards[:10]:
        title_el = card.select_one("h2, h3, h4, .entry-title")
        if not title_el:
            continue
        title = _clean(title_el.get_text())
        if not title or len(title) < 4:
            continue
        # Only keep items that look like expositions
        if not any(kw in title.lower() for kw in ["exposición", "exposicion", "muestra", "colección", "coleccion", "exhibición"]):
            continue
        desc_el = card.select_one("p, .excerpt")
        description = _clean(desc_el.get_text()) if desc_el else ""
        img_el = card.select_one("img")
        img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        link_el = card.select_one("a")
        link = link_el.get("href") if link_el else museum["website_url"]
        date_end = _parse_end_date(card.get_text())
        events.append(_make_event(museum, title, TODAY, description, img_url, link, date_end=date_end))
    return events


def _parse_mnhn_cartelera(museum: dict) -> list[dict]:
    """MNHN: https://www.mnhn.gob.cl/cartelera/proximos"""
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    cards = soup.select("article, .card, .event, .actividad")
    for card in cards:
        title_el = card.select_one("h2, h3, h4, .title, .entry-title")
        if not title_el:
            continue
        title = _clean(title_el.get_text())
        if not title or len(title) < 4:
            continue
        date_el = card.select_one("time, .date, .fecha")
        date_str = TODAY
        if date_el:
            dt_attr = date_el.get("datetime", "")
            date_str = dt_attr[:10] if dt_attr else (_parse_spanish_date(date_el.get_text()) or TODAY)
        desc_el = card.select_one("p, .excerpt, .description")
        description = _clean(desc_el.get_text()) if desc_el else ""
        img_el = card.select_one("img")
        img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        link_el = card.select_one("a")
        link = link_el.get("href") if link_el else museum["website_url"]
        date_end = _parse_end_date(card.get_text())
        events.append(_make_event(museum, title, date_str, description, img_url, link, date_end=date_end))
    return events


def _parse_mssa_exposiciones(museum: dict) -> list[dict]:
    """MSSA: https://www.mssa.cl/exposiciones/"""
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    cards = soup.select("article, .exposicion, .post, .entry")
    for card in cards:
        title_el = card.select_one("h2, h3, h4, .entry-title")
        if not title_el:
            continue
        title = _clean(title_el.get_text())
        if not title or len(title) < 4:
            continue
        desc_el = card.select_one("p, .excerpt")
        description = _clean(desc_el.get_text()) if desc_el else ""
        img_el = card.select_one("img")
        img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        link_el = card.select_one("a")
        link = link_el.get("href") if link_el else museum["website_url"]
        date_end = _parse_end_date(card.get_text())
        events.append(_make_event(museum, title, TODAY, description, img_url, link, date_end=date_end))
    return events


def _parse_mim_events(museum: dict) -> list[dict]:
    """MIM: Next.js SPA — content rendered client-side, no __NEXT_DATA__ in static HTML.
    Returns empty list; events must be added manually or via Playwright.
    """
    logger.debug("[museo] MIM is a Next.js SPA — skipping Phase B (client-side rendered)")
    return []


def _parse_mui_events(museum: dict) -> list[dict]:
    """MUI: https://www.mui.cl/eventos/"""
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    cards = soup.select("article, .event, .card, .post")
    for card in cards:
        title_el = card.select_one("h2, h3, h4, .entry-title")
        if not title_el:
            continue
        title = _clean(title_el.get_text())
        if not title or len(title) < 4:
            continue
        desc_el = card.select_one("p, .excerpt")
        description = _clean(desc_el.get_text()) if desc_el else ""
        img_el = card.select_one("img")
        img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        link_el = card.select_one("a")
        link = link_el.get("href") if link_el else museum["website_url"]
        date_end = _parse_end_date(card.get_text())
        events.append(_make_event(museum, title, TODAY, description, img_url, link, date_end=date_end))
    return events


def _parse_mavi_exposiciones(museum: dict) -> list[dict]:
    """MAVI: WordPress with custom post type 'exposiciones'.
    Uses WP REST API /wp-json/wp/v2/exposiciones.
    """
    try:
        r = requests.get(
            "https://mavi.uc.cl/wp-json/wp/v2/exposiciones?per_page=20&orderby=date&order=desc",
            headers=HEADERS, timeout=25,
        )
        if r.status_code != 200:
            return []
        posts = r.json()
    except Exception as exc:
        logger.warning("[museo] MAVI WP REST failed: %s", exc)
        return []
    events = []
    for post in posts:
        title = _clean(post.get("title", {}).get("rendered", ""))
        if not title or len(title) < 4:
            continue
        raw_desc = post.get("excerpt", {}).get("rendered", "") or post.get("content", {}).get("rendered", "")
        description = _clean(BeautifulSoup(raw_desc, "html.parser").get_text()) if raw_desc else ""
        img_url = post.get("jetpack_featured_media_url") or None
        link = post.get("link", museum["website_url"])
        date_str = (post.get("date", "") or "")[:10] or TODAY
        # Try end date from ACF 'fecha_termino' or 'fecha_fin', else parse from content
        acf = post.get("acf", {}) or {}
        raw_end = acf.get("fecha_termino") or acf.get("fecha_fin") or acf.get("fecha_hasta") or ""
        date_end = raw_end[:10] if raw_end and len(raw_end) >= 10 else _parse_end_date(description)
        events.append(_make_event(museum, title, date_str, description, img_url, link, date_end=date_end))
    return events


def _parse_violeta_agenda(museum: dict) -> list[dict]:
    """Violeta Parra: uses JetEngine listing grid (div.jet-listing-grid__item).
    Each item contains title + Spanish date string in its text content.
    """
    soup = _get_html(museum["cartelera_url"])
    if not soup:
        return []
    events = []
    for card in soup.select("div.jet-listing-grid__item"):
        # Title: first heading or first non-empty link text
        title_el = card.select_one("h2, h3, h4, [class*='title'], [class*='heading']")
        if not title_el:
            # Fallback: extract first line of the card text as title
            raw_text = _clean(card.get_text())
            lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
            if not lines:
                continue
            title = lines[0]
        else:
            title = _clean(title_el.get_text())
        if not title or len(title) < 4:
            continue
        # Date: parse first Spanish date found in card text
        card_text = card.get_text()
        date_str = _parse_spanish_date(card_text) or TODAY
        # Image
        img_el = card.select_one("img")
        img_url = (img_el.get("src") or img_el.get("data-src")) if img_el else None
        if img_url and img_url.startswith("data:"):
            img_url = None
        # Link
        link_el = card.select_one("a[href]")
        link = link_el.get("href") if link_el else museum["website_url"]
        # Description: remaining text after title
        raw_text = _clean(card.get_text())
        description = raw_text[len(title):].strip()[:500]
        date_end = _parse_end_date(card_text)
        events.append(_make_event(museum, title, date_str, description, img_url, link, date_end=date_end))
    return events


# Map venue_id → cartelera parser function
CARTELERA_PARSERS: dict[int, Any] = {
    77:  _parse_mnba_cartelera,
    78:  _parse_mac_cartelera,
    79:  _parse_mac_cartelera,
    80:  _parse_mmdh_cartelera,
    81:  _parse_precolombino_noticias,
    82:  _parse_mnhn_cartelera,
    85:  _parse_mssa_exposiciones,
    88:  _parse_mim_events,
    89:  _parse_mui_events,
    90:  _parse_mavi_exposiciones,
    93:  _parse_violeta_agenda,
}


# ── Spanish date parser ────────────────────────────────────────────────────────

MONTH_MAP = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}

def _parse_spanish_date(text: str) -> str | None:
    """Parse Spanish date strings to YYYY-MM-DD. Returns None on failure."""
    text = text.lower().strip()
    # ISO format: 2026-05-10
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # "10 de mayo de 2026", "10 mayo 2026", or "9 de mayo, 2026" (comma before year)
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)[,\s]+(?:de\s+)?(\d{4})", text)
    if m:
        month_num = MONTH_MAP.get(m.group(2)[:3])
        if month_num:
            return f"{m.group(3)}-{month_num:02d}-{int(m.group(1)):02d}"
    # "mayo 2026" or "mayo, 2026" → first of month
    m = re.search(r"([a-záéíóúñ]+)[,\s]+(\d{4})", text)
    if m:
        month_num = MONTH_MAP.get(m.group(1)[:3])
        if month_num:
            return f"{m.group(2)}-{month_num:02d}-01"
    return None


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _parse_end_date(text: str) -> str | None:
    """Extract an end date from text with range patterns.

    Handles:
      'hasta el 15 de mayo de 2026'
      'hasta mayo 2026'             → last day of that month
      'al 15 de junio de 2026'
      'D de MMMM – D de MMMM YYYY' → part after the dash
    Returns YYYY-MM-DD or None.
    """
    if not text:
        return None
    tl = text.lower()

    # "hasta ..." / "al ..." markers
    for marker in (r"hasta\s+(?:el\s+)?", r"\bal\s+"):
        # "hasta D de MMMM de YYYY"
        m = re.search(
            marker + r"(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)[,\s]+(?:de\s+)?(\d{4})",
            tl,
        )
        if m:
            mn = MONTH_MAP.get(m.group(2)[:3])
            if mn:
                return f"{m.group(3)}-{mn:02d}-{int(m.group(1)):02d}"
        # "hasta MMMM YYYY" → last day
        m = re.search(marker + r"([a-záéíóúñ]+)\s+(?:de\s+)?(\d{4})", tl)
        if m:
            mn = MONTH_MAP.get(m.group(1)[:3])
            if mn:
                yr = int(m.group(2))
                return f"{yr}-{mn:02d}-{_last_day_of_month(yr, mn):02d}"

    # Dash range: "D MMMM – D MMMM YYYY" → extract after the dash
    m = re.search(
        r"[–—\-]\s*(\d{1,2})\s+(?:de\s+)?([a-záéíóúñ]+)[,\s]+(?:de\s+)?(\d{4})",
        tl,
    )
    if m:
        mn = MONTH_MAP.get(m.group(2)[:3])
        if mn:
            return f"{m.group(3)}-{mn:02d}-{int(m.group(1)):02d}"

    return None


# ── Event factory ──────────────────────────────────────────────────────────────

def _make_event(museum: dict, title: str, date_str: str, description: str,
                img_url: str | None, url: str | None,
                date_end: str | None = None) -> dict:
    """Build a standardised event dict for a museum exposition."""
    slug = _slug_from_title(title)
    source_url = f"museo:{museum['slug']}:{slug}"

    opening_hours = museum.get("opening_hours", "")
    time_start = _opening_time(opening_hours)
    time_end = _closing_time(opening_hours)

    admission = museum.get("admission_info", "")
    if "Gratuito" in admission or "gratuito" in admission:
        price_range = [0.0, 0.0]
    elif "costo" in admission.lower():
        price_range = None  # unknown — varies
    else:
        price_range = None

    ticket = museum.get("ticket_url") or museum.get("website_url")

    return {
        "name": title,
        "description": description[:1000] if description else "",
        "date": date_str,
        "date_end": date_end,
        "time_start": time_start,
        "time_end": time_end,
        "venue_name": museum["venue_name"],
        "category": "Arte",
        "type": "Exposición",
        "_locked_category": "Arte",
        "price_range": price_range,
        "image_url": img_url if img_url and img_url.startswith("http") else None,
        "source_url": source_url,
        "ticket_url": url or ticket,
        "url": url or ticket,
        "kids_friendly": False,
        "is_sold_out": False,
    }


# ── Main scraper class ─────────────────────────────────────────────────────────

class MuseoScraper(BaseScraper):
    """Two-phase scraper for Santiago museum venues and expositions."""

    name = "museo"

    def __init__(self, max_events: int = 0, enrich: bool = False, dry_run: bool = False) -> None:
        super().__init__()
        self.max_events = max_events
        self.enrich = enrich
        self.dry_run = dry_run

    # ── Phase A ───────────────────────────────────────────────────────────────

    def run_venue_enrichment(self, db_conn=None) -> None:
        """Phase A: Update venue rows with museum-specific metadata."""
        import psycopg2

        DB_URL = os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz@shuttle.proxy.rlwy.net:17408/railway",
        )

        conn = db_conn or psycopg2.connect(DB_URL)
        should_close = db_conn is None
        cur = conn.cursor()

        for museum in MUSEUMS:
            vid = museum["venue_id"]
            # Collect enrichment data
            update: dict[str, Any] = {
                "opening_hours": museum.get("opening_hours"),
                "admission_info": museum.get("admission_info"),
                "instagram_url": museum.get("instagram_url"),
            }
            if museum.get("website_url"):
                update["website_url"] = museum["website_url"]
            if museum.get("ticket_url"):
                update["ticket_url"] = museum["ticket_url"]

            # Scrape about/description if scraper defined
            about_fn = ABOUT_SCRAPERS.get(vid)
            if about_fn:
                time.sleep(REQUEST_DELAY)
                about = about_fn()
                update.update({k: v for k, v in about.items() if v})

            if self.dry_run:
                logger.info("[museo] DRY-RUN enrich id=%d %s → %s",
                            vid, museum["venue_name"],
                            {k: (v[:60] + "...") if isinstance(v, str) and len(v) > 60 else v
                             for k, v in update.items()})
                print(f"  DRY-RUN enrich id={vid} {museum['venue_name']}")
                continue

            # Build SET clause
            cols = list(update.keys())
            vals = [update[c] for c in cols]
            set_clause = ", ".join(f"{c} = %s" for c in cols)
            cur.execute(f"UPDATE venues SET {set_clause} WHERE id = %s", vals + [vid])
            conn.commit()
            print(f"  ✓ {museum['venue_name']} venue enriched")

        cur.close()
        if should_close:
            conn.close()

    # ── Phase B ───────────────────────────────────────────────────────────────

    def fetch_events(self) -> list[dict]:
        """Phase B: Scrape current expositions for all museums with cartelera URLs."""
        all_events: list[dict] = []
        seen: set[str] = set()

        for museum in MUSEUMS:
            cartelera_url = museum.get("cartelera_url")
            if not cartelera_url:
                continue

            parser = CARTELERA_PARSERS.get(museum["venue_id"])
            if not parser:
                continue

            logger.info("[museo] Scraping %s ...", museum["venue_name"])
            time.sleep(REQUEST_DELAY)
            try:
                events = parser(museum)
            except Exception as exc:
                logger.warning("[museo] Parser error %s: %s", museum["slug"], exc)
                events = []

            # Deduplicate within run; skip ended expositions
            for e in events:
                # Use date_end if set (exposition may have started in the past but still running)
                # Only skip if the exposition has actually ended
                effective_end = e.get("date_end") or e.get("date") or ""
                if effective_end and effective_end < TODAY:
                    continue
                src = e.get("source_url", "")
                if src and src in seen:
                    continue
                if src:
                    seen.add(src)
                all_events.append(e)
                if self.max_events and len(all_events) >= self.max_events:
                    return all_events

            logger.info("[museo] %s → %d exposition(s)", museum["venue_name"], len(events))

        return all_events


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Museum scraper — venue enrichment + expositions")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    parser.add_argument("--enrich", action="store_true", help="Run Phase A (venue enrichment)")
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    scraper = MuseoScraper(
        max_events=args.max_events,
        enrich=args.enrich,
        dry_run=args.dry_run,
    )

    if args.enrich:
        print("\n=== Phase A: Venue Enrichment ===")
        scraper.run_venue_enrichment()

    print("\n=== Phase B: Exposition Events (dry-run) ===")
    events = scraper.fetch_events()
    print(f"\nTotal expositions found: {len(events)}")

    from collections import Counter
    by_venue: Counter = Counter(e["venue_name"] for e in events)
    print("\nPer museum:")
    for name, count in sorted(by_venue.items()):
        marker = "✓" if count > 0 else "⚠ 0"
        print(f"  {marker}  {name}: {count}")

    zero = [m["venue_name"] for m in MUSEUMS if m.get("cartelera_url") and
            by_venue.get(m["venue_name"], 0) == 0]
    if zero:
        print(f"\n⚠  0 expositions found for:")
        for n in zero:
            print(f"     {n}")

    if args.verbose and events:
        print("\nSample events (first 3):")
        for e in events[:3]:
            print(json.dumps(e, indent=2, ensure_ascii=False, default=str))
