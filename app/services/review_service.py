"""Review service layer."""
from typing import List, Optional
from sqlalchemy.orm import Session, joinedload
from app.db.models import Review, User
from app.models.schemas import ReviewCreate, ReviewUpdate


class ReviewService:
    """Service for review operations."""

    @staticmethod
    def get_reviews_for_venue(
        db: Session, venue_id: int, skip: int = 0, limit: int = 50
    ) -> List[Review]:
        return (
            db.query(Review)
            .options(joinedload(Review.user))
            .filter(Review.venue_id == venue_id)
            .order_by(Review.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_reviews_for_event(
        db: Session, event_id: int, skip: int = 0, limit: int = 50
    ) -> List[Review]:
        return (
            db.query(Review)
            .options(joinedload(Review.user))
            .filter(Review.event_id == event_id)
            .order_by(Review.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def create_review(db: Session, user_id: int, review_data: ReviewCreate) -> Review:
        db_review = Review(
            user_id=user_id,
            venue_id=review_data.venue_id,
            event_id=review_data.event_id,
            rating=review_data.rating,
            comment=review_data.comment,
        )
        db.add(db_review)
        db.commit()
        db.refresh(db_review)
        return db_review

    @staticmethod
    def update_review(
        db: Session, review_id: int, user_id: int, review_data: ReviewUpdate
    ) -> Optional[Review]:
        db_review = db.query(Review).filter(
            Review.id == review_id, Review.user_id == user_id
        ).first()
        if not db_review:
            return None
        update_data = review_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_review, field, value)
        db.commit()
        db.refresh(db_review)
        return db_review

    @staticmethod
    def delete_review(db: Session, review_id: int, user_id: int, is_admin: bool = False) -> bool:
        query = db.query(Review).filter(Review.id == review_id)
        if not is_admin:
            query = query.filter(Review.user_id == user_id)
        db_review = query.first()
        if not db_review:
            return False
        db.delete(db_review)
        db.commit()
        return True
