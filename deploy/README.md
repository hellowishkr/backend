# Wishkr Backend Deployment

OVH production uses the immutable single-host release procedure in
[`../ops/deployment/README.md`](../ops/deployment/README.md). The GitHub
workflow is `.github/workflows/deploy-backend.yml`.

## GitHub Actions behavior

- Pushes to `main` run tests but never deploy automatically.
- Production deployment requires a manual workflow dispatch with the explicit
  `DEPLOY_OVH_PRODUCTION` confirmation.
- The runner connects through a dedicated WireGuard peer; public SSH must stay
  closed.
- SSH uses a pinned `known_hosts` entry rather than live `ssh-keyscan` trust.
- A root-owned host helper validates the archive and checksum, builds and scans
  the application image, re-scans the reviewed database image, activates an
  immutable release, waits for service health, and runs the runtime validator.
- The workflow targets one OVH host and never contacts a Hetzner server.

### Required `production` environment secrets

- `OVH_SSH_KEY`
- `OVH_HOST` — the OVH WireGuard address, not its public address
- `OVH_DEPLOY_USER`
- `OVH_SSH_KNOWN_HOSTS`
- `OVH_WIREGUARD_CONFIG` — a dedicated CI peer, never a personal peer

Do not paste secret values into source control, workflow logs, tickets, or
chat. The old `HETZNER_*` secrets must remain only until the new path passes a
controlled deployment, then be deleted from the GitHub environment.

## Host prerequisite

Install the reviewed helper as a root-owned executable before enabling the
workflow:

```bash
sudo install -o root -g root -m 0755 \
  ops/deployment/deploy-release.sh \
  /usr/local/sbin/wishkr-deploy-release
```

The deployment identity must be a dedicated VPN/SSH identity with permission
to run only the installed helper non-interactively. Do not reuse the personal
administrator key or grant the CI identity general-purpose sudo.

## Legacy three-server helpers

`deploy-prod-web.sh` and `deploy-prod-worker.sh` are retained only for the
temporary Hetzner rollback environment. They require an explicit host argument
and contain no default address. They are not the OVH production rollout path.
