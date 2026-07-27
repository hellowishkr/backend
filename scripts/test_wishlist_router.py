"""Smoke test for the deterministic wishlist router.

Mocks the wishlist_store boundary so we can run without a DB. Verifies the
router still routes correctly across new/duplicate/limit_reached/show paths.

Run inside docker:
  docker compose exec backend python -m scripts.test_wishlist_router
Or locally (pure Python paths only):
  python -m scripts.test_wishlist_router
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.bot import action_router
from app.bot.canned_responses import (
    PRODUCT_GATE_RESPONSE,
    WISHLIST_ADDED_RESPONSE,
    WISHLIST_AFFIRMATIVE_REPROMPT,
    WISHLIST_DUPLICATE_RESPONSE,
    WISHLIST_EMPTY_RESPONSE,
    WISHLIST_LIMIT_RESPONSE,
)
from app.bot.intent_classifier import IntentResult
from app.bot.wishlist_store import WishlistItemView
from app.models import User


class FakeWorld:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.appended: list[tuple[str, str | None]] = []
        self.next_outcome: str = "new"
        self.items: list[WishlistItemView] = []


async def _run() -> None:
    world = FakeWorld()

    async def fake_get_awaiting(_db, _phone):
        v = world.state.get("awaiting")
        return v if isinstance(v, str) else None

    async def fake_set_state(_db, _phone, key, value):
        world.state[key] = value

    async def fake_clear_state(_db, _phone, key):
        world.state.pop(key, None)

    async def fake_append_message(_db, _phone, role, content=None):
        world.appended.append(
            (role.value if hasattr(role, "value") else str(role), content)
        )

    async def fake_add(_db, _phone, _url):
        return world.next_outcome

    async def fake_list(_db, _phone):
        return list(world.items)

    action_router.get_awaiting = fake_get_awaiting  # type: ignore[assignment]
    action_router.set_state = fake_set_state  # type: ignore[assignment]
    action_router.clear_state = fake_clear_state  # type: ignore[assignment]
    action_router.append_message = fake_append_message  # type: ignore[assignment]
    action_router.add_wishlist_product_url = fake_add  # type: ignore[assignment]
    action_router.list_wishlist_items = fake_list  # type: ignore[assignment]

    user = User(id="u1", phone="911", name="Test")
    db = object()
    phone = "911"

    product_intent = IntentResult("product_question", 0.9, "rule")
    affirmative_intent = IntentResult("affirmative", 0.95, "rule")
    other_intent = IntentResult("other", 0.5, "rule")
    show_intent = IntentResult("show_wishlist", 0.95, "rule")

    # 1. "earbuds" → product gate sets awaiting=wishlist_link
    reply = await action_router.try_handle_actions(
        db, phone, "earbuds", user=user, intent_result=product_intent,
    )
    assert reply == PRODUCT_GATE_RESPONSE, reply
    assert world.state.get("awaiting") == "wishlist_link"

    # 2. "yes" while awaiting → re-prompt, awaiting still set
    reply = await action_router.try_handle_actions(
        db, phone, "yes", user=user, intent_result=affirmative_intent,
    )
    assert reply == WISHLIST_AFFIRMATIVE_REPROMPT, reply
    assert world.state.get("awaiting") == "wishlist_link"

    # 3. Product link while awaiting — handled by preflight (before classifier in prod)
    world.next_outcome = "new"
    url = "https://www.amazon.in/dp/B09XYZ1234?ref=foo"
    reply = await action_router.try_handle_product_urls_before_classifier(
        db, phone, f"here it is: {url}",
    )
    assert reply == WISHLIST_ADDED_RESPONSE, reply
    assert "awaiting" not in world.state

    # 4. Re-gate, send same link again, store reports "duplicate"
    await action_router.try_handle_actions(
        db, phone, "earbuds again", user=user, intent_result=product_intent,
    )
    world.next_outcome = "duplicate"
    reply = await action_router.try_handle_product_urls_before_classifier(
        db, phone, url,
    )
    assert reply == WISHLIST_DUPLICATE_RESPONSE, reply

    # 5. Limit reached path
    await action_router.try_handle_actions(
        db, phone, "earbuds again", user=user, intent_result=product_intent,
    )
    world.next_outcome = "limit_reached"
    reply = await action_router.try_handle_product_urls_before_classifier(
        db, phone, url,
    )
    assert reply == WISHLIST_LIMIT_RESPONSE, reply
    assert "awaiting" not in world.state

    # 6. Non-product text while awaiting → router defers (None)
    world.state["awaiting"] = "wishlist_link"
    reply = await action_router.try_handle_actions(
        db, phone, "actually nevermind", user=user, intent_result=other_intent,
    )
    assert reply is None, reply

    # 7. show_wishlist when empty
    world.state.clear()
    world.items = []
    reply = await action_router.try_handle_actions(
        db, phone, "my wishlist", user=user, intent_result=show_intent,
    )
    assert reply == WISHLIST_EMPTY_RESPONSE, reply

    # 8. show_wishlist with items — formatted list
    world.items = [
        WishlistItemView(title="Sony WF-1000XM5", url="https://amazon.in/dp/B09",
                         product_id="amazon:B09"),
        WishlistItemView(title="", url="https://flipkart.com/p/itm123",
                         product_id="flipkart:itm123"),
    ]
    reply = await action_router.try_handle_actions(
        db, phone, "show my wishlist", user=user, intent_result=show_intent,
    )
    assert reply.startswith("Your wishlist (2 items):"), reply
    assert "Sony WF-1000XM5" in reply
    assert "https://flipkart.com/p/itm123" in reply

    # 9. Mixed message — "add" + product URL autosaves without intent / LLM
    world.state.clear()
    world.next_outcome = "new"
    mixed = "bro add this https://www.amazon.in/dp/B09MIXED123 and also what should i gift rahul"
    reply = await action_router.try_handle_product_urls_before_classifier(
        db, phone, mixed,
    )
    assert reply == WISHLIST_ADDED_RESPONSE, reply

    print("OK — wishlist router behaves correctly")


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
