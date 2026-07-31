FROM postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777

# The upstream entrypoint uses gosu only when the container starts as root.
# Wishkr always starts PostgreSQL as its fixed non-root account, so retaining
# that helper would add an unused Go binary (and its vulnerability surface).
RUN rm -f /usr/local/bin/gosu

RUN install -d -o postgres -g postgres -m 0750 /etc/postgresql

COPY --chown=postgres:postgres --chmod=0700 001-wishkr-role-policy.sh /docker-entrypoint-initdb.d/001-wishkr-role-policy.sh
COPY --chown=postgres:postgres --chmod=0600 wishkr-pg_hba.conf /etc/postgresql/wishkr-pg_hba.conf

USER postgres
