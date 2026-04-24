"""Neighborhood API endpoints."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import User
from app.models.schemas import (
    Neighborhood,
    NeighborhoodCreate,
    NeighborhoodUpdate
)
from app.services.neighborhood_service import NeighborhoodService
from app.services.venue_service import VenueService
from app.api.dependencies import get_current_user, get_optional_current_user, get_admin_user

router = APIRouter(prefix="/neighborhoods", tags=["neighborhoods"])


@router.get("", response_model=List[Neighborhood], status_code=status.HTTP_200_OK)
async def get_neighborhoods(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    """Get all neighborhoods with pagination."""
    neighborhoods = NeighborhoodService.get_neighborhoods(db, skip=skip, limit=limit)
    return neighborhoods


@router.get("/map", response_model=List[Neighborhood], status_code=status.HTTP_200_OK)
async def get_neighborhoods_by_map_bounds(
    min_lat: float = Query(..., description="Minimum latitude (south boundary)", ge=-90, le=90),
    max_lat: float = Query(..., description="Maximum latitude (north boundary)", ge=-90, le=90),
    min_lon: float = Query(..., description="Minimum longitude (west boundary)", ge=-180, le=180),
    max_lon: float = Query(..., description="Maximum longitude (east boundary)", ge=-180, le=180),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get neighborhoods within a geographic bounding box.

    Returns neighborhoods with coordinates within the specified map bounds.
    Requires all four boundary parameters (min_lat, max_lat, min_lon, max_lon).
    """
    # Validate bounds
    if min_lat >= max_lat:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_lat must be less than max_lat"
        )

    if min_lon >= max_lon:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_lon must be less than max_lon"
        )

    neighborhoods = NeighborhoodService.get_neighborhoods_by_bounds(
        db,
        min_lat=min_lat,
        max_lat=max_lat,
        min_lon=min_lon,
        max_lon=max_lon,
        skip=skip,
        limit=limit
    )

    return neighborhoods

@router.get("/venue-types", response_model=List[str], status_code=status.HTTP_200_OK)
async def get_all_types_of_venues(
    neighborhood_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all types of venues."""
    return VenueService.get_all_types_of_venues(neighborhood_id, db)


@router.get("/{neighborhood_id}", response_model=Neighborhood, status_code=status.HTTP_200_OK)
async def get_neighborhood(
    neighborhood_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a neighborhood by ID."""
    neighborhood = NeighborhoodService.get_neighborhood(db, neighborhood_id)
    if not neighborhood:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Neighborhood with id {neighborhood_id} not found"
        )
    return neighborhood


@router.post("", response_model=Neighborhood, status_code=status.HTTP_201_CREATED)
async def create_neighborhood(
    neighborhood: NeighborhoodCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Create a new neighborhood."""
    return NeighborhoodService.create_neighborhood(db, neighborhood)


@router.put("/{neighborhood_id}", response_model=Neighborhood, status_code=status.HTTP_200_OK)
async def update_neighborhood(
    neighborhood_id: int,
    neighborhood_update: NeighborhoodUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Update a neighborhood."""
    neighborhood = NeighborhoodService.update_neighborhood(
        db, neighborhood_id, neighborhood_update
    )
    if not neighborhood:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Neighborhood with id {neighborhood_id} not found"
        )
    return neighborhood


@router.delete("/{neighborhood_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_neighborhood(
    neighborhood_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a neighborhood."""
    success = NeighborhoodService.delete_neighborhood(db, neighborhood_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Neighborhood with id {neighborhood_id} not found"
        )
    return None
