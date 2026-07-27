"""Stable phone number hashing for cost tracking without exposing PII."""

import hashlib


def hash_phone(phone: str) -> str:
    """Return a stable SHA256 hash of phone number (first 16 hex chars).

    Safe for logging/DB storage — cannot be reversed to get the original number.
    Stable across invocations for the same phone.

    Args:
        phone: Raw phone number (e.g., '+919876543210' or '9876543210')

    Returns:
        First 16 chars of SHA256 hash (8 bytes in hex)
    """
    digest = hashlib.sha256(phone.encode()).hexdigest()
    return digest[:16]
