FROM postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777

# The upstream entrypoint uses gosu only when the container starts as root.
# Wishkr always starts PostgreSQL as its fixed non-root account, so retaining
# that helper would add an unused Go binary (and its vulnerability surface).
RUN rm -f /usr/local/bin/gosu

USER postgres
