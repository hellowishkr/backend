FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

WORKDIR /app

# Keep the production image minimal. All pinned Python dependencies publish
# wheels for Linux amd64, so compilers and development headers are unnecessary.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
# Refresh the packaging toolchain to fixed releases before resolving the
# application wheels. These pins also replace vulnerable versions vendored by
# the upstream Python image.
RUN pip install --no-cache-dir --upgrade \
        setuptools==83.0.0 \
        wheel==0.47.0 && \
    pip install --no-cache-dir -r requirements.txt

# Run as a fixed, non-root identity. `COPY --chown` avoids a second image layer
# containing root-owned copies of the application.
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser

COPY --chown=appuser:appgroup . .

# Release tag for Sentry — pass via `docker compose build --build-arg GIT_SHA=$(git rev-parse --short HEAD)`.
# Without this, Sentry can still track errors but can't group them by deploy,
# so regressions vs fresh bugs become harder to tell apart.
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1
