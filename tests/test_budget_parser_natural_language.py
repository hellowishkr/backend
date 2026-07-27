import pytest

from app.bot.state_utils import remember_budget
from app.utils.budget_parser import parse_budget


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Max of 3000", (None, 3000)),
        ("My budget is 3000", (3000, 3000)),
        ("I just told 3000", (3000, 3000)),
        ("I just told you ₹3k", (3000, 3000)),
        ("I can spend 3000 on the gift", (3000, 3000)),
        ("I can spend up to ₹3,000", (None, 3000)),
        ("My budget is 1k to 3k", (1000, 3000)),
        ("No more than 2500", (None, 2500)),
        ("Nothing over 1500", (None, 1500)),
        ("I want to spend not more than ₹2k", (None, 2000)),
        ("2000 and 3000", (2000, 3000)),
        ("2k and 3k", (2000, 3000)),
        ("My budget is ₹2k and ₹3k", (2000, 3000)),
    ],
)
def test_parse_budget_from_natural_sentences(text, expected):
    assert parse_budget(text) == expected


def test_unrelated_sentence_number_is_not_a_budget():
    assert parse_budget("The wedding is on 27 July") == (None, None)
    assert parse_budget("Order 2000 and 3000 was delivered") == (None, None)


@pytest.mark.parametrize(
    "text",
    [
        "Spend 2 hours and 3 days planning",
        "Spend 2 hours planning",
        "Spend 3 days",
        "Spend 2 people on the task",
        "My budget is 2 hours and 3 days",
        "I can spend up to 2 hours",
    ],
)
def test_non_money_counts_are_not_budgets(text):
    assert parse_budget(text) == (None, None)


@pytest.mark.asyncio
async def test_non_money_count_is_not_persisted_as_budget(db):
    stored = await remember_budget(
        db,
        "919999999999",
        "Spend 2 hours and 3 days planning",
    )

    assert stored is None
