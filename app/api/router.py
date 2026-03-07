"""Main API router that includes all endpoint routers."""
from fastapi import APIRouter
from app.api import auth, config, neighborhoods, venues, events, search, reviews, user_preferences, discover

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(config.router)
api_router.include_router(neighborhoods.router)
api_router.include_router(venues.router)
api_router.include_router(events.router)
api_router.include_router(search.router)
api_router.include_router(reviews.router)
api_router.include_router(user_preferences.router)
api_router.include_router(discover.router)

