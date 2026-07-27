# Wishkr Backend Deployment

Production deploys now run through GitHub Actions in `.github/workflows/deploy-backend.yml`.

## CI/CD Flow

The workflow:

1. Triggers on push to `main` and `workflow_dispatch`.
2. Runs backend tests before any deploy step.
3. Rsyncs `wishkr-backend/` to `/opt/wishkr/` on the web server and worker server separately.
4. Leaves `.env` in place on each server and never syncs it.
5. Deploys the web server first with `docker compose -f docker-compose.prod-web.yml up -d --build backend`.
6. Verifies `http://127.0.0.1:8000/health` on the web host and `https://api.wishkr.com/health` externally.
7. Deploys the worker server second with `docker compose -f docker-compose.prod-worker.yml up -d --build scheduler`.
8. Never touches the DB server from CI.
9. Never runs `docker compose down` in CI.

### Required GitHub Secrets

- `HETZNER_SSH_KEY`
- `HETZNER_WEB_HOST`
- `HETZNER_WORKER_HOST`
- `HETZNER_DEPLOY_USER`

### Server Roles

- Web/API: `5.223.77.195`
- Worker/Scheduler: `5.223.79.238`
- DB: `5.223.61.116`

The web container startup path runs Alembic as part of its startup command. The worker container must not run Alembic.

## Local Deployment Helpers

The scripts in this directory are still available for manual server setup and troubleshooting, but they are not the production rollout path.
