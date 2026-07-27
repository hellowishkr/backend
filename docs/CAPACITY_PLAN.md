# Wishkr — Capacity Plan

_Last updated: 2026-05-19_

This document is the source of truth for "how much load can the platform
handle right now, and what unblocks the next 10×." Every number here is
derived from real config in the repo, not hand-waved estimates.

---

## 1. Hardware envelope

3 servers, all Hetzner CPX21 (2 vCPU / 4 GB RAM / 80 GB disk):

| Server | Role | Process |
|---|---|---|
| `5.223.61.116` | DB + Redis | Postgres 16 + Redis 7 |
| `5.223.77.195` | Web / API | nginx + uvicorn (FastAPI) |
| `5.223.79.238` | Worker | APScheduler process |

Per-server bandwidth: 1 TB/mo out. Current usage: ~2%. Bandwidth is
not a constraint at the scale we plan for in 2026.

---

## 2. Current code config (post 2026-05-19 throughput PR)

| Knob | Setting | Source |
|---|---|---|
| Reminder cron tick | 5 min | `settings.reminder_tick_minutes` |
| Reminder batch size | 300/tick | `settings.reminder_batch_size` |
| Reminder concurrency | 15 parallel sends | `settings.reminder_concurrency` |
| Reminder operational window | 06:30–22:00 IST | `settings.reminder_window_*_ist` |
| Recipient quiet hours | 06:30–22:00 recipient local time | `settings.reminder_recipient_window_*` |
| Web workers | 1 | `WEB_WORKERS=1` in `.env.prod-web` |
| DB pool per process | 10 + 10 overflow = 20 | `settings.db_pool_size` |
| Postgres max_connections | 100 (default) | needs check on live DB |
| Webhook burst limit | 5 msg/5 s per phone | `_burst_rate_limiter` |
| Webhook in-flight cap | 500 concurrent tasks | `MAX_ACTIVE_TASKS` |
| Agent turn budget | 12 s timeout | `agent_max_tool_rounds=5` |

---

## 3. Theoretical ceilings, post-throughput-PR

### Reminder dispatch (the festival-day question)

Throughput math:

```
sustained_sends_per_sec ≈ reminder_concurrency / avg_meta_send_seconds
                       ≈ 15 / 0.3
                       ≈ 50 sends/sec
```

Per-tick capacity (5-min tick):

```
per_tick = min(reminder_batch_size, sends_per_sec × 300)
         = min(300, 15000)
         = 300
```

The batch size is the binding constraint, not the concurrency. Raising
batch alone past 300 doesn't help until we also raise concurrency (which
is gated by DB pool — see §4).

Daily capacity in active window (15.5 h = 06:30–22:00):

```
daily_max = (15.5 h × 60 min) ÷ 5 min × 300 reminders
          = 186 ticks × 300
          = 55,800 reminders/day theoretical
```

That's the **hardware/code** ceiling. The **Meta tier** ceiling is much
lower until we earn the upgrade (see §6).

### Web tier (inbound WhatsApp turns)

Single worker, async FastAPI, capped at `MAX_ACTIVE_TASKS=500` concurrent
in-flight handlers. Each turn averages ~3–5 s end-to-end (debounce +
Gemini + DB writes + outbound send), so:

```
sustained_turns_per_sec ≈ 500 ÷ 4 ≈ 125 turns/sec hard ceiling
sustained_turns_per_min ≈ 7,500 turns/min
```

Realistic ceiling (90% headroom margin for Gemini quota, DB pool,
backpressure): **~30–80 turns/min sustained**. Above that, expect to
start serving `_BACKPRESSURE_TEXT` to some users.

### DB connections

```
peak_in_flight = web_workers × (pool_size + max_overflow) × 2 engines
               + worker_process × (pool_size + max_overflow)
               = 1 × 20 × 2 + 1 × 20
               = 60 connections peak
```

Against default `max_connections=100`, we have 40 connections of
headroom. After bumping `WEB_WORKERS=2` (post-Redis): 80 peak, still
fits. After bumping to 4: 120 peak — exceeds default. **Plan to raise
Postgres `max_connections=150` before going past 2 web workers.**

---

## 4. Binding constraints, ranked

For a beta cohort during May–August 2026:

1. **Meta messaging tier (currently 250 conversations/24h).** Hard
   external ceiling. Earned, not configured. See §6.
2. **Founder support time.** ~3 hours/day per 100 active users in beta.
3. **Reminder cron throughput.** 55,800/day theoretical post-PR — not
   binding for any cohort we can realistically run before Q4.
4. **Web worker count.** Stays at 1 until Redis migration ships;
   limits us to ~80 turns/min — fine for any cohort under 1,000 users.
5. **DB pool.** 60-conn peak fits the default 100 with margin. Not
   binding before WEB_WORKERS=4.

---

## 5. Cohort-size thresholds

Assumes Option B (`seed` users can fan out reminders; `friend` tier
receives but cannot register). Peak day = a major festival where ~60%
of seeds have an occasion saved.

| Cohort | Peak-day reminders | Meta tier needed | Server-side OK? | Notes |
|---|---|---|---|---|
| 60 seeds | 180 | 250 (current) | ✅ | Recommended phase-1 cohort |
| 100 seeds | 300 | 250 (current) | ✅ | Risky — at the edge of Meta tier |
| 200 seeds | 600 | 2,000 (next tier) | ✅ | Wait for tier upgrade |
| 500 seeds | 1,500 | 2,000 (next tier) | ✅ | Comfortable, post-tier-2 |
| 2,000 seeds | 6,000 | 10,000 (tier-3) | ✅ | Pre-Diwali target |
| 10,000+ | 30,000+ | 100,000 (tier-4) | ⚠️ | Needs Redis + WEB_WORKERS=2 |

---

## 6. Meta tier upgrade path

Current: 250 conversations / 24h.

Next: 2,000 — unlocked automatically after 1,000 *unique customer*
business-initiated conversations in a rolling 7-day window with sustained
"Medium" or "High" quality rating.

Strategy to earn tier-2 inside the beta window:

- Each reminder to a unique friend phone counts.
- 60 seeds × ~5 friends per occasion × ~3 occasions reminded in 30 days
  = ~900 unique recipients. Just short of 1,000.
- Bridge the gap by messaging the existing waitlist (legitimate
  business-initiated welcome, opt-in) to inflate week-1 unique-customer
  count.

Quality rating is the soft constraint — any spike in opt-outs, blocks,
or "report spam" tanks the upgrade path. Protect it by:

- Sending only inside the 06:30–22:00 IST active window (enforced by
  cron — see PR 2026-05-19).
- Never sending the same friend more than one reminder per 24h across
  the platform (already enforced by the cross-celebrant guard in
  `_send_friend_reminder`).
- Honouring opt-outs immediately (already wired through the cancel
  flow).

---

## 7. What needs to ship before the next ceiling

### Before WEB_WORKERS=2 (target: post-50-seed-cohort, ~2 weeks)

- Redis-backed rate limiter (replaces `app/utils/rate_limit.py`).
- Redis-backed webhook idempotency (replaces `_SEEN_MESSAGE_IDS` LRU in
  `app/webhooks/whatsapp.py`).
- Wire `redis.asyncio` client + connection pool. See
  `docs/REDIS_MIGRATION_SPEC.md` for the exact plan.

### Before 2,000 seeds (target: pre-Diwali, October)

- Confirm Meta tier-2 (10,000/24h) unlocked.
- Run `scripts/load_test.py` at 200 concurrent webhook simulation
  against staging and confirm <2% error rate, P95 < 5 s.
- Bump Postgres `max_connections=150` (single ALTER + restart).
- Bump WEB_WORKERS=2 (depends on Redis migration shipping first).

### Before 10,000+ seeds (target: post-Diwali, January 2027)

- Meta tier-3 (100,000/24h).
- Consider upgrading web server to CPX31 (4 vCPU / 8 GB, ~€7/mo extra)
  to support WEB_WORKERS=4 cleanly.
- Move APScheduler jobstore from in-memory to Postgres (so jobs survive
  worker restarts and can be inspected via SQL).

---

## 8. Operational SLO

Targets for the closed beta:

- **Reminder send-to-receive latency:** P95 ≤ 10 min after `scheduled_at`,
  ignoring the quiet-hours window.
- **Inbound message reply latency:** P95 ≤ 8 s for short messages,
  P95 ≤ 15 s for long/complex turns.
- **Bot uptime:** ≥ 99% measured by `/health` endpoint over 30 days.
- **Meta quality rating:** stays "Medium" or "High."

If any of these fall outside targets for 2 consecutive days, pause new
invites and root-cause before continuing.

---

## 9. Single most important metric to monitor

`oldest_pending_reminder_age_minutes` — query against the `reminders`
table:

```sql
SELECT EXTRACT(EPOCH FROM (now() - MIN(scheduled_at))) / 60
FROM reminders
WHERE status = 'pending' AND scheduled_at <= now();
```

If this ever exceeds **30 minutes during the active window**, the cron
isn't keeping up and new invites must pause until it recovers.
