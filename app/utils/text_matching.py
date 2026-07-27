"""Shared normalization and boundary matching for gift-plan text."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def normalize_match_text(value: Any) -> str:
    """Normalize user/catalogue text without weakening word boundaries."""
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def normalize_match_values(values: Iterable[Any] | None) -> list[str]:
    """Normalize non-empty values while preserving their input order."""
    return [
        normalized
        for value in (values or [])
        if (normalized := normalize_match_text(value))
    ]


def _contains_normalized_value(normalized_text: str, normalized_value: str) -> bool:
    if not normalized_value:
        return False
    escaped = re.escape(normalized_value).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){escaped}(?!\w)", normalized_text))


def contains_match_value(text: str, value: str) -> bool:
    """Match a normalized phrase without substring false positives."""
    return _contains_normalized_value(
        normalize_match_text(text),
        normalize_match_text(value),
    )


def count_match_values(text: str, values: Iterable[str] | None) -> int:
    """Count supplied phrases that occur in ``text``."""
    normalized_text = normalize_match_text(text)
    return sum(
        _contains_normalized_value(normalized_text, value)
        for value in normalize_match_values(values)
    )
