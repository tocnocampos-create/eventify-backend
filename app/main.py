"""FastAPI application entry point."""
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.models.schemas import HealthResponse
from app.config import settings
from app.api.router import api_router
from app.admin.router import router as admin_router

logger = logging.getLogger(__name__)


class HeartbeatFilter(logging.Filter):
    """Suppress access log entries for heartbeat endpoint."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/heartbeat" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(HeartbeatFilter())

app = FastAPI(
    title=settings.APP_NAME,
    description="Eventify Backend API - Events Microservice",
    version=settings.APP_VERSION
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(api_router, prefix="/api")

# Admin panel
app.include_router(admin_router, prefix="/admin")


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "Welcome to Eventify API"}


@app.get("/api/v2/heartbeat")
async def heartbeat():
    """Lightweight heartbeat for dev tooling."""
    return {"status": "ok"}


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint with database connectivity check."""
    from sqlalchemy import text
    from app.db.base import SessionLocal
    
    try:
        # Check database connection
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception as e:
            db_status = f"disconnected: {str(e)}"
        finally:
            db.close()
        
        if db_status == "connected":
            return HealthResponse(
                status="healthy",
                message="API is running and database is connected"
            )
        else:
            return HealthResponse(
                status="degraded",
                message=f"API is running but database is {db_status}"
            )
    except Exception as e:
        return HealthResponse(
            status="unhealthy",
            message=f"Health check failed: {str(e)}"
        )
