"""User preferences API endpoints — follows, saves, interests, feed."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.db.base import get_db
from app.db.models import User, Venue, Event
from app.models.schemas import (
    Event as EventSchema,
    FollowVenueResponse,
    InterestItem,
    NotificationFeed,
    ReviewWithContext,
    SavedEventResponse,
    UserInterestResponse,
    UserInterestsUpdate,
    UserSettings,
    UserSettingsUpdate,
    Venue as VenueSchema,
)
from app.services.user_preferences_service import UserPreferencesService

router = APIRouter(prefix="/me", tags=["user-preferences"])


# ── Venue follows ──────────────────────────────────────────────

@router.post("/venues/{venue_id}/follow", response_model=FollowVenueResponse)
async def follow_venue(
    venue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    venue = db.query(Venue).filter(Venue.id == venue_id).first()
    if not venue:
        raise HTTPException(status_code=404, detail="Venue not found")
    UserPreferencesService.follow_venue(db, current_user.id, venue_id)
    return FollowVenueResponse(venue_id=venue_id, following=True)


@router.delete("/venues/{venue_id}/follow", response_model=FollowVenueResponse)
async def unfollow_venue(
    venue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    UserPreferencesService.unfollow_venue(db, current_user.id, venue_id)
    return FollowVenueResponse(venue_id=venue_id, following=False)


@router.get("/venues/following", response_model=List[VenueSchema])
async def get_following_venues(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return UserPreferencesService.get_followed_venues(db, current_user.id)


@router.get("/venues/{venue_id}/is-following")
async def check_following(
    venue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"following": UserPreferencesService.is_following_venue(db, current_user.id, venue_id)}


# ── Saved events ───────────────────────────────────────────────

@router.post("/events/{event_id}/save", response_model=SavedEventResponse)
async def save_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    UserPreferencesService.save_event(db, current_user.id, event_id)
    return SavedEventResponse(event_id=event_id, saved=True)


@router.delete("/events/{event_id}/save", response_model=SavedEventResponse)
async def unsave_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    UserPreferencesService.unsave_event(db, current_user.id, event_id)
    return SavedEventResponse(event_id=event_id, saved=False)


@router.get("/events/saved", response_model=List[EventSchema])
async def get_saved_events(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return UserPreferencesService.get_saved_events(db, current_user.id)


@router.get("/events/{event_id}/is-saved")
async def check_saved(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"saved": UserPreferencesService.is_event_saved(db, current_user.id, event_id)}


# ── Interests ──────────────────────────────────────────────────

@router.put("/interests", response_model=List[UserInterestResponse])
async def set_interests(
    body: UserInterestsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = UserPreferencesService.set_interests(
        db,
        current_user.id,
        [item.model_dump() for item in body.interests],
    )
    return rows


@router.get("/interests", response_model=List[UserInterestResponse])
async def get_interests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return UserPreferencesService.get_interests(db, current_user.id)


# ── Notification feed ──────────────────────────────────────────

@router.get("/feed", response_model=NotificationFeed)
async def get_feed(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return UserPreferencesService.get_notification_feed(db, current_user.id)


# ── Settings ───────────────────────────────────────────────────

@router.get("/settings", response_model=UserSettings)
async def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return UserSettings(notifications_enabled=current_user.notifications_enabled)


@router.put("/settings", response_model=UserSettings)
async def update_settings(
    body: UserSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.notifications_enabled = body.notifications_enabled
    db.commit()
    db.refresh(current_user)
    return UserSettings(notifications_enabled=current_user.notifications_enabled)


# ── My reviews ─────────────────────────────────────────────────

@router.get("/reviews", response_model=List[ReviewWithContext])
async def get_my_reviews(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return UserPreferencesService.get_my_reviews(db, current_user.id)
