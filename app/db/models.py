"""SQLAlchemy database models."""
import enum

from sqlalchemy import Boolean, Column, Enum, Integer, String, Float, Text, ForeignKey, Time, ARRAY, DateTime, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    """User database model."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(Enum(UserRole), default=UserRole.USER, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    reviews = relationship("Review", back_populates="user", cascade="all, delete-orphan")
    followed_venues = relationship("UserVenueFollow", back_populates="user", cascade="all, delete-orphan")
    saved_events = relationship("UserSavedEvent", back_populates="user", cascade="all, delete-orphan")
    interests = relationship("UserInterest", back_populates="user", cascade="all, delete-orphan")


class Neighborhood(Base):
    """Neighborhood database model."""
    __tablename__ = "neighborhoods"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    coordinates = Column(ARRAY(Float), nullable=False)  # [latitude, longitude]
    fill_color = Column(String(50), nullable=True)
    stroke_color = Column(String(50), nullable=True)
    short_description = Column(Text, nullable=True)
    schedule_open = Column(String(10), nullable=True)
    schedule_close = Column(String(10), nullable=True)
    keywords = Column(ARRAY(String), nullable=True)
    photos = Column(ARRAY(String), nullable=True)
    recommendations = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    venues = relationship("Venue", back_populates="neighborhood", cascade="all, delete-orphan")


class Venue(Base):
    """Venue database model."""
    __tablename__ = "venues"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    venue_type = Column(String(50), nullable=False)  # Restaurant, Bar, Night club, etc.
    description = Column(Text, nullable=True)
    stars = Column(Float, nullable=True)  # Rating out of 10
    coordinates = Column(ARRAY(Float), nullable=False)  # [latitude, longitude]
    schedule = Column(Time, nullable=True)
    city = Column(String(100), nullable=True)
    cover_image_url = Column(String(500), nullable=True)
    profile_image_url = Column(String(500), nullable=True)
    website_url = Column(String(500), nullable=True)
    menu_pdf_url = Column(String(500), nullable=True)
    neighborhood_id = Column(Integer, ForeignKey("neighborhoods.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    neighborhood = relationship("Neighborhood", back_populates="venues")
    events = relationship("Event", back_populates="venue", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="venue", cascade="all, delete-orphan")
    followers = relationship("UserVenueFollow", back_populates="venue", cascade="all, delete-orphan")


class Event(Base):
    """Event database model."""
    __tablename__ = "events"
    
    id = Column(Integer, primary_key=True, index=True)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    type = Column(String(50), nullable=True)  # Tour, Music, Outdoors, Festival
    category = Column(String(100), nullable=True)  # Sports/park, Rock/jazz/etc, Music/art/food
    keywords = Column(ARRAY(String), nullable=True)  # List of keywords
    description = Column(Text, nullable=True)
    price_range = Column(ARRAY(Float), nullable=True)  # [min_price, max_price]
    date = Column(String(50), nullable=False)  # Event date
    time_start = Column(String(10), nullable=True)  # HH:mm
    time_end = Column(String(10), nullable=True)  # HH:mm
    image_url = Column(String(500), nullable=True)
    url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    venue = relationship("Venue", back_populates="events")
    reviews = relationship("Review", back_populates="event", cascade="all, delete-orphan")
    products = relationship("EventProduct", back_populates="event", cascade="all, delete-orphan")
    community_links = relationship("EventCommunityLink", back_populates="event", cascade="all, delete-orphan")
    saved_by = relationship("UserSavedEvent", back_populates="event", cascade="all, delete-orphan")


class EventProduct(Base):
    """Product associated with an event (merch, vinyl, etc.)."""
    __tablename__ = "event_products"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    price = Column(String(50), nullable=True)
    image_url = Column(String(500), nullable=True)
    purchase_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    event = relationship("Event", back_populates="products")


class EventCommunityLink(Base):
    """Community/social link associated with an event."""
    __tablename__ = "event_community_links"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    platform = Column(String(100), nullable=False)
    url = Column(String(500), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    event = relationship("Event", back_populates="community_links")


class Review(Base):
    """Review database model."""
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(
            "(venue_id IS NOT NULL AND event_id IS NULL) OR (venue_id IS NULL AND event_id IS NOT NULL)",
            name="ck_review_venue_or_event"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    user = relationship("User", back_populates="reviews")
    venue = relationship("Venue", back_populates="reviews")
    event = relationship("Event", back_populates="reviews")


class UserVenueFollow(Base):
    """Tracks which venues a user follows."""
    __tablename__ = "user_venue_follows"
    __table_args__ = (
        UniqueConstraint("user_id", "venue_id", name="uq_user_venue_follow"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    venue_id = Column(Integer, ForeignKey("venues.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("User", back_populates="followed_venues")
    venue = relationship("Venue", back_populates="followers")


class UserSavedEvent(Base):
    """Tracks which events a user has saved."""
    __tablename__ = "user_saved_events"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_user_saved_event"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("User", back_populates="saved_events")
    event = relationship("Event", back_populates="saved_by")


class UserInterest(Base):
    """User interest categories and optional subtypes."""
    __tablename__ = "user_interests"
    __table_args__ = (
        UniqueConstraint("user_id", "category", "subtype", name="uq_user_interest"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(100), nullable=False)
    subtype = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    user = relationship("User", back_populates="interests")

