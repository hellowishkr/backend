"""Cron jobs for the wishlist lifecycle.

Two jobs registered with the scheduler:

1. ``send_wishlist_reminders`` — during each user's local 08:00 hour.
   Picks up rows where ``remind_on <= local today`` AND
   ``reminded_at IS NULL``, sends a WhatsApp nudge with the saved URL,
   then stamps ``reminded_at``.

2. ``validate_wishlist_urls`` — daily at 03:00 IST. HEAD-pings URLs
   that haven't been validated in the last 30 days. 4xx/5xx (except
   429/408 transients) marks the row ``unavailable_at = now`` so the
   suggester filters it out. Successful pings stamp
   ``last_validated_at`` so the row drops out of the work queue.

Both jobs are idempotent and safe to retry.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import and_, or_, select, update

from app.db.session import async_session
from app.models import User, WishlistItem
from app.bot.whatsapp_templates import TEMPLATE_WISHLIST_REMINDER, _first_name
from app.services.whatsapp import send_text_or_template
from app.utils.timezones import timezone_for_name

logger = logging.getLogger("wishkr.jobs.wishlist")

# Validation cadence — once per item per 30 days. Cheap because the work
# queue index is partial (unavailable_at IS NULL).
_VALIDATION_AGE = timedelta(days=30)
# Cap a single tick to a small batch so a backlog can't stall startup
# or run past its window. Multiple ticks drain the queue.
_BATCH = 50
_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
# Concurrency cap for HEAD pings — be polite to commerce hosts.
_PING_CONCURRENCY = 8


# ─────────────────────────────────────────────────────────
# Reminder dispatch
# ─────────────────────────────────────────────────────────

async def send_wishlist_reminders(now: datetime | None = None) -> None:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    async with async_session() as db:
        pending_filters = (
            WishlistItem.remind_on.is_not(None),
            WishlistItem.reminded_at.is_(None),
            WishlistItem.gifted_at.is_(None),
            WishlistItem.unavailable_at.is_(None),
        )
        timezone_rows = await db.execute(
            select(User.timezone)
            .join(WishlistItem, WishlistItem.user_id == User.id)
            .where(*pending_filters)
            .distinct()
        )
        local_dates = {}
        for timezone_name in timezone_rows.scalars().all():
            local_now = now_utc.astimezone(timezone_for_name(timezone_name))
            if local_now.hour == 8:
                local_dates[timezone_name] = local_now.date()
        if not local_dates:
            return

        due_timezone_predicates = [
            and_(
                User.timezone == timezone_name,
                WishlistItem.remind_on <= local_date,
            )
            for timezone_name, local_date in local_dates.items()
        ]
        rows = await db.execute(
            select(WishlistItem)
            .join(User, User.id == WishlistItem.user_id)
            .where(
                *pending_filters,
                or_(*due_timezone_predicates),
            )
            .order_by(WishlistItem.remind_on, WishlistItem.id)
            .limit(_BATCH)
        )
        items = list(rows.scalars().all())
        if not items:
            return

        logger.info("wishlist reminders: dispatching %d", len(items))

        # Pre-fetch user phones in one round-trip. Name and last_inbound_at
        # feed the template path when the user's CS window is closed.
        user_ids = {it.user_id for it in items}
        phone_rows = await db.execute(
            select(User.id, User.phone, User.name, User.last_inbound_at)
            .where(User.id.in_(user_ids))
        )
        user_by_id = {row.id: row for row in phone_rows.all()}

        sent_ids: list[str] = []
        for it in items:
            user_row = user_by_id.get(it.user_id)
            if not user_row or not user_row.phone:
                continue
            label = it.title or "your saved item"
            message = (
                f"⏰ Reminder — you asked me to nudge you about *{label}* today.\n\n"
                f"{it.url}\n\n"
                f"Want gift ideas around this? Reply *suggest*."
            )
            try:
                # The template body has no URL slot ({{2}} is the item title);
                # the user gets the link when they reply and the window opens.
                await send_text_or_template(
                    user_row.phone,
                    message,
                    last_inbound_at=user_row.last_inbound_at,
                    template_name=TEMPLATE_WISHLIST_REMINDER,
                    body_params=[_first_name(user_row.name), label],
                )
                sent_ids.append(it.id)
            except Exception:
                logger.exception(
                    "wishlist reminder send failed item=%s phone=%s",
                    it.id, user_row.phone,
                )

        if sent_ids:
            await db.execute(
                update(WishlistItem)
                .where(WishlistItem.id.in_(sent_ids))
                .values(reminded_at=now_utc)
            )
            await db.commit()
            logger.info("wishlist reminders: stamped %d", len(sent_ids))


# ─────────────────────────────────────────────────────────
# URL validation
# ─────────────────────────────────────────────────────────

_ALLOWED_SCHEMES = ("http", "https")
# Cap redirect chains. Validators following arbitrary redirects = an SSRF
# bypass — a public host can 302 to http://169.254.169.254 (AWS metadata)
# or http://10.0.0.1. With max_redirects=1 plus a per-hop IP check below,
# every concrete request the client makes is to a vetted public IP.
_MAX_REDIRECTS = 1


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject loopback, RFC1918, link-local, multicast, cloud metadata, etc."""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _url_resolves_to_public_ip(url: str) -> bool:
    """SSRF guard for a user-supplied URL.

    Allowlists ``http``/``https`` schemes only, then DNS-resolves the host
    and rejects any non-public address. Both A and AAAA records are
    checked — a host with one public and one internal IP fails closed.
    The check is best-effort against DNS-rebinding (a TOCTOU attacker can
    flip the record between this resolve and the actual connect); paired
    with ``follow_redirects=False`` and the per-hop redirect handler below,
    it materially closes the typical SSRF surface for HEAD validation.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not _is_public_ip(ip):
            return False
    return True


async def _safe_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
) -> httpx.Response | None:
    """Walk redirects manually so each hop's IP is re-validated.

    httpx's ``follow_redirects`` follows blindly; with our IP allowlist on
    the initial URL only, a redirect to ``http://10.0.0.1`` would still
    reach the internal host. We cap depth and re-check every Location.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not _url_resolves_to_public_ip(current):
            return None
        r = await client.request(method, current, follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("location")
            if not location:
                return r
            # Resolve against current URL for relative redirects
            current = str(httpx.URL(current).join(location))
            continue
        return r
    return None


async def _ping(client: httpx.AsyncClient, url: str) -> int | None:
    """Return status code, or None on transport error / timeout / SSRF block.

    HEAD first; some sites (Flipkart, Myntra) reject HEAD with 405 — we
    fall back to a single GET that aborts after the headers arrive. Every
    request hop is gated by the IP allowlist in ``_safe_request``.
    """
    try:
        r = await _safe_request(client, "HEAD", url)
        if r is None:
            return None
        if r.status_code == 405:
            r = await _safe_request(client, "GET", url)
            if r is None:
                return None
        return r.status_code
    except (httpx.TimeoutException, httpx.TransportError):
        return None
    except Exception:
        logger.exception("validate ping unhandled error url=%s", url[:80])
        return None


async def validate_wishlist_urls() -> None:
    cutoff = datetime.now(timezone.utc) - _VALIDATION_AGE
    async with async_session() as db:
        rows = await db.execute(
            select(WishlistItem)
            .where(
                WishlistItem.unavailable_at.is_(None),
                # Either never validated, or validated >30d ago.
                (WishlistItem.last_validated_at.is_(None)
                 | (WishlistItem.last_validated_at < cutoff)),
            )
            .limit(_BATCH)
        )
        items = list(rows.scalars().all())
        if not items:
            return

        logger.info("wishlist validation: pinging %d", len(items))

        sem = asyncio.Semaphore(_PING_CONCURRENCY)
        results: dict[str, int | None] = {}

        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "wishkr-link-validator/1.0"},
        ) as client:
            async def _check(item: WishlistItem) -> None:
                async with sem:
                    results[item.id] = await _ping(client, item.url)

            await asyncio.gather(*(_check(it) for it in items))

        now = datetime.now(timezone.utc)
        ok_ids: list[str] = []
        dead_ids: list[str] = []
        for it in items:
            code = results.get(it.id)
            # Treat unknown / transient errors as "try again later".
            if code is None or code in (408, 429, 500, 502, 503, 504):
                continue
            if 200 <= code < 400:
                ok_ids.append(it.id)
            elif code in (404, 410):
                dead_ids.append(it.id)
            else:
                # Other 4xx — don't kill the row, but don't pretend
                # it validated either. Skip; will re-ping next cycle.
                continue

        if ok_ids:
            await db.execute(
                update(WishlistItem)
                .where(WishlistItem.id.in_(ok_ids))
                .values(last_validated_at=now)
            )
        if dead_ids:
            await db.execute(
                update(WishlistItem)
                .where(WishlistItem.id.in_(dead_ids))
                .values(unavailable_at=now, last_validated_at=now)
            )
        if ok_ids or dead_ids:
            await db.commit()
            logger.info(
                "wishlist validation: ok=%d dead=%d skipped=%d",
                len(ok_ids), len(dead_ids),
                len(items) - len(ok_ids) - len(dead_ids),
            )
