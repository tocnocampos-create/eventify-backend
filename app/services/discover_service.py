"""Discover feed service — trending, today, weekly, nearby, personalized."""
import math
from datetime import date, timedelta
from typing import List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.db.models import (
    Event,
    Review,
    UserInterest,
    UserSavedEvent,
    UserVenueFollow,
    Venue,
)


class DiscoverService:
    """Builds the discover feed with multiple curated sections."""

    @staticmethod
    def get_discover_feed(
        db: Session,
        user_id: Optional[int] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        city: str = "Santiago",
        radius_km: float = 10,
    ) -> dict:
        today_str = date.today().isoformat()

        trending = DiscoverService._get_trending_events(db, today_str, city)
        today_events = DiscoverService._get_today_events(db, today_str, city)
        this_week = DiscoverService._get_week_highlights(db, today_str, city)
        nearby_venues = DiscoverService._get_nearby_venues(db, lat, lon, city, radius_km)
        popular_categories = DiscoverService._get_popular_categories(db, today_str)

        result = {
            "trending": trending,
            "today": today_events,
            "this_week": this_week,
            "nearby_venues": nearby_venues,
            "popular_categories": popular_categories,
        }

        if user_id is not None:
            result["for_you"] = DiscoverService._get_personalized_picks(
                db, user_id, today_str, lat=lat, lon=lon
            )

        return result

    # ── Trending ──────────────────────────────────────────────────

    @staticmethod
    def _get_trending_events(
        db: Session, today_str: str, city: str, limit: int = 10
    ) -> List[Event]:
        saves_sub = (
            db.query(
                UserSavedEvent.event_id,
                func.count().label("saves_count"),
            )
            .group_by(UserSavedEvent.event_id)
            .subquery()
        )
        reviews_sub = (
            db.query(
                Review.event_id,
                func.count().label("review_count"),
                func.coalesce(func.avg(Review.rating), 0).label("avg_rating"),
            )
            .filter(Review.event_id.isnot(None))
            .group_by(Review.event_id)
            .subquery()
        )

        score = (
            func.coalesce(saves_sub.c.saves_count, 0) * 3
            + func.coalesce(reviews_sub.c.review_count, 0) * 2
            + func.coalesce(reviews_sub.c.avg_rating, 0) * 1.5
        ).label("score")

        query = (
            db.query(Event)
            .options(joinedload(Event.venue))
            .outerjoin(saves_sub, Event.id == saves_sub.c.event_id)
            .outerjoin(reviews_sub, Event.id == reviews_sub.c.event_id)
            .filter(Event.date >= today_str)
        )
        if city:
            query = query.join(Venue, Event.venue_id == Venue.id).filter(
                Venue.city == city
            )

        return query.order_by(score.desc()).limit(limit).all()

    # ── Today ─────────────────────────────────────────────────────

    @staticmethod
    def _get_today_events(
        db: Session, today_str: str, city: str, limit: int = 15
    ) -> List[Event]:
        query = (
            db.query(Event)
            .options(joinedload(Event.venue))
            .filter(Event.date == today_str)
        )
        if city:
            query = query.join(Venue, Event.venue_id == Venue.id).filter(
                Venue.city == city
            )
        return query.order_by(Event.time_start).limit(limit).all()

    # ── This Week ─────────────────────────────────────────────────

    @staticmethod
    def _get_week_highlights(
        db: Session, today_str: str, city: str, limit: int = 10
    ) -> List[Event]:
        week_end = (date.today() + timedelta(days=6)).isoformat()

        saves_sub = (
            db.query(
                UserSavedEvent.event_id,
                func.count().label("saves_count"),
            )
            .group_by(UserSavedEvent.event_id)
            .subquery()
        )
        reviews_sub = (
            db.query(
                Review.event_id,
                func.count().label("review_count"),
                func.coalesce(func.avg(Review.rating), 0).label("avg_rating"),
            )
            .filter(Review.event_id.isnot(None))
            .group_by(Review.event_id)
            .subquery()
        )

        score = (
            func.coalesce(saves_sub.c.saves_count, 0) * 3
            + func.coalesce(reviews_sub.c.review_count, 0) * 2
            + func.coalesce(reviews_sub.c.avg_rating, 0) * 1.5
        ).label("score")

        query = (
            db.query(Event)
            .options(joinedload(Event.venue))
            .outerjoin(saves_sub, Event.id == saves_sub.c.event_id)
            .outerjoin(reviews_sub, Event.id == reviews_sub.c.event_id)
            .filter(Event.date >= today_str, Event.date <= week_end)
        )
        if city:
            query = query.join(Venue, Event.venue_id == Venue.id).filter(
                Venue.city == city
            )

        return query.order_by(score.desc()).limit(limit).all()

    # ── Nearby Venues ─────────────────────────────────────────────

    @staticmethod
    def _get_nearby_venues(
        db: Session,
        lat: Optional[float],
        lon: Optional[float],
        city: str,
        radius_km: float,
        limit: int = 10,
    ) -> list:
        if lat is None or lon is None:
            # Fallback to city-based venue list
            venues = (
                db.query(Venue)
                .filter(Venue.city == city)
                .order_by(Venue.stars.desc().nullslast())
                .limit(limit)
                .all()
            )
            return [{"venue": v, "distance_km": None} for v in venues]

        # Bounding box pre-filter (equirectangular approximation)
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))

        venues = (
            db.query(Venue)
            .filter(
                Venue.coordinates[1] >= lat - lat_delta,
                Venue.coordinates[1] <= lat + lat_delta,
                Venue.coordinates[2] >= lon - lon_delta,
                Venue.coordinates[2] <= lon + lon_delta,
            )
            .all()
        )

        # Compute actual distance and sort
        results = []
        for v in venues:
            v_lat = v.coordinates[0]
            v_lon = v.coordinates[1]
            dist = DiscoverService._haversine(lat, lon, v_lat, v_lon)
            if dist <= radius_km:
                results.append({"venue": v, "distance_km": round(dist, 1)})

        results.sort(key=lambda x: x["distance_km"])
        return results[:limit]

    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2
        )
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    # ── Popular Categories ────────────────────────────────────────

    @staticmethod
    def _get_popular_categories(
        db: Session, today_str: str, limit: int = 8
    ) -> list:
        rows = (
            db.query(Event.category, func.count().label("event_count"))
            .filter(Event.date >= today_str, Event.category.isnot(None))
            .group_by(Event.category)
            .order_by(func.count().desc())
            .limit(limit)
            .all()
        )
        return [{"category": r[0], "event_count": r[1]} for r in rows]

    # ── Personalized Picks (For You) ──────────────────────────────

    # Keywords for mode-based filtering (all lowercase for comparison)
    _EXPLORADOR_KW: Set[str] = {"city tour", "tour", "barrio", "patrimonio", "ruta"}
    _ENGRUPO_KW: Set[str] = {"club", "dj", "after", "vida nocturna", "festival"}
    _TRANQUILO_EXCLUDE_KW: Set[str] = {"club", "boliche", "after", "vida nocturna", "dj"}

    @staticmethod
    def _event_keywords_lower(ev: Event) -> Set[str]:
        """Return the event's keyword list as a lowercased set."""
        return {k.lower() for k in (ev.keywords or [])}

    @staticmethod
    def _get_personalized_picks(
        db: Session,
        user_id: int,
        today_str: str,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        limit: int = 10,
    ) -> List[Event]:
        interests = db.query(UserInterest).filter_by(user_id=user_id).all()

        # Split interests into two buckets
        category_interests = [i for i in interests if i.category]
        active_modes: Set[str] = {
            i.exploration_mode for i in interests if i.exploration_mode
        }

        followed_venue_ids = [
            r.venue_id
            for r in db.query(UserVenueFollow.venue_id)
            .filter_by(user_id=user_id)
            .all()
        ]
        saved_event_ids = set(
            r.event_id
            for r in db.query(UserSavedEvent.event_id)
            .filter_by(user_id=user_id)
            .all()
        )

        upcoming = (
            db.query(Event)
            .options(joinedload(Event.venue))
            .filter(Event.date >= today_str)
            .all()
        )

        # Pre-compute trending scores once if EnGrupo is active (avoids N+1)
        trending_scores: dict = {}
        if "EnGrupo" in active_modes:
            saves_counts = dict(
                db.query(UserSavedEvent.event_id, func.count())
                .group_by(UserSavedEvent.event_id)
                .all()
            )
            review_counts = dict(
                db.query(Review.event_id, func.count())
                .filter(Review.event_id.isnot(None))
                .group_by(Review.event_id)
                .all()
            )
            for ev_id in set(saves_counts) | set(review_counts):
                trending_scores[ev_id] = (
                    saves_counts.get(ev_id, 0) * 3
                    + review_counts.get(ev_id, 0) * 2
                )

        scored = []
        for ev in upcoming:
            if ev.id in saved_event_ids:
                continue

            kw = DiscoverService._event_keywords_lower(ev)

            # ── Hard exclusions (applied before scoring) ──────────
            if "EnFamilia" in active_modes:
                if ev.time_start and ev.time_start >= "22:00":
                    continue

            if "Tranquilo" in active_modes:
                if kw & DiscoverService._TRANQUILO_EXCLUDE_KW:
                    continue

            # ── Base category / follow scoring ────────────────────
            score = 0
            for interest in category_interests:
                if ev.category and ev.category.lower() == interest.category.lower():
                    if (
                        interest.subtype
                        and ev.type
                        and ev.type.lower() == interest.subtype.lower()
                    ):
                        score += 3
                    elif not interest.subtype:
                        score += 2
            if ev.venue_id in followed_venue_ids:
                score += 1

            # ── Exploration mode boosts ───────────────────────────
            if "Espontaneo" in active_modes:
                if ev.date == today_str:
                    score += 3
                if ev.time_start and ev.time_start >= "20:00":
                    score += 2

            if "Explorador" in active_modes:
                if kw & DiscoverService._EXPLORADOR_KW:
                    score += 3
                if ev.venue and ev.venue.neighborhood_id is not None:
                    score += 2

            if "EnFamilia" in active_modes:
                if ev.kids_friendly:
                    score += 5
                if ev.age_restriction is None or ev.age_restriction <= 12:
                    score += 2

            if "Tranquilo" in active_modes:
                if lat is not None and lon is not None and ev.venue:
                    coords = ev.venue.coordinates
                    if coords and len(coords) >= 2:
                        dist = DiscoverService._haversine(
                            lat, lon, coords[0], coords[1]
                        )
                        if dist <= 3.0:
                            score += 4

            if "EnGrupo" in active_modes:
                if kw & DiscoverService._ENGRUPO_KW:
                    score += 3
                if trending_scores.get(ev.id, 0) > 5:
                    score += 2

            if score > 0:
                scored.append((score, ev.date, ev))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [item[2] for item in scored[:limit]]
