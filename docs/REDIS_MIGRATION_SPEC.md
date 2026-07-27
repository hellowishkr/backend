# Redis Migration Spec — Rate Limiter + Webhook Idempotency

_Status: spec, not yet implemented._
_Owner: backend._
_Estimated effort: 1.5 days dev + 0.5 day staging soak._

## Why this needs to happen

Two pieces of state live in single-process memory and block horizontal
scaling of the web tier:

1. `app/utils/rate_limit.py` — `RateLimiter` uses a per-process dict.
   With `WEB_WORKERS > 1`, each worker maintains its own bucket, so a
   user could send 2N messages/min by hitting different workers.

2. `app/webhooks/whatsapp.py` — `_SEEN_MESSAGE_IDS` is a per-process
   `OrderedDict` LRU for Meta's 5-second webhook retry dedup. With
   multi-worker, the same Meta retry can land on a different worker
   and be re-processed (= duplicate replies, duplicate DB writes,
   duplicate Gemini cost).

Until both are Redis-backed, `WEB_WORKERS` must stay at 1. Once they
are, the cap moves to ~3 workers (limited by RAM on the 4 GB box).

## The infrastructure already exists

- Redis is running on the DB node (`docker-compose.prod-db.yml`).
- `REDIS_URL` is a required env var in both
  `docker-compose.prod-web.yml` and `docker-compose.prod-worker.yml`.
- No Python client is wired in yet — `pip list | grep redis` returns
  empty in the current container.

## Changes

### 1. Dependency

Add to `requirements.txt`:

```
redis[hiredis]>=5.0.0
```

`hiredis` is the C parser; ~10× faster on the hot path. Worth the
extra MB.

### 2. Client module — `app/utils/redis_client.py`

Single shared async client, created at process startup and reused. No
per-request connection churn.

```python
from redis.asyncio import Redis, ConnectionPool

from app.core.config import settings

_pool: ConnectionPool | None = None


def get_redis() -> Redis:
    """Return the shared async Redis client. Lazy init, single instance."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=20,
            decode_responses=True,
        )
    return Redis(connection_pool=_pool)


async def close_redis() -> None:
    """Called from FastAPI lifespan shutdown."""
    global _pool
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
```

Add `redis_url: str = "redis://localhost:6379/0"` to `Settings` in
`config.py`.

### 3. Rate limiter — replace `app/utils/rate_limit.py`

Use a Lua script for atomic check-and-set (avoid race condition between
ZREMRANGEBYSCORE / ZCARD / ZADD). Pattern:

```lua
-- KEYS[1] = bucket key
-- ARGV[1] = window seconds
-- ARGV[2] = max requests
-- ARGV[3] = now (unix seconds)

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3] - ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
```

The `RateLimiter` class keeps its `is_allowed(key)` signature but
becomes `async def is_allowed(...)`. All callers in `whatsapp.py` are
already inside async handlers, so the migration is a sed.

Drop `cleanup()` and the corresponding cron job in `app/jobs/cleanup_rate_limiters.py`
— Redis EXPIRE handles eviction natively.

### 4. Webhook idempotency — replace `_SEEN_MESSAGE_IDS`

In `app/webhooks/whatsapp.py`, replace:

```python
_SEEN_MESSAGE_IDS: OrderedDict[str, None] = OrderedDict()

def _remember_processed_wamid(wamid: str) -> None:
    # ... LRU insert
```

with:

```python
async def _claim_wamid(wamid: str) -> bool:
    """Returns True if this wamid is newly claimed; False if seen recently.
    SETEX with NX gives us atomic test-and-set."""
    redis = get_redis()
    ok = await redis.set(f"wamid:{wamid}", "1", ex=600, nx=True)
    return bool(ok)
```

Then at the webhook entry point, replace the LRU check with:

```python
if not await _claim_wamid(wamid):
    return Response(status_code=200)  # Meta retry, already processed
```

The `ProcessedMessage` DB table stays as the durable record — Redis is
purely the fast-path retry guard.

### 5. Lifespan wiring — `app/main.py`

In the FastAPI lifespan context manager:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing init ...
    # Eagerly ping Redis so we fail fast on misconfig, not on first request.
    await get_redis().ping()
    try:
        yield
    finally:
        # ... existing shutdown ...
        await close_redis()
```

### 6. Tests

- `tests/test_redis_rate_limiter.py` — exercise the Lua script under
  parallel `asyncio.gather` of 50 calls and verify it never exceeds
  the configured max.
- `tests/test_webhook_idempotency.py` — fire the same Meta payload at
  the webhook twice in parallel; assert only one downstream
  `handle_message` invocation.

Use `fakeredis-py` for test isolation; no live Redis needed in CI.

### 7. Migration steps (production)

1. Merge the dependency + client + tests; deploy to staging with
   `WEB_WORKERS=1` (no behavior change yet — Redis is wired but
   not used).
2. Deploy the rate-limiter swap to staging; smoke-test with
   `scripts/load_test.py`.
3. Deploy the webhook idempotency swap to staging; verify Meta
   retries are deduped via Redis (look for `wamid:` keys in
   `redis-cli MONITOR`).
4. Production deploy in one tick (both changes together).
5. Watch quality metrics for 24h. If clean, bump
   `WEB_WORKERS=2` in `.env.prod-web` and roll the web container.

### 8. Rollback

Keep the old in-memory `RateLimiter` class in a `legacy_` submodule
for one release. If Redis goes down post-cutover, env-flag
`USE_REDIS_LIMITER=false` reverts to the in-memory path within one
deploy. Cleanup happens one release later when stability is proven.

---

## Out of scope

- Moving APScheduler jobstore to Redis (or Postgres). The current
  in-memory jobstore is fine; jobs are cron-defined in code, not
  user-scheduled.
- Distributed locks for tool execution. The agent already uses a
  per-phone DB advisory lock; that's sufficient.
- Caching Gemini responses in Redis. Premature; per-user variance is
  too high to benefit from a shared cache at this scale.
