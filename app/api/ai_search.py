"""AI-powered natural language event search via Groq (llama-3.3-70b-versatile)."""
import json
import asyncio
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.db.base import get_db
from app.db.models import Event, Venue
from app.models.schemas import EventWithVenue, Venue as VenueSchema
from app.config import settings

router = APIRouter(prefix="/search", tags=["ai-search"])

# ── Request / Response ────────────────────────────────────────────────────────

class AISearchRequest(BaseModel):
    prompt: str
    limit: int = 8


class AISearchResponse(BaseModel):
    message: str
    events: List[dict]


# ── Intent detection ──────────────────────────────────────────────────────────

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Cine":          ["cine", "película", "pelicula", "film", "peli"],
    "Música":        ["música", "musica", "concierto", "recital", "banda", "cumbia",
                      "rock", "pop", "hip hop", "reggaeton", "electrónica", "electronica"],
    "Jazz":          ["jazz"],
    "Teatro":        ["teatro", "obra", "dramaturgia", "performance"],
    "Arte":          ["arte", "exposición", "exposicion", "galería", "galeria",
                      "museo", "exhibición", "exhibicion"],
    "Comedia":       ["comedia", "humor", "stand up", "standup"],
    "Familia":       ["familia", "familiar", "niños", "ninos", "niñas", "infantil"],
    "Vida Nocturna": ["nocturna", "club", "fiesta", "noche", "discoteca", "boliche", "bar"],
    "Ferias":        ["feria", "mercado"],
    "Festivales":    ["festival"],
    "Nacional":      ["nacional", "chilena", "chileno", "cumbia", "cueca"],
}


def _detect_category(prompt: str) -> Optional[str]:
    low = prompt.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            return category
    return None


def _detect_date_range(prompt: str) -> tuple[str, str]:
    today = date.today()
    low = prompt.lower()

    if "hoy" in low or "esta noche" in low:
        return today.isoformat(), today.isoformat()

    if "mañana" in low or "manana" in low:
        tmrw = today + timedelta(days=1)
        return tmrw.isoformat(), tmrw.isoformat()

    if "fin de semana" in low or "finde" in low or "weekend" in low:
        days_to_sat = (5 - today.weekday()) % 7 or 7
        sat = today + timedelta(days=days_to_sat)
        sun = sat + timedelta(days=1)
        return sat.isoformat(), sun.isoformat()

    if "viernes" in low:
        days = (4 - today.weekday()) % 7 or 7
        d = today + timedelta(days=days)
        return d.isoformat(), d.isoformat()

    if "sábado" in low or "sabado" in low:
        days = (5 - today.weekday()) % 7 or 7
        d = today + timedelta(days=days)
        return d.isoformat(), d.isoformat()

    if "domingo" in low:
        days = (6 - today.weekday()) % 7 or 7
        d = today + timedelta(days=days)
        return d.isoformat(), d.isoformat()

    if "esta semana" in low or "semana" in low:
        end = today + timedelta(days=7)
        return today.isoformat(), end.isoformat()

    return today.isoformat(), (today + timedelta(days=30)).isoformat()


def _detect_free(prompt: str) -> bool:
    low = prompt.lower()
    return any(w in low for w in ("gratis", "gratuito", "gratuita", "free", "sin costo"))


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _event_summary_for_llm(event: Event) -> dict:
    """Compact representation sent to the LLM to minimise token usage."""
    price = "Gratuito"
    if event.price_range and len(event.price_range) >= 1 and event.price_range[0] > 0:
        lo = event.price_range[0]
        hi = event.price_range[1] if len(event.price_range) >= 2 else lo
        price = f"${lo:,.0f}" if lo == hi else f"${lo:,.0f}–${hi:,.0f}"

    return {
        "id": event.id,
        "title": event.name,
        "category": event.category or "",
        "date": event.date,
        "time": event.time_start or "",
        "venue": event.venue.name if event.venue else "",
        "price": price,
        "description": (event.description or "")[:150],
    }


def _serialize_with_venue(event: Event) -> dict:
    """Serialize Event ORM → EventWithVenue dict (matches discover endpoint shape)."""
    venue_data = None
    if event.venue:
        venue_data = VenueSchema.model_validate(event.venue).model_dump()
    result = EventWithVenue.model_validate(event).model_dump()
    result["venue"] = venue_data
    return result


# ── Endpoint ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Eres un asistente de Eventify, una app de descubrimiento de eventos culturales \
en Santiago de Chile. Tu rol es recomendar eventos reales basándote en lo que \
el usuario pide.

Responde SIEMPRE en español, de forma cálida y concisa (máx 2 oraciones). \
Luego lista los event_ids recomendados en orden de relevancia. \
NO inventes eventos — solo usa los que te entrego.

Responde SOLO con este JSON (sin markdown, sin texto extra):
{
  "message": "tu respuesta cálida aquí",
  "event_ids": [1, 2, 3, ...]
}\
"""


@router.post("/ai", response_model=AISearchResponse)
async def ai_search(
    request: AISearchRequest,
    db: Session = Depends(get_db),
) -> AISearchResponse:
    start_str, end_str = _detect_date_range(request.prompt)
    detected_category = _detect_category(request.prompt)
    is_free = _detect_free(request.prompt)

    # ── Pre-filter: narrow to ≤ 40 candidates ────────────────────────────────
    query = (
        db.query(Event)
        .options(joinedload(Event.venue))
        .filter(Event.date >= start_str, Event.date <= end_str)
    )

    if detected_category:
        from sqlalchemy import func
        query = query.filter(
            func.lower(Event.category) == detected_category.lower()
        )

    if is_free:
        query = query.filter(Event.price_range == None)  # noqa: E711

    candidates: List[Event] = (
        query.order_by(Event.date.asc()).limit(40).all()
    )

    if not candidates:
        return AISearchResponse(
            message="No encontré eventos para lo que buscas en este momento. ¡Prueba con otra búsqueda!",
            events=[],
        )

    # ── Fallback: no API key ──────────────────────────────────────────────────
    if not settings.GROQ_API_KEY:
        return AISearchResponse(
            message="Aquí hay algunos eventos que podrían interesarte:",
            events=[_serialize_with_venue(e) for e in candidates[: request.limit]],
        )

    # ── Call Groq ─────────────────────────────────────────────────────────────
    summaries = [_event_summary_for_llm(e) for e in candidates]
    user_message = (
        f'El usuario busca: "{request.prompt}"\n\n'
        f"Eventos disponibles:\n"
        f"{json.dumps(summaries, ensure_ascii=False, indent=2)}\n\n"
        f"Selecciona los más relevantes (máx {request.limit}) y responde con el JSON indicado."
    )

    try:
        from groq import Groq

        client = Groq(api_key=settings.GROQ_API_KEY)

        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat.completions.create,
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=500,
            ),
            timeout=10.0,
        )

        raw_text = response.choices[0].message.content.strip()
        # Strip markdown code fences if the model wrapped the JSON
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        llm_data = json.loads(raw_text)

        message: str = llm_data.get("message", "Aquí hay eventos para ti:")
        event_ids: list = llm_data.get("event_ids", [])

        candidate_map = {e.id: e for e in candidates}
        ordered = [candidate_map[eid] for eid in event_ids if eid in candidate_map]

        # Pad with remaining candidates if the model returned fewer than limit
        seen = set(event_ids)
        for e in candidates:
            if len(ordered) >= request.limit:
                break
            if e.id not in seen:
                ordered.append(e)

        return AISearchResponse(
            message=message,
            events=[_serialize_with_venue(e) for e in ordered[: request.limit]],
        )

    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        pass
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass

    # ── Fallback: Groq call failed ────────────────────────────────────────────
    return AISearchResponse(
        message="Aquí hay eventos que podrían interesarte:",
        events=[_serialize_with_venue(e) for e in candidates[: request.limit]],
    )
