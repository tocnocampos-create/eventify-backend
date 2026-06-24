"""Application configuration."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore"
    )

    # Application
    APP_NAME: str = "Eventify API"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"

    # JWT
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Database - connection to db container
    DB_HOST: str = "db"  # Service name in docker-compose
    DB_PORT: int = 5432
    DB_USER: str = "eventify"
    DB_PASSWORD: str = "eventify"
    DB_NAME: str = "eventify"
    DATABASE_URL: Optional[str] = None

    # Groq (AI search)
    GROQ_API_KEY: Optional[str] = None

    # CORS — comma-separated list of allowed origins.
    # Use "*" to allow all origins (fine for a public read-only API).
    # Example: "https://app.eventify.cl,https://admin.eventify.cl"
    CORS_ORIGINS: str = "*"

    @property
    def database_url(self) -> str:
        """Construct database URL using db container hostname."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS_ORIGINS into a list."""
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()

