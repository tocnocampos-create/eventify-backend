"""Pydantic schemas for request/response models."""
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional, List, Dict
from datetime import datetime, time


class HealthResponse(BaseModel):
    """Health check response schema."""
    status: str
    message: str


# Neighborhood Schemas
class NeighborhoodBase(BaseModel):
    """Base neighborhood schema."""
    name: str = Field(..., max_length=255)
    description: str
    coordinates: List[List[float]] = Field(..., description="Array of coordinate pairs [[lat, lon], [lat, lon], ...]")
    fill_color: Optional[str] = Field(None, max_length=50)
    stroke_color: Optional[str] = Field(None, max_length=50)
    short_description: Optional[str] = None
    schedule_open: Optional[str] = Field(None, max_length=10)
    schedule_close: Optional[str] = Field(None, max_length=10)
    keywords: Optional[List[str]] = None
    photos: Optional[List[str]] = None
    recommendations: Optional[str] = None


class NeighborhoodCreate(NeighborhoodBase):
    """Schema for creating a neighborhood."""
    pass


class NeighborhoodUpdate(BaseModel):
    """Schema for updating a neighborhood."""
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    coordinates: Optional[List[List[float]]] = None
    fill_color: Optional[str] = Field(None, max_length=50)
    stroke_color: Optional[str] = Field(None, max_length=50)
    short_description: Optional[str] = None
    schedule_open: Optional[str] = Field(None, max_length=10)
    schedule_close: Optional[str] = Field(None, max_length=10)
    keywords: Optional[List[str]] = None
    photos: Optional[List[str]] = None
    recommendations: Optional[str] = None


class Neighborhood(NeighborhoodBase):
    """Neighborhood response schema."""
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Venue Schemas
class VenueBase(BaseModel):
    """Base venue schema."""
    name: str = Field(..., max_length=255)
    venue_type: str = Field(..., max_length=50)
    description: Optional[str] = None
    stars: Optional[float] = Field(None, ge=0, le=10, description="Rating out of 10")
    coordinates: List[float] = Field(..., min_items=2, max_items=2, description="[latitude, longitude]")
    schedule: Optional[time] = None
    city: Optional[str] = Field(None, max_length=100)
    cover_image_url: Optional[str] = Field(None, max_length=500)
    profile_image_url: Optional[str] = Field(None, max_length=500)
    website_url: Optional[str] = Field(None, max_length=500)
    menu_pdf_url: Optional[str] = Field(None, max_length=500)
    neighborhood_id: Optional[int] = None


class VenueCreate(VenueBase):
    """Schema for creating a venue."""
    pass


class VenueUpdate(BaseModel):
    """Schema for updating a venue."""
    name: Optional[str] = Field(None, max_length=255)
    venue_type: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = None
    stars: Optional[float] = Field(None, ge=0, le=10)
    coordinates: Optional[List[float]] = None
    schedule: Optional[time] = None
    city: Optional[str] = Field(None, max_length=100)
    cover_image_url: Optional[str] = Field(None, max_length=500)
    profile_image_url: Optional[str] = Field(None, max_length=500)
    website_url: Optional[str] = Field(None, max_length=500)
    menu_pdf_url: Optional[str] = Field(None, max_length=500)
    neighborhood_id: Optional[int] = None


class Venue(VenueBase):
    """Venue response schema."""
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Event Schemas
class EventBase(BaseModel):
    """Base event schema."""
    name: str = Field(..., max_length=255)
    type: Optional[str] = Field(None, max_length=50)
    category: Optional[str] = Field(None, max_length=100)
    keywords: Optional[List[str]] = None
    description: Optional[str] = None
    price_range: Optional[List[float]] = Field(None, min_items=2, max_items=2, description="[min_price, max_price]")
    date: str = Field(..., max_length=50)
    time_start: Optional[str] = Field(None, max_length=10)
    time_end: Optional[str] = Field(None, max_length=10)
    image_url: Optional[str] = Field(None, max_length=500)
    url: Optional[str] = Field(None, max_length=500)
    venue_id: Optional[int] = None
    
    @field_validator('price_range', mode='before')
    @classmethod
    def convert_empty_price_range_to_none(cls, v):
        """Convert empty arrays to None for price_range."""
        if isinstance(v, list) and len(v) == 0:
            return None
        return v
    
    @field_validator('keywords', mode='before')
    @classmethod
    def convert_empty_keywords_to_none(cls, v):
        """Convert empty arrays to None for keywords."""
        if isinstance(v, list) and len(v) == 0:
            return None
        return v


class EventCreate(EventBase):
    """Schema for creating an event."""
    pass


class EventUpdate(BaseModel):
    """Schema for updating an event."""
    name: Optional[str] = Field(None, max_length=255)
    type: Optional[str] = Field(None, max_length=50)
    category: Optional[str] = Field(None, max_length=100)
    keywords: Optional[List[str]] = None
    description: Optional[str] = None
    price_range: Optional[List[float]] = None
    date: Optional[str] = Field(None, max_length=50)
    time_start: Optional[str] = Field(None, max_length=10)
    time_end: Optional[str] = Field(None, max_length=10)
    image_url: Optional[str] = Field(None, max_length=500)
    url: Optional[str] = Field(None, max_length=500)
    venue_id: Optional[int] = None


class Event(EventBase):
    """Event response schema."""
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Search Response Schemas
# User & Auth Schemas
class UserBase(BaseModel):
    """Base user schema."""
    email: EmailStr
    full_name: Optional[str] = None


class UserCreate(UserBase):
    """Schema for creating a user."""
    password: str = Field(..., min_length=8)


class UserResponse(UserBase):
    """User response schema."""
    id: int
    role: str
    is_active: bool
    notifications_enabled: bool = True
    has_interests: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class UserSettings(BaseModel):
    """User settings response schema."""
    notifications_enabled: bool


class UserSettingsUpdate(BaseModel):
    """Schema for updating user settings."""
    notifications_enabled: bool


class Token(BaseModel):
    """Token response schema."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    """Login request schema."""
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    """Refresh token request schema."""
    refresh_token: str


# Search Response Schemas
class SearchMeta(BaseModel):
    """Metadata for search results."""
    total_venues: int
    total_events: int
    filters_applied: Dict[str, Optional[str]] = Field(..., description="Dictionary of applied filters")


class SearchResponse(BaseModel):
    """Search results response schema."""
    venues: List[Venue]
    events: List[Event]
    meta: SearchMeta


# Review Schemas
class ReviewBase(BaseModel):
    """Base review schema."""
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None


class ReviewCreate(ReviewBase):
    """Schema for creating a review."""
    venue_id: Optional[int] = None
    event_id: Optional[int] = None

    @model_validator(mode="after")
    def check_venue_or_event(self):
        if bool(self.venue_id) == bool(self.event_id):
            raise ValueError("Exactly one of venue_id or event_id must be set")
        return self


class ReviewUpdate(BaseModel):
    """Schema for updating a review."""
    rating: Optional[int] = Field(None, ge=1, le=5)
    comment: Optional[str] = None


class Review(ReviewBase):
    """Review response schema."""
    id: int
    user_id: int
    venue_id: Optional[int] = None
    event_id: Optional[int] = None
    user_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ReviewWithContext(ReviewBase):
    """Review with venue/event name for user history."""
    id: int
    venue_id: Optional[int] = None
    event_id: Optional[int] = None
    venue_name: Optional[str] = None
    event_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# EventProduct Schemas
class EventProductBase(BaseModel):
    """Base event product schema."""
    title: str = Field(..., max_length=255)
    price: Optional[str] = Field(None, max_length=50)
    image_url: Optional[str] = Field(None, max_length=500)
    purchase_url: Optional[str] = Field(None, max_length=500)


class EventProductCreate(EventProductBase):
    """Schema for creating an event product."""
    event_id: int


class EventProductResponse(EventProductBase):
    """Event product response schema."""
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# EventCommunityLink Schemas
class EventCommunityLinkBase(BaseModel):
    """Base event community link schema."""
    platform: str = Field(..., max_length=100)
    url: str = Field(..., max_length=500)


class EventCommunityLinkCreate(EventCommunityLinkBase):
    """Schema for creating an event community link."""
    event_id: int


class EventCommunityLinkResponse(EventCommunityLinkBase):
    """Event community link response schema."""
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Detail Schemas
class VenueDetail(BaseModel):
    """Venue detail response with events and reviews."""
    venue: Venue
    upcoming_events: List[Event]
    past_events: List[Event]
    reviews: List[Review]
    average_rating: Optional[float] = None
    review_count: int


class EventDetail(BaseModel):
    """Event detail response with venue and reviews."""
    event: Event
    venue: Optional[Venue] = None
    reviews: List[Review]
    average_rating: Optional[float] = None
    review_count: int
    products: List[EventProductResponse] = []
    community_links: List[EventCommunityLinkResponse] = []


# User Preferences Schemas
class InterestItem(BaseModel):
    """Single interest input — either a category/subtype row or an exploration_mode row."""
    category: Optional[str] = Field(None, max_length=100)
    subtype: Optional[str] = Field(None, max_length=100)
    exploration_mode: Optional[str] = Field(None, max_length=50)


class UserInterestResponse(BaseModel):
    """Single interest output."""
    id: int
    category: Optional[str] = None
    subtype: Optional[str] = None
    exploration_mode: Optional[str] = None

    class Config:
        from_attributes = True


class UserInterestsUpdate(BaseModel):
    """Bulk replace interests."""
    interests: List[InterestItem]


class FollowVenueResponse(BaseModel):
    """Follow/unfollow confirmation."""
    venue_id: int
    following: bool


class SavedEventResponse(BaseModel):
    """Save/unsave confirmation."""
    event_id: int
    saved: bool


class NotificationFeed(BaseModel):
    """Aggregated notification feed."""
    followed_venue_events: Dict[str, List[Event]]
    saved_events: List[Event]
    recommended_events: List[Event]


# Discover Feed Schemas
class EventWithVenue(Event):
    """Event with inline venue data."""
    venue: Optional[Venue] = None


class NearbyVenueItem(BaseModel):
    """Venue with distance from user."""
    venue: Venue
    distance_km: Optional[float] = None


class PopularCategoryItem(BaseModel):
    """Category with event count."""
    category: str
    event_count: int


class DiscoverResponse(BaseModel):
    """Discover feed response with all sections."""
    trending: List[EventWithVenue]
    today: List[EventWithVenue]
    this_week: List[EventWithVenue]
    nearby_venues: List[NearbyVenueItem]
    popular_categories: List[PopularCategoryItem]
    for_you: Optional[List[EventWithVenue]] = None
