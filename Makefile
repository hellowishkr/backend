# Wishkr Backend - Development Commands
# Usage: make <command>

.PHONY: dev run tunnel db migrate shell clean lint typecheck docker-up docker-down docker-build docker-logs

# Start development server (hot reload)
dev:
	uvicorn app.main:app --reload --port 8000

# Start production server
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

# Start ngrok tunnel for webhook testing
tunnel:
	ngrok http 8000

# Start cloudflare tunnel (alternative)
tunnel-cf:
	cloudflared tunnel --url http://localhost:8000

# Database migrations
migrate:
	alembic upgrade head

# Create new migration
migration:
	@read -p "Migration message: " msg; \
	alembic revision --autogenerate -m "$$msg"

# Database shell (psql)
db:
	psql postgresql://wishkr:wishkr_dev@localhost:5432/wishkr

# Python shell with app context
shell:
	python3 -c "from app.core.config import settings; from app.db import *; from app.models import *; import asyncio; print('Wishkr shell ready')" -i

# Install dependencies
install:
	pip install -r requirements.txt

# Lint — catch unused/missing imports (F401, F821) and import order (I)
lint:
	ruff check app/ tests/

# Lint and auto-fix what ruff can fix automatically
lint-fix:
	ruff check --fix app/ tests/

# Type check — catch type mismatches and missing attributes before runtime
typecheck:
	mypy app/

# Clean python cache
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Docker — production
docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-build:
	docker compose build --no-cache

docker-logs:
	docker compose logs -f backend

# Run both server and tunnel (in separate terminals)
# Use: make dev (terminal 1) + make tunnel (terminal 2)

# Show all commands
help:
	@echo "Available commands:"
	@echo "  make dev          - Start dev server (port 8000)"
	@echo "  make run          - Start production server"
	@echo "  make tunnel       - Start ngrok tunnel"
	@echo "  make tunnel-cf    - Start cloudflare tunnel"
	@echo "  make migrate      - Run database migrations"
	@echo "  make migration    - Create new migration"
	@echo "  make db           - Open database shell"
	@echo "  make install      - Install dependencies"
	@echo "  make lint         - Check for unused/missing imports"
	@echo "  make lint-fix     - Auto-fix import issues"
	@echo "  make typecheck    - Static type check (mypy)"
	@echo "  make clean        - Clean python cache"
	@echo "  make docker-up    - Start all services (DB + backend)"
	@echo "  make docker-down  - Stop all services"
	@echo "  make docker-build - Rebuild Docker images"
	@echo "  make docker-logs  - Tail backend logs"
