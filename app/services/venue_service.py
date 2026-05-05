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

