FROM python:3.11-slim

WORKDIR /app

# System deps for asyncpg and runtime health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc curl libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "psycopg[binary]"

COPY . .

# Release tag for Sentry — pass via `docker compose build --build-arg GIT_SHA=$(git rev-parse --short HEAD)`.
# Without this, Sentry can still track errors but can't group them by deploy,
# so regressions vs fresh bugs become harder to tell apart.
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

# Run as non-root user for security
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000
