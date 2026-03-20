"""User preferences service layer — follows, saves, interests, feed."""
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_

from app.db.models import (
    Event,
    Review,
    UserInterest,
    UserSavedEvent,
    UserVenueFollow,
    Venue,
)


class UserPreferencesService:
    """Service for user preference operations."""

    # ── Venue follows ──────────────────────────────────────────────

    @staticmethod
    def follow_venue(db: Session, user_id: int, venue_id: int) -> bool:
        existing = (
            db.query(UserVenueFollow)
            .filter_by(user_id=user_id, venue_id=venue_id)
            .first()
        )
        if existing:
            return False
        db.add(UserVenueFollow(user_id=user_id, venue_id=venue_id))
        db.commit()
        return True

    @staticmethod
    def unfollow_venue(db: Session, user_id: int, venue_id: int) -> bool:
        row = (
            db.query(UserVenueFollow)
            .filter_by(user_id=user_id, venue_id=venue_id)
            .first()
        )
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    @staticmethod
    def get_followed_venues(db: Session, user_id: int) -> List[Venue]:
        follows = (
            db.query(UserVenueFollow)
            .options(joinedload(UserVenueFollow.venue))
            .filter_by(user_id=user_id)
            .all()
        )
        return [f.venue for f in follows if f.venue]

    @staticmethod
    def is_following_venue(db: Session, user_id: int, venue_id: int) -> bool:
        return (
            db.query(UserVenueFollow)
            .filter_by(user_id=user_id, venue_id=venue_id)
            .first()
            is not None
        )

    # ── Saved events ───────────────────────────────────────────────

    @staticmethod
    def save_event(db: Session, user_id: int, event_id: int) -> bool:
        existing = (
            db.query(UserSavedEvent)
            .filter_by(user_id=user_id, event_id=event_id)
            .first()
        )
        if existing:
            return False
        db.add(UserSavedEvent(user_id=user_id, event_id=event_id))
        db.commit()
        return True

    @staticmethod
    def unsave_event(db: Session, user_id: int, event_id: int) -> bool:
        row = (
            db.query(UserSavedEvent)
            .filter_by(user_id=user_id, event_id=event_id)
            .first()
        )
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    @staticmethod
    def get_saved_events(db: Session, user_id: int) -> List[Event]:
        saves = (
            db.query(UserSavedEvent)
            .options(joinedload(UserSavedEvent.event))
            .filter_by(user_id=user_id)
            .all()
        )
        return [s.event for s in saves if s.event]

    @staticmethod
    def is_event_saved(db: Session, user_id: int, event_id: int) -> bool:
        return (
            db.query(UserSavedEvent)
            .filter_by(user_id=user_id, event_id=event_id)
            .first()
            is not None
        )

    # ── Interests ──────────────────────────────────────────────────

    @staticmethod
    def set_interests(
        db: Session, user_id: int, interests: List[dict]
    ) -> List[UserInterest]:
        db.query(UserInterest).filter_by(user_id=user_id).delete()
        new_rows = []
        for item in interests:
            row = UserInterest(
                user_id=user_id,
                category=item["category"],
                subtype=item.get("subtype"),
            )
            db.add(row)
            new_rows.append(row)
        db.commit()
        for r in new_rows:
            db.refresh(r)
        return new_rows

    @staticmethod
    def get_interests(db: Session, user_id: int) -> List[UserInterest]:
        return (
            db.query(UserInterest).filter_by(user_id=user_id).all()
        )

    @staticmethod
    def has_interests(db: Session, user_id: int) -> bool:
        return (
            db.query(UserInterest).filter_by(user_id=user_id).first()
            is not None
        )

    # ── Notification feed ──────────────────────────────────────────

    @staticmethod
    def get_notification_feed(db: Session, user_id: int) -> dict:
        today = date.today().isoformat()

        # 1) Followed venue events
        followed_venue_ids = [
            r.venue_id
            for r in db.query(UserVenueFollow.venue_id)
            .filter_by(user_id=user_id)
            .all()
        ]
        followed_venue_events: Dict[str, list] = {}
        if followed_venue_ids:
            venues = (
                db.query(Venue)
                .filter(Venue.id.in_(followed_venue_ids))
                .all()
            )
            venue_map = {v.id: v.name for v in venues}
            events_at_venues = (
                db.query(Event)
                .filter(
                    Event.venue_id.in_(followed_venue_ids),
                    Event.date >= today,
                )
                .order_by(Event.date)
                .all()
            )
            grouped: Dict[str, list] = defaultdict(list)
            for ev in events_at_venues:
                name = venue_map.get(ev.venue_id, "Venue")
                grouped[name].append(ev)
            followed_venue_events = dict(grouped)

        # 2) Saved events
        saved_event_ids = [
            r.event_id
            for r in db.query(UserSavedEvent.event_id)
            .filter_by(user_id=user_id)
            .all()
        ]
        saved_events = []
        if saved_event_ids:
            saved_events = (
                db.query(Event)
                .filter(Event.id.in_(saved_event_ids))
                .order_by(Event.date)
                .all()
            )

        # 3) Recommended events
        interests = (
            db.query(UserInterest).filter_by(user_id=user_id).all()
        )
        upcoming = (
            db.query(Event)
            .filter(Event.date >= today)
            .all()
        )
        exclude_ids = set(saved_event_ids)
        scored: List[tuple] = []
        for ev in upcoming:
            if ev.id in exclude_ids:
                continue
            score = 0
            for interest in interests:
                if ev.category and ev.category.lower() == interest.category.lower():
                    if interest.subtype and ev.type and ev.type.lower() == interest.subtype.lower():
                        score += 3
                    elif not interest.subtype:
                        score += 2
            if ev.venue_id in followed_venue_ids:
                score += 1
            if score > 0:
                scored.append((score, ev.date, ev))

        scored.sort(key=lambda x: (-x[0], x[1]))
        recommended_events = [item[2] for item in scored[:10]]

        return {
            "followed_venue_events": followed_venue_events,
            "saved_events": saved_events,
            "recommended_events": recommended_events,
        }

    # ── My reviews ─────────────────────────────────────────────────

    @staticmethod
    def get_my_reviews(db: Session, user_id: int) -> list:
        reviews = (
            db.query(Review)
            .options(joinedload(Review.venue), joinedload(Review.event))
            .filter(Review.user_id == user_id)
            .order_by(Review.created_at.desc())
            .all()
        )
        result = []
        for r in reviews:
            result.append({
                "id": r.id,
                "rating": r.rating,
                "comment": r.comment,
                "venue_id": r.venue_id,
                "event_id": r.event_id,
                "venue_name": r.venue.name if r.venue else None,
                "event_name": r.event.name if r.event else None,
                "created_at": r.created_at,
            })
        return result
