"""Event service layer."""
from typing import List, Optional
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session, joinedload
from app.db.models import Event, EventProduct, EventCommunityLink, Review, Venue
from app.models.schemas import EventCreate, EventUpdate


class EventService:
    """Service for event operations."""

    @staticmethod
    def get_event(db: Session, event_id: int) -> Optional[Event]:
        """Get an event by ID."""
        return db.query(Event).filter(Event.id == event_id).first()

    @staticmethod
    def get_events(
        db: Session,
        skip: int = 0,
        limit: int = 100,
        venue_id: Optional[int] = None,
        category: Optional[str] = None
    ) -> List[Event]:
        """Get all events with optional filtering."""
        query = db.query(Event)
        
        if venue_id is not None:
            query = query.filter(Event.venue_id == venue_id)
        
        if category is not None:
            query = query.filter(Event.category == category)
        
        return query.offset(skip).limit(limit).all()

    @staticmethod
    def create_event(db: Session, event: EventCreate) -> Event:
        """Create a new event."""
        db_event = Event(
            name=event.name,
            type=event.type,
            category=event.category,
            keywords=event.keywords,
            description=event.description,
            price_range=event.price_range,
            date=event.date,
            venue_id=event.venue_id
        )
        db.add(db_event)
        db.commit()
        db.refresh(db_event)
        return db_event

    @staticmethod
    def update_event(
        db: Session,
        event_id: int,
        event_update: EventUpdate
    ) -> Optional[Event]:
        """Update an event."""
        db_event = db.query(Event).filter(Event.id == event_id).first()
        
        if not db_event:
            return None
        
        update_data = event_update.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_event, field, value)
        
        db.commit()
        db.refresh(db_event)
        return db_event

    @staticmethod
    def delete_event(db: Session, event_id: int) -> bool:
        """Delete an event."""
        db_event = db.query(Event).filter(Event.id == event_id).first()

        if not db_event:
            return False

        db.delete(db_event)
        db.commit()
        return True

    @staticmethod
    def get_event_detail(db: Session, event_id: int) -> Optional[dict]:
        """Get event with venue and reviews aggregated."""
        event = db.query(Event).filter(Event.id == event_id).first()
        if not event:
            return None

        venue = None
        if event.venue_id:
            venue = db.query(Venue).filter(Venue.id == event.venue_id).first()

        reviews = (
            db.query(Review)
            .options(joinedload(Review.user))
            .filter(Review.event_id == event_id)
            .order_by(Review.created_at.desc())
            .all()
        )

        avg_result = (
            db.query(sa_func.avg(Review.rating))
            .filter(Review.event_id == event_id)
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

        products = (
            db.query(EventProduct)
            .filter(EventProduct.event_id == event_id)
            .order_by(EventProduct.created_at)
            .all()
        )

        community_links = (
            db.query(EventCommunityLink)
            .filter(EventCommunityLink.event_id == event_id)
            .order_by(EventCommunityLink.created_at)
            .all()
        )

        return {
            "event": event,
            "venue": venue,
            "reviews": review_dicts,
            "average_rating": round(float(avg_result), 2) if avg_result else None,
            "review_count": len(reviews),
            "products": products,
            "community_links": community_links,
        }

