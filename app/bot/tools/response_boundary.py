"""Sanitize tool results before they are handed back to the agent.

The raw tool layer can carry internal IDs, debug reasons, and provider details
for logs/tests. The conversational layer should have a stable user-safe field
to speak from instead of paraphrasing internal terminology.
"""

from __future__ import annotations

from typing import Any


_INTERNAL_TERMS = (
    "FriendContext",
    "CelebrantOccasion",
    "celebrant_occasion_id",
    "recipient_occasion_id",
    "occ_",
    "cel_",
)


def _fallback_user_message(result: dict[str, Any]) -> str:
    if result.get("user_message"):
        return str(result["user_message"])
    if result.get("message"):
        text = str(result["message"])
        if not any(term in text for term in _INTERNAL_TERMS):
            return text
    code = str(result.get("error") or "tool_error")
    if code in {"precondition_failed", "invalid_input"}:
        return "I need one more detail before I can do that."
    if code == "brand_not_found":
        return "I could not find that gift option right now."
    if code == "occasion_not_found":
        return "I could not find that occasion in your saved details."
    return "Something went wrong while doing that. Please try again in a moment."


def with_safe_response(result: dict[str, Any]) -> dict[str, Any]:
    """Attach a sanitized response boundary without discarding raw metadata.

    Keeping raw fields for now avoids breaking existing tests and internal
    observability. Agent/prompt code can progressively move to ``safe_response``.
    """
    if "error" not in result:
        return result
    if "safe_response" in result:
        return result
    safe = {
        "error": result.get("error"),
        "user_message": _fallback_user_message(result),
        "retryable": bool(result.get("retryable", False)),
    }
    return {**result, "safe_response": safe}
