# Development stage
FROM python:3.11-slim as dev

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run with hot reload for development
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# Production stage
FROM python:3.11-slim as prod

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Make startup script executable before switching to non-root user
RUN chmod +x start.sh

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Railway injects $PORT at runtime; start.sh reads it (defaults to 8000)
EXPOSE 8000

# start.sh runs: alembic upgrade head → uvicorn on $PORT
CMD ["./start.sh"]

# Cron stage — extends prod, adds Playwright + Chromium for Thelonious scraper.
# The main API image stays lean; only the cron service pays the ~400 MB browser cost.
FROM prod as cron

USER root
RUN pip install --no-cache-dir playwright==1.44.0 && playwright install chromium --with-deps
USER appuser

CMD ["python", "scrapers/run_scrapers_only.py"]

