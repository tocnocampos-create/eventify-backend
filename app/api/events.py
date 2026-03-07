"""Event API endpoints."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import User
from app.models.schemas import Event, EventCreate, EventUpdate, EventDetail
from app.services.event_service import EventService
from app.api.dependencies import get_current_user, get_admin_user

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=List[Event], status_code=status.HTTP_200_OK)
async def get_events(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return"),
    venue_id: Optional[int] = Query(None, description="Filter by venue ID"),
    category: Optional[str] = Query(None, description="Filter by category"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all events with optional filtering."""
    events = EventService.get_events(
        db, skip=skip, limit=limit, venue_id=venue_id, category=category
    )
    return events


@router.get("/{event_id}/detail", response_model=EventDetail, status_code=status.HTTP_200_OK)
async def get_event_detail(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get event detail with venue and reviews."""
    result = EventService.get_event_detail(db, event_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event with id {event_id} not found"
        )
    return result


@router.get("/{event_id}", response_model=Event, status_code=status.HTTP_200_OK)
async def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get an event by ID."""
    event = EventService.get_event(db, event_id)
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event with id {event_id} not found"
        )
    return event


@router.post("", response_model=Event, status_code=status.HTTP_201_CREATED)
async def create_event(
    event: EventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Create a new event."""
    return EventService.create_event(db, event)


@router.put("/{event_id}", response_model=Event, status_code=status.HTTP_200_OK)
async def update_event(
    event_id: int,
    event_update: EventUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Update an event."""
    event = EventService.update_event(db, event_id, event_update)
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event with id {event_id} not found"
        )
    return event


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete an event."""
    success = EventService.delete_event(db, event_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event with id {event_id} not found"
        )
    return None
