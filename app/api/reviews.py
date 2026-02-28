"""Review API endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import User
from app.models import schemas
from app.services.review_service import ReviewService
from app.api.dependencies import get_current_user

router = APIRouter(prefix="/reviews", tags=["reviews"])


def _review_to_response(review) -> dict:
    """Convert a Review ORM object to a response dict with user_name."""
    return {
        "id": review.id,
        "user_id": review.user_id,
        "venue_id": review.venue_id,
        "event_id": review.event_id,
        "rating": review.rating,
        "comment": review.comment,
        "user_name": review.user.full_name if review.user else None,
        "created_at": review.created_at,
    }


@router.get("", response_model=list[schemas.Review], status_code=status.HTTP_200_OK)
async def get_reviews(
    venue_id: Optional[int] = Query(None, description="Filter by venue ID"),
    event_id: Optional[int] = Query(None, description="Filter by event ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get reviews for a venue or event."""
    if venue_id and event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either venue_id or event_id, not both",
        )
    if not venue_id and not event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either venue_id or event_id",
        )
    if venue_id:
        reviews = ReviewService.get_reviews_for_venue(db, venue_id, skip, limit)
    else:
        reviews = ReviewService.get_reviews_for_event(db, event_id, skip, limit)
    return [_review_to_response(r) for r in reviews]


@router.post("", response_model=schemas.Review, status_code=status.HTTP_201_CREATED)
async def create_review(
    review: schemas.ReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a review."""
    db_review = ReviewService.create_review(db, current_user.id, review)
    # Reload with user relationship
    db.refresh(db_review)
    from sqlalchemy.orm import joinedload
    db_review = db.query(db_review.__class__).options(
        joinedload(db_review.__class__.user)
    ).filter_by(id=db_review.id).first()
    return _review_to_response(db_review)


@router.put("/{review_id}", response_model=schemas.Review, status_code=status.HTTP_200_OK)
async def update_review(
    review_id: int,
    review_update: schemas.ReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update own review."""
    review = ReviewService.update_review(db, review_id, current_user.id, review_update)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found or not owned by you",
        )
    from sqlalchemy.orm import joinedload
    from app.db.models import Review as ReviewModel
    review = db.query(ReviewModel).options(
        joinedload(ReviewModel.user)
    ).filter_by(id=review.id).first()
    return _review_to_response(review)


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete own review (admins can delete any)."""
    is_admin = current_user.role == "admin"
    success = ReviewService.delete_review(db, review_id, current_user.id, is_admin)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found or not owned by you",
        )
    return None
