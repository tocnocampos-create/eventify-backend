"""Venue service layer."""
from datetime import date
from typing import List, Optional
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session, joinedload
from app.db.models import Venue, Event, Review
from app.models.schemas import VenueCreate, VenueUpdate


class VenueService:
    """Service for venue operations."""

    @staticmethod
    def get_venue(db: Session, venue_id: int) -> Optional[Venue]:
        """Get a venue by ID."""
        return db.query(Venue).filter(Venue.id == venue_id).first()

    @staticmethod
    def get_venues(
        db: Session,
        skip: int = 0,
        limit: int = 100,
        neighborhood_id: Optional[int] = None
    ) -> List[Venue]:
        """Get all venues with optional filtering by neighborhood."""
        query = db.query(Venue)
        
        if neighborhood_id is not None:
            query = query.filter(Venue.neighborhood_id == neighborhood_id)
        
        return query.offset(skip).limit(limit).all()

    @staticmethod
    def create_venue(db: Session, venue: VenueCreate) -> Venue:
        """Create a new venue."""
        db_venue = Venue(
            name=venue.name,
            venue_type=venue.venue_type,
            description=venue.description,
            stars=venue.stars,
            coordinates=venue.coordinates,
            schedule=venue.schedule,
            neighborhood_id=venue.neighborhood_id
        )
        db.add(db_venue)
        db.commit()
        db.refresh(db_venue)
        return db_venue

    @staticmethod
    def update_venue(
        db: Session,
        venue_id: int,
        venue_update: VenueUpdate
    ) -> Optional[Venue]:
        """Update a venue."""
        db_venue = db.query(Venue).filter(Venue.id == venue_id).first()
        
        if not db_venue:
            return None
        
        update_data = venue_update.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_venue, field, value)
        
        db.commit()
        db.refresh(db_venue)
        return db_venue

    @staticmethod
    def delete_venue(db: Session, venue_id: int) -> bool:
        """Delete a venue."""
        db_venue = db.query(Venue).filter(Venue.id == venue_id).first()
        
        if not db_venue:
            return False
        
        db.delete(db_venue)
        db.commit()
        return True
    
    @staticmethod
    def get_venues_by_type(db: Session, venue_types: list) -> list:
        """Return venues matching the given types, with upcoming event count.

        Excludes:
          - IDs 461, 238, 403, 260 (misclassified / city-center fallback coords)
          - Any venue whose first coordinate is ~-33.4372 (city-center fallback)
        """
        from datetime import date as date_cls

        today = str(date_cls.today())
        _EXCLUDED_IDS = (461, 238, 403, 260)
        _CITY_CENTER_LAT = -33.4372

        upcoming_sub = (
            db.query(Event.venue_id, sa_func.count(Event.id).label("cnt"))
            .filter(Event.date >= today)
            .group_by(Event.venue_id)
            .subquery()
        )

        rows = (
            db.query(Venue, sa_func.coalesce(upcoming_sub.c.cnt, 0).label("upcoming_events"))
            .outerjoin(upcoming_sub, Venue.id == upcoming_sub.c.venue_id)
            .filter(
                Venue.venue_type.in_(venue_types),
                Venue.id.notin_(_EXCLUDED_IDS),
            )
            .order_by(sa_func.coalesce(upcoming_sub.c.cnt, 0).desc(), Venue.name)
            .all()
        )

        results = []
        for venue, upcoming_events in rows:
            coords = venue.coordinates or []
            if len(coords) < 2 or coords[0] is None:
                continue
            if abs(float(coords[0]) - _CITY_CENTER_LAT) < 0.001:
                continue
            results.append({
                "id": venue.id,
                "name": venue.name,
                "venue_type": venue.venue_type,
                "description": venue.description,
                "stars": venue.stars,
                "coordinates": venue.coordinates,
                "schedule": venue.schedule,
                "city": venue.city,
                "address": venue.address,
                "cover_image_url": venue.cover_image_url,
                "profile_image_url": venue.profile_image_url,
                "website_url": venue.website_url,
                "menu_pdf_url": venue.menu_pdf_url,
                "neighborhood_id": venue.neighborhood_id,
                "created_at": venue.created_at,
                "upcoming_events": upcoming_events,
            })
        return results

    @staticmethod
    def get_all_types_of_venues(neighborhood_id: int, db: Session) -> List[str]:
        """Get all types of venues."""
        results = db.query(Venue.venue_type).filter(Venue.neighborhood_id == neighborhood_id).distinct().all()
        return [row[0] for row in results if row[0] is not None]

    @staticmethod
    def get_venue_detail(db: Session, venue_id: int) -> Optional[dict]:
        """Get venue with events and reviews aggregated."""
        venue = db.query(Venue).filter(Venue.id == venue_id).first()
        if not venue:
            return None

        today = date.today().isoformat()
        events = db.query(Event).filter(Event.venue_id == venue_id).all()
        upcoming = sorted([e for e in events if e.date >= today], key=lambda e: e.date)
        past = sorted([e for e in events if e.date < today], key=lambda e: e.date, reverse=True)

        reviews = (
            db.query(Review)
            .options(joinedload(Review.user))
            .filter(Review.venue_id == venue_id)
            .order_by(Review.created_at.desc())
            .all()
        )

        avg_result = (
            db.query(sa_func.avg(Review.rating))
            .filter(Review.venue_id == venue_id)
            .scalar()
        )

        review_dicts = [
            {
                "id": r.id,
                "user_id": r.user_id,
                "venue_id": r.venue_id,
                "event_id": r.event_id,
                "rating": r.rating,
                "comment": r.comment,
                "user_name": r.user.full_name if r.user else None,
                "created_at": r.created_at,
            }
            for r in reviews
        ]

        return {
            "venue": venue,
            "upcoming_events": upcoming,
            "past_events": past,
            "reviews": review_dicts,
            "average_rating": round(float(avg_result), 2) if avg_result else None,
            "review_count": len(reviews),
        }

