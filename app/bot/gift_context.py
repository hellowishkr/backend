"""Typed active gift context for the WhatsApp gifting flow.

The persisted storage is still ``ConversationState.temp_data`` under the
legacy ``active_gift_context`` key. This module is the typed boundary around
that JSON blob so routers, tools, and marketplace services do not each invent
their own dictionary contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

GiftPurpose = Literal["gift_for_friend", "self"]

GIFT_NEED_PROFILE_VERSION = 1


@dataclass(slots=True)
class GiftNeedProfile:
    version: int = GIFT_NEED_PROFILE_VERSION
    recipient_name: str | None = None
    recipient_phone: str | None = None
    recipient_city: str | None = None
    relationship: str | None = None
    occasion_id: str | None = None
    occasion_type: str | None = None
    occasion_date: str | None = None
    purpose: GiftPurpose | None = None
    recipient_vibe: str | None = None
    interest: str | None = None
    gift_category: str | None = None
    selected_brand: str | None = None
    selected_candidate_id: str | None = None
    selected_provider_id: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    current_step: str | None = None
    recipient_message_permission: bool | None = None
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "GiftNeedProfile | None":
        if not isinstance(raw, dict):
            return None
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        data = {k: v for k, v in raw.items() if k in allowed and v is not None}
        if not data:
            return None
        data["version"] = int(data.get("version") or GIFT_NEED_PROFILE_VERSION)
        if data.get("purpose") not in {"gift_for_friend", "self", None}:
            data.pop("purpose", None)
        return cls(**data)

    def to_mapping(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }

    def is_recent(self, now: datetime | None = None, ttl_hours: int = 24) -> bool:
        raw = self.updated_at
        if not isinstance(raw, str) or not raw.strip():
            return False
        try:
            updated_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return updated_at >= now - timedelta(hours=ttl_hours)

    @property
    def is_gift_for_friend(self) -> bool:
        return self.purpose == "gift_for_friend"

    @property
    def recipient_label(self) -> str:
        return self.recipient_name or "the recipient"


def parse_gift_need_profile(raw: dict[str, Any] | None) -> GiftNeedProfile | None:
    return GiftNeedProfile.from_mapping(raw)


def gift_need_profile_is_recent(
    profile: GiftNeedProfile | None,
    now: datetime | None = None,
    ttl_hours: int = 24,
) -> bool:
    return bool(profile and profile.is_recent(now=now, ttl_hours=ttl_hours))
