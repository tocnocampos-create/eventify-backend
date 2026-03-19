"""Discover feed endpoint — curated content for the discovery screen."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import User
from app.api.dependencies import get_optional_current_user
from app.models.schemas import DiscoverResponse, EventWithVenue, NearbyVenueItem, Venue as VenueSchema
from app.services.discover_service import DiscoverService

router = APIRouter(prefix="/discover", tags=["discover"])


def _event_with_venue(event) -> dict:
    """Serialize an Event ORM object with its loaded venue relationship."""
    venue_data = None
    if event.venue:
        venue_data = VenueSchema.model_validate(event.venue).model_dump()
    result = EventWithVenue.model_validate(event).model_dump()
    result["venue"] = venue_data
    return result


@router.get("", response_model=DiscoverResponse)
def get_discover_feed(
    lat: Optional[float] = Query(None, description="User latitude"),
    lon: Optional[float] = Query(None, description="User longitude"),
    city: str = Query("Santiago", max_length=100),
    radius_km: float = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    user_id = current_user.id if current_user else None
    feed = DiscoverService.get_discover_feed(
        db=db,
        user_id=user_id,
        lat=lat,
        lon=lon,
        city=city,
        radius_km=radius_km,
    )

    # Serialize events with inline venue
    result = {
        "trending": [_event_with_venue(e) for e in feed["trending"]],
        "today": [_event_with_venue(e) for e in feed["today"]],
        "this_week": [_event_with_venue(e) for e in feed["this_week"]],
        "nearby_venues": [
            NearbyVenueItem(
                venue=VenueSchema.model_validate(item["venue"]),
                distance_km=item["distance_km"],
            )
            for item in feed["nearby_venues"]
        ],
        "popular_categories": feed["popular_categories"],
    }

    if "for_you" in feed:
        result["for_you"] = [_event_with_venue(e) for e in feed["for_you"]]

    return result
