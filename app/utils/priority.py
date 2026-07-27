"""Occasion priority scoring — weight by relationship closeness and urgency.

Used by the weekly digest to order occasions most-important-first.
"""

from datetime import date, datetime, timezone


RELATIONSHIP_WEIGHTS: dict[str, int] = {
    "spouse": 10,
    "partner": 10,
    "parent": 9,
    "mother": 9,
    "father": 9,
    "sibling": 7,
    "brother": 7,
    "sister": 7,
    "best_friend": 8,
    "close_friend": 6,
    "friend": 5,
    "cousin": 4,
    "colleague": 3,
    "acquaintance": 2,
}


def relationship_weight(relationship_type: str | None) -> int:
    """Score 1-10 based on relationship closeness."""
    if not relationship_type:
        return 4  # default for unspecified
    key = relationship_type.strip().lower().replace(" ", "_")
    return RELATIONSHIP_WEIGHTS.get(key, 4)


def urgency_weight(
    occasion_date: datetime,
    *,
    reference_date: date | None = None,
) -> float:
    """Higher score for sooner occasions. Decays after ~14 days."""
    if reference_date is not None:
        days_away = (occasion_date.date() - reference_date).days
    else:
        now = datetime.now(timezone.utc)
        days_away = (occasion_date - now).total_seconds() / 86400
    if days_away < 0:
        return 0.0
    if days_away <= 1:
        return 10.0
    if days_away <= 3:
        return 8.0
    if days_away <= 7:
        return 6.0
    if days_away <= 14:
        return 4.0
    return 2.0


def priority_score(
    occasion_date: datetime,
    relationship_type: str | None,
    *,
    reference_date: date | None = None,
) -> float:
    """Combined priority score for ranking occasions in a digest."""
    return relationship_weight(relationship_type) * urgency_weight(
        occasion_date,
        reference_date=reference_date,
    )
