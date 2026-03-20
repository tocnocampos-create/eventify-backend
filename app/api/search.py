"""Search API endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import User
from app.models.schemas import SearchResponse
from app.services.search_service import SearchService, SearchFilters, ReturnType
from app.api.dependencies import get_current_user

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse, status_code=status.HTTP_200_OK)
async def search(
    q: Optional[str] = Query(None, min_length=1, max_length=200, description="Text search across event names, descriptions, keywords, and venue names"),
    venue_type: Optional[str] = Query(None, description="Filter by venue type (e.g., 'Bar', 'Club')"),
    event_type: Optional[str] = Query(None, description="Filter by event type (e.g., 'Música', 'Teatro')"),
    event_category: Optional[str] = Query(None, description="Filter by event category (e.g., 'Pop', 'Rock')"),
    keyword_category: Optional[str] = Query(None, description="Filter by pill category key — matches against event keywords array (e.g., 'Nacional', 'Jazz', 'Vida Nocturna')"),
    start_date: Optional[str] = Query(None, description="Filter by event date or start of date range (format: 'YYYY-MM-DD', e.g., '2025-11-15')"),
    end_date: Optional[str] = Query(None, description="End of date range (format: 'YYYY-MM-DD', e.g., '2025-11-20'). Requires start_date."),
    min_lat: Optional[float] = Query(None, description="Minimum latitude (south boundary)", ge=-90, le=90),
    max_lat: Optional[float] = Query(None, description="Maximum latitude (north boundary)", ge=-90, le=90),
    min_lon: Optional[float] = Query(None, description="Minimum longitude (west boundary)", ge=-180, le=180),
    max_lon: Optional[float] = Query(None, description="Maximum longitude (east boundary)", ge=-180, le=180),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return per entity type"),
    return_type: ReturnType = Query(ReturnType.BOTH, description="What to return: 'both', 'events', or 'venues'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    """
    Search venues and events by various filters.
    
    Filters can be combined with AND logic:
    - venue_type: Filter venues by their type
    - event_type: Filter by event type (returns venues with events of this type + all events of this type)
    - event_category: Filter by event category (returns venues with events of this category + all events of this category)
    - start_date: Filter by single event date or start of date range (format: 'YYYY-MM-DD')
    - end_date: End of date range (requires start_date). If both provided, filters events between start_date and end_date (inclusive)
    - Coordinate bounds: Filter by geographic bounding box (requires all four: min_lat, max_lat, min_lon, max_lon)
    - return_type: What to return - 'both' (default), 'events', or 'venues'
    
    Examples:
    - /search?venue_type=Bar → All bars and their events
    - /search?event_type=Música → All music events and venues that have them
    - /search?venue_type=Bar&event_type=Música → Bars that have music events + only music events from those bars
    - /search?start_date=2025-11-15 → All events on that date and their venues
    - /search?start_date=2025-11-15&end_date=2025-11-20 → All events between those dates (inclusive)
    - /search?venue_type=Bar&return_type=venues → Only venues (no events)
    - /search?event_type=Música&return_type=events → Only events (no venues)
    """
    # Validate coordinate bounds if any are provided
    if any([min_lat, max_lat, min_lon, max_lon]):
        if not all([min_lat, max_lat, min_lon, max_lon]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="All coordinate bounds must be provided together: min_lat, max_lat, min_lon, max_lon"
            )
        
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
    
    # Validate date format and range if provided
    if start_date is not None:
        try:
            from datetime import datetime
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_date must be in format 'YYYY-MM-DD' (e.g., '2025-11-15')"
            )
        
        if end_date is not None:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="end_date must be in format 'YYYY-MM-DD' (e.g., '2025-11-20')"
                )
            
            if start_dt > end_dt:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="start_date must be less than or equal to end_date"
                )
    elif end_date is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_date requires start_date to be provided"
        )
    
    # Validate that at least one filter is provided
    if not any([q, venue_type, event_type, event_category, keyword_category, start_date, min_lat]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one filter must be provided: q, venue_type, event_type, event_category, keyword_category, start_date, or coordinate bounds"
        )
    
    # Create SearchFilters object
    filters = SearchFilters(
        q=q,
        venue_type=venue_type,
        event_type=event_type,
        event_category=event_category,
        keyword_category=keyword_category,
        start_date=start_date,
        end_date=end_date,
        min_lat=min_lat,
        max_lat=max_lat,
        min_lon=min_lon,
        max_lon=max_lon,
        skip=skip,
        limit=limit,
        return_type=return_type
    )
    
    # Create SearchService instance with database session
    search_service = SearchService(db=db)
    return search_service.search_by_filters(filters=filters)

