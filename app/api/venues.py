"""Venue API endpoints."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import User
from app.models.schemas import Venue, VenueCreate, VenueUpdate, VenueDetail
from app.services.venue_service import VenueService
from app.api.dependencies import get_current_user, get_admin_user

router = APIRouter(prefix="/venues", tags=["venues"])


@router.get("", response_model=List[Venue], status_code=status.HTTP_200_OK)
async def get_venues(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return"),
    neighborhood_id: Optional[int] = Query(None, description="Filter by neighborhood ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all venues with optional filtering by neighborhood."""
    venues = VenueService.get_venues(
        db, skip=skip, limit=limit, neighborhood_id=neighborhood_id
    )
    return venues


@router.get("/{venue_id}/detail", response_model=VenueDetail, status_code=status.HTTP_200_OK)
async def get_venue_detail(
    venue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get venue detail with events and reviews."""
    result = VenueService.get_venue_detail(db, venue_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Venue with id {venue_id} not found"
        )
    return result


@router.get("/{venue_id}", response_model=Venue, status_code=status.HTTP_200_OK)
async def get_venue(
    venue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a venue by ID."""
    venue = VenueService.get_venue(db, venue_id)
    if not venue:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Venue with id {venue_id} not found"
        )
    return venue


@router.post("", response_model=Venue, status_code=status.HTTP_201_CREATED)
async def create_venue(
    venue: VenueCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Create a new venue."""
    return VenueService.create_venue(db, venue)


@router.put("/{venue_id}", response_model=Venue, status_code=status.HTTP_200_OK)
async def update_venue(
    venue_id: int,
    venue_update: VenueUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Update a venue."""
    venue = VenueService.update_venue(db, venue_id, venue_update)
    if not venue:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Venue with id {venue_id} not found"
        )
    return venue


@router.delete("/{venue_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_venue(
    venue_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a venue."""
    success = VenueService.delete_venue(db, venue_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Venue with id {venue_id} not found"
        )
    return None
