"""SQLAlchemy database models."""
import enum

from sqlalchemy import Boolean, Column, Enum, Integer, String, Float, Text, ForeignKey, Time, ARRAY, DateTime
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

