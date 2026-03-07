# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Eventify Backend — a FastAPI microservice for event discovery and venue management with geographic filtering. Python 3.11, PostgreSQL 15, SQLAlchemy 2.0, Pydantic v2.

## Development Commands

```bash
# Start development environment (FastAPI + PostgreSQL via Docker)
make dev-build        # Build and start dev containers (port 8000, hot reload)
make up               # Start all services
make dev-down         # Stop dev containers

# Database management
make db-seed          # Seed database from app/db/seed.sql
make db-reset         # Drop and recreate database
make db-clear         # Clear all table data
make db-connect       # Open psql shell to eventify database

# Production
docker-compose up -d api-prod   # Run on port 8001 with 4 workers
```

No test runner, linter, or formatter is currently configured.

## Architecture

**Service Layer Pattern:** `API routers → Services → Database`

- `app/api/` — FastAPI routers (endpoints). Aggregated in `router.py`.
- `app/services/` — Business logic as static methods (EventService, VenueService, NeighborhoodService, SearchService).
- `app/db/models.py` — SQLAlchemy ORM models (Neighborhood → Venue → Event, cascade deletes).
- `app/models/schemas.py` — Pydantic request/response schemas (Base/Create/Update/Response pattern).
- `app/db/base.py` — Database engine, session factory, `get_db()` dependency.
- `app/config.py` — Pydantic Settings with env var support.

## Key Patterns

- **Dependency injection:** All endpoints receive `db: Session = Depends(get_db)`.
- **Pagination:** All list endpoints use `skip`/`limit` query params.
- **Geographic filtering:** Coordinates stored as PostgreSQL arrays. Neighborhoods use 2D arrays for polygons. Bounding box filtering via `coordinate_filter.py` using `generate_subscripts()`.
- **Search:** `GET /api/search` combines venue_type, event_type, event_category, date range, and geographic bounds filters with AND logic.
- **Database migrations:** Always use Alembic for schema changes. Never modify tables directly or use `Base.metadata.create_all()` for new changes. Generate migrations with `alembic revision --autogenerate -m "description"` and apply with `alembic upgrade head`.
- **No authentication** is implemented.

## API Routes

All routes prefixed with `/api/`. Swagger docs at `/docs`, ReDoc at `/redoc`.

- `/api/neighborhoods` — CRUD + `/map` (bounds filter) + `/venue-types`
- `/api/venues` — CRUD with optional neighborhood filter
- `/api/events` — CRUD with optional venue_id and category filters
- `/api/search` — Advanced multi-filter search

## Docker Setup

- `api-dev`: Python 3.11-slim, hot reload, volume mount, port 8000
- `api-prod`: Non-root user, 4 uvicorn workers, port 8001
- `db`: PostgreSQL 15 with health checks, persistent volume
- Network: `eventify-network` (bridge)
- Default DB creds: `eventify`/`eventify` on host `db:5432`
