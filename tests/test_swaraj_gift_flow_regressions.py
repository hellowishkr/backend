"""Regressions from the "Swaraj birthday / BGMI" conversation post-mortem.

The user reported the following bot failures in a single gifting session:
  1. "Recall occasions" first-attempt phrasing failed.
  2. The canned MENU_RESPONSE replaced the gift flow when the user asked
     "Can you suggest me more option like 10/15 options".
  3. Asking for headphone brands while gifting Swaraj pivoted into
     "add it to YOUR wishlist" — wrong purpose.
  4. The gift suggestion vibe taxonomy had no mobile-gaming / BGMI route.

Each test below pins one of those behaviours. They are deliberately
narrow and exercise the deterministic router + classifier paths so they
run without a live Gemini call.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/wishkr_swaraj_test.db")
from app.core.config import settings  # noqa: E402
settings.database_url = "sqlite:////tmp/wishkr_swaraj_test.db"

from app.bot.action_router import (  # noqa: E402
    _has_active_gift_for_friend_flow,
    try_handle_exact_phrases,
    try_handle_product_urls_before_classifier,
    try_handle_actions,
)
from app.bot.canned_responses import MENU_RESPONSE, PRODUCT_GATE_RESPONSE  # noqa: E402
from app.bot.gift_taxonomy import (  # noqa: E402
    OCCASION_DEFAULT_VIBES,
    VIBE_TO_HUBBLE_BRANDS,
    all_vibe_ids,
)
from app.bot.intent_classifier import IntentResult  # noqa: E402
from app.models import Contact, Occasion, OccasionType, User  # noqa: E402
from app.utils.id_gen import generate_id  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# 1. Recall phrases
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "my occasions",
        "show my occasions",
        "list my occasions",
        "recall occasions",
        "recall the occasions",
        "history of my occasions",
        "what occasions have i saved",
        # Long-form entries that already sit in the exact-phrase table.
        "Can u recall whatever occasions I have told you till now",
        "I want history of my occasions I have told you",
    ],
)
@pytest.mark.asyncio
async def test_exact_recall_phrases_answer_without_an_llm(
    db: AsyncSession, text: str,
) -> None:
    """Exact recall phrases stay on the zero-LLM lookup path.

    These are whole-message matches, so no interpretation is needed and the
    turn should cost nothing. Phrasings OUTSIDE this exact set (e.g. "can you
    recall whatever occasions I have told you till now") are deliberately no
    longer keyword-matched — see the test below.
    """
    phone = "+919000000021"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()
    user = (await db.execute(_user_select(phone))).scalar_one()

    with patch("app.bot.action_router.append_message", new=AsyncMock()):
        reply = await try_handle_exact_phrases(db, phone, text, user=user)

    assert reply is not None, f"Expected {text!r} to be answered without an LLM"
    assert "occasion" in reply.lower()


@pytest.mark.parametrize(
    "text",
    [
        "What are my upcoming ocassions?",
        # The 2026-08 bug: a reply to our own question that merely CONTAINS
        # "birthday" must never be treated as a list request.
        "Myra - birthday - 7th Aug",
        "Uttu, birthday, 9th Aug",
    ],
)
@pytest.mark.asyncio
async def test_conversational_phrasings_go_to_the_intent_router(
    db: AsyncSession, text: str,
) -> None:
    """Anything not an exact match must fall through to be understood.

    Previously these were keyword-matched, which is precisely how
    "Myra - birthday - 7th Aug" — an ANSWER to the bot's own question — got
    answered with "I don't have any occasions saved for you yet" while
    occasions existed. The exact-phrase table must not guess; the intent
    router reads these with conversation history.
    """
    phone = "+919000000022"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()
    user = (await db.execute(_user_select(phone))).scalar_one()

    with patch("app.bot.action_router.append_message", new=AsyncMock()) as mock_append:
        reply = await try_handle_exact_phrases(db, phone, text, user=user)

    assert reply is None, (
        f"Expected {text!r} to fall through to the intent router, "
        f"got canned reply: {reply!r}"
    )
    mock_append.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# 2. Menu dump regression
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_more_options_during_gift_flow_does_not_dump_menu(
    db: AsyncSession,
) -> None:
    """A long "...more options..." message must never dump the system menu.

    It used to: an intent label of `menu_request` fired MENU_RESPONSE and blew
    away the recipient + interest + budget context. No label can force that
    now — only an EXACT whole-message match reaches the menu — so this message
    passes through to the agent, which has the flow in view."""
    phone = "+919000000010"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()

    with patch("app.bot.action_router.append_message", new=AsyncMock()) as mock_append:
        reply = await try_handle_exact_phrases(
            db, phone,
            "Can you suggest me more option like 10/15 options",
            user=(await db.get(User, (await db.execute(_user_select(phone))).scalar_one().id)),
        )

    assert reply is None, (
        "Expected the router to pass through to the agent — "
        f"got canned reply: {reply!r}"
    )
    mock_append.assert_not_called()


def _user_select(phone: str):
    from sqlalchemy import select as _select
    return _select(User).where(User.phone == phone)


@pytest.mark.asyncio
async def test_exact_menu_still_returns_canned_menu(db: AsyncSession) -> None:
    """Sanity check: the tightening above must not break the actual
    'menu'/'help' short-circuits."""
    phone = "+919000000011"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()

    user = (
        await db.execute(_user_select(phone))
    ).scalar_one()

    with patch("app.bot.action_router.append_message", new=AsyncMock()):
        reply = await try_handle_exact_phrases(db, phone, "menu", user=user)
    assert reply == MENU_RESPONSE


# ──────────────────────────────────────────────────────────────────────────
# 3. Active gift-for-friend flow detection / product gate suppression
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_product_gate_skipped_when_friend_birthday_within_window(
    db: AsyncSession,
) -> None:
    """User saved Swaraj's birthday today, then asked "Suggest me brands for
    headphones". The cold "add to YOUR wishlist" gate must be suppressed —
    the agent should answer with full recipient context."""
    phone = "+919000000012"
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Swaraj",
        relationship_type="friend",
    )
    today = datetime.now(timezone.utc)
    occ = Occasion(
        id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
        type=OccasionType.BIRTHDAY, date=today, recurring=True,
    )
    db.add_all([user, contact, occ])
    await db.commit()

    rule = IntentResult("product_question", 0.85, "llm")

    with patch("app.bot.action_router.append_message", new=AsyncMock()) as mock_append:
        reply = await try_handle_actions(
            db, phone, "Suggest me brands for headphones",
            user=user, intent_result=rule,
        )

    assert reply is None, (
        "Active gift-for-friend flow must bypass the product gate; "
        f"got canned reply: {reply!r}"
    )
    # The router must not echo the user message into history either —
    # the agent will save it on its own turn.
    mock_append.assert_not_called()


@pytest.mark.asyncio
async def test_product_gate_still_fires_for_cold_user_no_recent_occasion(
    db: AsyncSession,
) -> None:
    """Mirror test: a user without any recent contact-bound occasion
    should still see the product gate when asking 'earbuds pls'."""
    phone = "+919000000013"
    user = User(
        id=generate_id("usr"), phone=phone, name="Solo", city_raw="Pune",
    )
    db.add(user)
    await db.commit()

    rule = IntentResult("product_question", 0.85, "llm")

    with patch("app.bot.action_router.append_message", new=AsyncMock()):
        with patch("app.bot.action_router.set_state", new=AsyncMock()):
            reply = await try_handle_actions(
                db, phone, "earbuds pls",
                user=user, intent_result=rule,
            )

    assert reply == PRODUCT_GATE_RESPONSE


@pytest.mark.asyncio
async def test_active_gift_helper_ignores_far_future_occasions(
    db: AsyncSession,
) -> None:
    """The helper must not flag a contact occasion that's months away as
    "active flow" — otherwise every user with an upcoming friend birthday
    permanently loses the product gate."""
    phone = "+919000000014"
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Faraway",
        relationship_type="friend",
    )
    far = datetime.now(timezone.utc) + timedelta(days=120)
    db.add_all([
        user, contact,
        Occasion(
            id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
            type=OccasionType.BIRTHDAY, date=far, recurring=True,
        ),
    ])
    await db.commit()

    assert await _has_active_gift_for_friend_flow(db, user) is False


# ──────────────────────────────────────────────────────────────────────────
# 4. Gaming/BGMI vibe coverage
# ──────────────────────────────────────────────────────────────────────────


def test_mobile_gaming_vibe_exists_and_is_birthday_default() -> None:
    """Add mobile_gaming so BGMI/Free Fire/COD-mobile recipients map to
    real catalogue brands instead of dropping into fashion/footwear."""
    assert "mobile_gaming" in all_vibe_ids()
    brands = VIBE_TO_HUBBLE_BRANDS["mobile_gaming"]
    # Lower-cased substring matchers used inside the resolver; we just
    # assert presence so a future taxonomy clean-up doesn't regress this.
    assert any("google play" in b.lower() for b in brands)
    assert any("croma" in b.lower() for b in brands)
    assert "mobile_gaming" in OCCASION_DEFAULT_VIBES["birthday"]


def test_gaming_console_includes_playstation_and_xbox() -> None:
    """Steam alone is not enough — console gamers can't redeem Steam keys
    on PS/Xbox."""
    titles = {b.lower() for b in VIBE_TO_HUBBLE_BRANDS["gaming_console"]}
    assert "playstation" in titles
    assert "xbox" in titles


# ──────────────────────────────────────────────────────────────────────────
# 5. Suggest-gifts: BGMI / gaming triggers mobile_gaming + audio_lover
#    and the recommended brands are gaming-relevant (Croma, Amazon, Google
#    Play, boAt/JBL) — never fashion/footwear like Khadims/Madame/Only.
# ──────────────────────────────────────────────────────────────────────────

_BANNED_BGMI_BRANDS = {
    "only", "khadims", "madame", "fitpass", "stylist on whatsapp",
    "beyoung", "vaya", "hamleys",
}

_GAMING_RELEVANT_HINTS = {
    "google play", "croma", "amazon", "flipkart", "reliance digital",
    "boat", "jbl", "steam", "playstation", "xbox",
}


@pytest.mark.asyncio
async def test_suggest_gifts_bgmi_resolves_to_mobile_gaming_brands(
    db: AsyncSession,
) -> None:
    """Run the real suggest_gifts code path with the BGMI/gaming wording
    the user actually typed. The vibe resolver should map to mobile_gaming
    + audio_lover + gaming_console, and the brand fan-out must surface
    only gaming/electronics-relevant titles."""
    from app.bot.tools.tool_impl.gifting import suggest_gifts
    from app.bot.sources import GiftCandidate
    import app.bot.tools.tool_impl.gifting as gifting_mod

    phone = "+919000000020"
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    db.add(user)
    await db.commit()

    # Fake Hubble/vendor catalogue: mix of relevant (Croma, Amazon, boAt,
    # Google Play, Steam) and irrelevant (Khadims, Only, Madame). Whatever
    # comes back should be filtered/ranked by the requested vibes — i.e.
    # only the gaming/electronics brands should match the vibe set.
    relevant_candidates = [
        GiftCandidate(
            source_id="hubble",
            name=name,
            vibes=vibes,
            price_min=200, price_max=10_000,
            fulfillment="instant_voucher",
            already_taken=False,
            item_id=name.lower(),
            contact_info=None,
        )
        for name, vibes in [
            ("Google Play", ["mobile_gaming"]),
            ("Croma", ["mobile_gaming", "mass_electronics", "audio_lover"]),
            ("Amazon", ["mobile_gaming", "mass_electronics", "book_lover"]),
            ("Flipkart", ["mobile_gaming"]),
            ("Reliance Digital", ["mobile_gaming", "mass_electronics"]),
            ("boAt", ["audio_lover", "mobile_gaming"]),
            ("JBL", ["audio_lover"]),
            ("Steam", ["gaming_console"]),
        ]
    ]
    irrelevant_candidates = [
        GiftCandidate(
            source_id="hubble",
            name=name, vibes=["gen_z_streetwear"],  # not in our resolved set
            price_min=500, price_max=5_000,
            fulfillment="instant_voucher",
            already_taken=False,
            item_id=name.lower(),
            contact_info=None,
        )
        for name in ["Khadims", "Only", "Madame", "Hamleys"]
    ]
    all_candidates = relevant_candidates + irrelevant_candidates

    class _FakeSource:
        source_id = "fake_hubble"

        async def list_candidates(self, *, db, vibes, budget_min, budget_max, city):
            # Mimic real Hubble source: return candidates whose vibes
            # intersect the requested set. Irrelevant ones never overlap,
            # so they should not even leave this stub.
            requested = set(vibes)
            return [c for c in all_candidates if set(c.vibes) & requested]

    with patch.object(gifting_mod, "SOURCES", [_FakeSource()]):
        result = await suggest_gifts(
            db, phone,
            {"recipient_vibe": "bgmi mobile gaming", "n": 8},
        )

    assert result.get("success") is True, result
    vibes_used = set(result.get("vibes_used") or [])
    assert "mobile_gaming" in vibes_used, vibes_used

    names = {s["name"].lower() for s in result.get("suggestions", [])}
    # Banned: never recommend fashion/footwear for BGMI.
    for banned in _BANNED_BGMI_BRANDS:
        assert banned not in names, f"BGMI suggestion leaked irrelevant brand: {banned}"
    # Required: at least one gaming/electronics-relevant brand surfaces.
    assert names & _GAMING_RELEVANT_HINTS, names


@pytest.mark.asyncio
async def test_suggest_gifts_he_likes_bgmi_recipient_vibe(
    db: AsyncSession,
) -> None:
    """When the user phrases the interest as 'He likes to play bgmi most',
    the resolver should still pick up mobile_gaming via the keyword map."""
    from app.bot.tools.tool_impl.gifting import suggest_gifts
    from app.bot.sources import GiftCandidate
    import app.bot.tools.tool_impl.gifting as gifting_mod

    phone = "+919000000021"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()

    class _StubSource:
        source_id = "stub"

        async def list_candidates(self, *, db, vibes, **_):
            return [
                GiftCandidate(
                    source_id="stub", name="Croma",
                    vibes=["mobile_gaming", "mass_electronics"],
                    price_min=500, price_max=10_000,
                    fulfillment="instant_voucher", already_taken=False,
                    item_id="croma", contact_info=None,
                ),
            ]

    with patch.object(gifting_mod, "SOURCES", [_StubSource()]):
        result = await suggest_gifts(
            db, phone,
            {"recipient_vibe": "He likes to play bgmi most", "n": 5},
        )

    assert "mobile_gaming" in (result.get("vibes_used") or []), result


@pytest.mark.asyncio
async def test_suggest_gifts_more_avoids_repeating_previous_names(
    db: AsyncSession,
) -> None:
    """The screenshot regression: user says "Gifts", then "More", and the
    second call must not return the exact same top suggestions."""
    from app.bot.tools.tool_impl.gifting import suggest_gifts
    from app.bot.sources import GiftCandidate
    import app.bot.tools.tool_impl.gifting as gifting_mod

    phone = "+919000000029"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()

    class _StubSource:
        source_id = "stub"

        async def list_candidates(self, *, db, vibes, **_):
            return [
                GiftCandidate(
                    source_id="stub",
                    item_id=name.lower().replace(" ", "_"),
                    name=name,
                    vibes=["mobile_gaming"],
                    price_min=500,
                    price_max=5000,
                    fulfillment="instant_voucher",
                )
                for name in [
                    "Google Play",
                    "Croma",
                    "Amazon",
                    "Flipkart",
                    "Reliance Digital",
                    "boAt",
                ]
            ]

    with patch.object(gifting_mod, "SOURCES", [_StubSource()]):
        first = await suggest_gifts(
            db, phone,
            {"recipient_vibe": "mobile gaming", "n": 3},
        )
        second = await suggest_gifts(
            db, phone,
            {"recipient_vibe": "mobile gaming", "n": 3},
        )

    first_names = [s["name"] for s in first["suggestions"]]
    second_names = [s["name"] for s in second["suggestions"]]
    assert set(first_names).isdisjoint(second_names), (first_names, second_names)


@pytest.mark.asyncio
async def test_add_occasion_requires_specific_name_for_cousin(
    db: AsyncSession,
) -> None:
    """Do not save "cousin" as if it were the person's name."""
    from app.bot.tools.tool_impl.occasions import add_occasion

    phone = "+919000000030"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune"))
    await db.commit()

    result = await add_occasion(
        db,
        phone,
        {
            "contact_name": "cousin",
            "occasion_type": "BIRTHDAY",
            "date_day": 2,
            "date_month": 6,
            "relationship": "cousin",
        },
    )

    assert result["error"] == "specific_contact_name_required"
    assert "name" in result["message"].lower()


@pytest.mark.asyncio
async def test_log_my_gift_links_to_contact_by_name(
    db: AsyncSession,
) -> None:
    """Logging a past gift resolves the recipient to an existing contact."""
    from app.bot.tools.tool_impl.gifting import (
        log_my_gift,
        get_past_gifts_for_contact,
    )
    from app.models import Contact

    phone = "+919000000031"
    user_id = generate_id("usr")
    db.add(User(id=user_id, phone=phone, name="Karan", city_raw="Pune"))
    contact = Contact(id=generate_id("con"), user_id=user_id, name="Riya")
    db.add(contact)
    await db.commit()

    result = await log_my_gift(
        db, phone,
        {"description": "wireless headphones", "recipient_name": "Riya", "gift_year": "2025"},
    )
    assert result["success"] is True
    assert result["contact_id"] == contact.id

    past = await get_past_gifts_for_contact(db, user_id, contact.id)
    assert past == [{"description": "wireless headphones", "year": "2025"}]


@pytest.mark.asyncio
async def test_log_my_gift_requires_description(db: AsyncSession) -> None:
    from app.bot.tools.tool_impl.gifting import log_my_gift

    phone = "+919000000032"
    db.add(User(id=generate_id("usr"), phone=phone, name="Karan"))
    await db.commit()

    result = await log_my_gift(db, phone, {"recipient_name": "Riya"})
    assert "error" in result


@pytest.mark.asyncio
async def test_suggest_gifts_surfaces_previously_gifted_for_occasion(
    db: AsyncSession,
) -> None:
    """When the active occasion's contact has logged gifts, suggest_gifts
    returns them under `previously_gifted` so the bot can avoid repeats."""
    from datetime import datetime, timezone
    from app.bot.tools.tool_impl.gifting import suggest_gifts
    from app.bot.state_utils import update_active_gift_context
    from app.bot.sources import GiftCandidate
    from app.models import Contact, Occasion, OccasionType, GiftLog
    import app.bot.tools.tool_impl.gifting as gifting_mod

    phone = "+919000000033"
    user_id = generate_id("usr")
    db.add(User(id=user_id, phone=phone, name="Karan", city_raw="Pune"))
    contact = Contact(id=generate_id("con"), user_id=user_id, name="Riya")
    occasion = Occasion(
        id=generate_id("occ"),
        user_id=user_id,
        contact_id=contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime(2027, 6, 2, 9, tzinfo=timezone.utc),
        recurring=True,
    )
    db.add_all([contact, occasion])
    db.add(GiftLog(
        id=generate_id("glog"),
        user_id=user_id,
        contact_id=contact.id,
        description="a watch",
        gift_year="2024",
    ))
    await db.commit()

    await update_active_gift_context(
        db, phone,
        {
            "recipient_name": "Riya",
            "occasion_id": occasion.id,
            "occasion_type": "Birthday",
            "purpose": "gift_for_friend",
        },
    )
    await db.commit()

    class _StubSource:
        source_id = "stub"

        async def list_candidates(self, *, db, vibes, **_):
            return [
                GiftCandidate(
                    source_id="stub", item_id="boat", name="boAt",
                    vibes=["audio_lover"], price_min=500, price_max=5000,
                    fulfillment="instant_voucher",
                )
            ]

    with patch.object(gifting_mod, "SOURCES", [_StubSource()]):
        result = await suggest_gifts(
            db, phone, {"occasion_id": occasion.id, "recipient_vibe": "audio"},
        )

    assert result.get("previously_gifted") == [{"description": "a watch", "year": "2024"}]


# ──────────────────────────────────────────────────────────────────────────
# 6. dynamic_tail surfaces the recipient name so the agent doesn't lose
#    context after the trim window drops the introduction.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dynamic_tail_surfaces_recipient_and_active_flag(
    db: AsyncSession,
) -> None:
    """After Swaraj's birthday is saved, the prefix prepended to every
    Gemini turn must explicitly name Swaraj and warn the model NOT to
    ask "who is this gift for?" again."""
    from app.bot.agent import _build_dynamic_tail

    phone = "+919000000022"
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Swaraj",
        relationship_type="friend",
    )
    occ = Occasion(
        id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc) + timedelta(days=1),
        recurring=True,
    )
    db.add_all([user, contact, occ])
    await db.commit()

    tail = await _build_dynamic_tail(db, phone, user, celebrant_occasion_id=None)

    assert "Swaraj" in tail, tail
    assert "ActiveRecipient" in tail, tail
    # The exact guidance string is short enough we can check it survives.
    assert "who is this gift for" in tail.lower(), tail


@pytest.mark.asyncio
async def test_dynamic_tail_omits_active_recipient_when_no_friend(
    db: AsyncSession,
) -> None:
    """A user with only self-occasions (no contact_id) must NOT get the
    ActiveRecipient hint — otherwise we'd push the agent into a phantom
    friend flow for solo gifters."""
    from app.bot.agent import _build_dynamic_tail

    phone = "+919000000023"
    user = User(
        id=generate_id("usr"), phone=phone, name="Solo", city_raw="Pune",
    )
    db.add(user)
    db.add(
        Occasion(
            id=generate_id("occ"), user_id=user.id, contact_id=None,
            type=OccasionType.BIRTHDAY,
            date=datetime.now(timezone.utc),
            recurring=True,
        ),
    )
    await db.commit()

    tail = await _build_dynamic_tail(db, phone, user, celebrant_occasion_id=None)
    assert "ActiveRecipient" not in tail, tail


@pytest.mark.asyncio
async def test_dynamic_tail_hides_far_future_next_occasion(
    db: AsyncSession,
) -> None:
    """Do not advertise "upcoming" occasions that are far away (~next year)."""
    from app.bot.agent import _build_dynamic_tail

    phone = "+919000000024"
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Swaraj",
        relationship_type="friend",
    )
    far_date = datetime.now(timezone.utc) + timedelta(days=120)
    db.add_all([user, contact])
    db.add(
        Occasion(
            id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
            type=OccasionType.BIRTHDAY, date=far_date, recurring=True,
        ),
    )
    await db.commit()

    tail = await _build_dynamic_tail(db, phone, user, celebrant_occasion_id=None)

    assert "Next:" not in tail, tail
    assert "ActiveRecipient" not in tail, tail


@pytest.mark.asyncio
async def test_dynamic_tail_next_includes_year_for_nearby_occasion(
    db: AsyncSession,
) -> None:
    """When we do surface "Next", include year to avoid date ambiguity."""
    from app.bot.agent import _build_dynamic_tail

    phone = "+919000000025"
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Swaraj",
        relationship_type="friend",
    )
    near_date = datetime.now(timezone.utc) + timedelta(days=10)
    db.add_all([user, contact])
    db.add(
        Occasion(
            id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
            type=OccasionType.BIRTHDAY, date=near_date, recurring=True,
        ),
    )
    await db.commit()

    tail = await _build_dynamic_tail(db, phone, user, celebrant_occasion_id=None)

    assert "Next: Swaraj's Birthday on " in tail, tail
    assert near_date.strftime("%Y") in tail, tail


# ──────────────────────────────────────────────────────────────────────────
# 7. Internal terms ("Celebrant Occasion", "FriendContext") must not leak
#    in adapter precondition errors that get fed back to the model.
# ──────────────────────────────────────────────────────────────────────────


def test_adapter_precondition_messages_avoid_internal_terms() -> None:
    """The precondition reason/hint strings are mirrored to the LLM and can
    leak through to the user reply. They must speak in friendly terms."""
    from app.bot.tools import adapter

    import inspect

    src = inspect.getsource(adapter)
    # These exact phrasings used to be passed back to the agent as
    # `reason` / `required_action` strings; the user-facing wording is
    # rewritten in the fix.
    banned_phrases = [
        "CelebrantOccasion row",
        "self celebrant occasion",
        "targeted reminder requires an owned celebrant occasion",
        "target_phone is not linked to this CelebrantOccasion",
        "celebrant_occasion_id does not exist",
        "caller is not invited for this celebrant occasion",
        "friend flow requires celebrant_occasion_id",
    ]
    for phrase in banned_phrases:
        assert phrase not in src, (
            f"Adapter still surfaces internal term {phrase!r} — would leak to chat"
        )


# ──────────────────────────────────────────────────────────────────────────
# 8. Wishlist purpose must match active_gift_context (Swaraj step 8).
#    When the user is mid-gift-for-friend, an add_to_wishlist call that
#    omits or contradicts purpose must be REJECTED at the adapter, not
#    silently saved against the caller's personal wishlist.
# ──────────────────────────────────────────────────────────────────────────


async def _seed_user(db: AsyncSession, phone: str) -> User:
    user = User(
        id=generate_id("usr"), phone=phone, name="Karan", city_raw="Pune",
    )
    db.add(user)
    await db.commit()
    return user


async def _own_wishlist_count(db: AsyncSession, user_id: str) -> int:
    from sqlalchemy import select as _select
    from app.models import WishlistItem
    rows = await db.execute(
        _select(WishlistItem).where(WishlistItem.user_id == user_id)
    )
    return len(rows.scalars().all())


@pytest.mark.asyncio
async def test_add_to_wishlist_rejected_when_active_context_is_gift_for_friend(
    db: AsyncSession,
) -> None:
    """Swaraj step 8 — LLM omits purpose, tool would default to 'self'.
    Adapter must reject before the tool runs so the item is never saved
    against the caller's personal wishlist."""
    from app.bot.state_utils import update_active_gift_context
    from app.bot.tools.adapter import execute_tool, reset_context, set_context

    phone = "+919000000080"
    user = await _seed_user(db, phone)
    await update_active_gift_context(
        db, phone,
        {
            "recipient_name": "Swaraj",
            "purpose": "gift_for_friend",
            "occasion_type": "Birthday",
        },
    )

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "add_to_wishlist",
            {"url": "https://www.boat-lifestyle.com/products/airdopes-141"},
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") == "precondition_failed", result
    assert result.get("internal_reason") == "purpose_mismatch_active_context", result
    assert result.get("user_message"), result
    assert "friend" in result["user_message"].lower(), result["user_message"]

    # Critical: no row landed in the caller's personal wishlist.
    assert await _own_wishlist_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_add_to_wishlist_rejected_when_purpose_self_contradicts_active_context(
    db: AsyncSession,
) -> None:
    """Even if the LLM explicitly passes purpose='self', the active context
    wins — gift_for_friend flows cannot be silently downgraded."""
    from app.bot.state_utils import update_active_gift_context
    from app.bot.tools.adapter import execute_tool, reset_context, set_context

    phone = "+919000000081"
    user = await _seed_user(db, phone)
    await update_active_gift_context(
        db, phone,
        {"recipient_name": "Swaraj", "purpose": "gift_for_friend"},
    )

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "add_to_wishlist",
            {
                "url": "https://www.amazon.in/dp/B0BX1L2345",
                "purpose": "self",
            },
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") == "precondition_failed", result
    assert result.get("internal_reason") == "purpose_mismatch_active_context", result
    assert await _own_wishlist_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_add_to_wishlist_passes_when_active_context_is_self(
    db: AsyncSession,
) -> None:
    """Sanity: when the active context is a self-flow, the existing
    self-default path must keep working."""
    from app.bot.state_utils import update_active_gift_context
    from app.bot.tools.adapter import execute_tool, reset_context, set_context

    phone = "+919000000082"
    await _seed_user(db, phone)
    await update_active_gift_context(db, phone, {"purpose": "self"})

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "add_to_wishlist",
            {"url": "https://www.nike.com/in/t/sneaker-1"},
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") != "precondition_failed", result


@pytest.mark.asyncio
async def test_add_to_wishlist_passes_when_no_active_context(
    db: AsyncSession,
) -> None:
    """Sanity: cold users with no active gift context must not be blocked."""
    from app.bot.tools.adapter import execute_tool, reset_context, set_context

    phone = "+919000000083"
    await _seed_user(db, phone)

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "add_to_wishlist",
            {"url": "https://www.amazon.in/dp/B0AAAAAAA1"},
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") != "precondition_failed", result


@pytest.mark.asyncio
async def test_add_to_wishlist_passes_when_purpose_gift_matches_active_context(
    db: AsyncSession,
) -> None:
    """Happy path: gift_for_friend active context + purpose='gift' +
    recipient_occasion_id must clear all preconditions and proceed."""
    from app.bot.state_utils import update_active_gift_context
    from app.bot.tools.adapter import execute_tool, reset_context, set_context

    phone = "+919000000084"
    user = await _seed_user(db, phone)
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Swaraj",
        relationship_type="friend",
    )
    occ = Occasion(
        id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc),
        recurring=True,
    )
    db.add_all([contact, occ])
    await db.commit()

    await update_active_gift_context(
        db, phone,
        {
            "recipient_name": "Swaraj",
            "purpose": "gift_for_friend",
            "occasion_id": occ.id,
        },
    )

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "add_to_wishlist",
            {
                "url": "https://www.croma.com/headphones",
                "purpose": "gift",
                "recipient_occasion_id": occ.id,
            },
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") != "precondition_failed", result
    assert result.get("success") is True, result


@pytest.mark.asyncio
async def test_router_product_url_save_uses_active_friend_context(
    db: AsyncSession,
) -> None:
    """The pre-classifier URL saver bypasses ADK, so it must still honor
    active_gift_context and save a friend gift idea against the recipient
    occasion instead of silently creating a self wishlist item."""
    from sqlalchemy import select as _select
    from app.bot.state_utils import update_active_gift_context
    from app.models import WishlistItem

    phone = "+919000000085"
    user = await _seed_user(db, phone)
    contact = Contact(
        id=generate_id("con"), user_id=user.id, name="Swaraj",
        relationship_type="friend",
    )
    occ = Occasion(
        id=generate_id("occ"), user_id=user.id, contact_id=contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc),
        recurring=True,
    )
    db.add_all([contact, occ])
    await db.commit()

    await update_active_gift_context(
        db,
        phone,
        {
            "recipient_name": "Swaraj",
            "purpose": "gift_for_friend",
            "occasion_id": occ.id,
        },
    )

    reply = await try_handle_product_urls_before_classifier(
        db,
        phone,
        "save this https://www.amazon.in/dp/B0AAAAAAA1",
    )

    assert reply == "Added to wishlist 👍"
    rows = (
        await db.execute(_select(WishlistItem).where(WishlistItem.user_id == user.id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].purpose == "gift"
    assert rows[0].recipient_occasion_id == occ.id


@pytest.mark.asyncio
async def test_recipient_preference_request_requires_permission(
    db: AsyncSession,
) -> None:
    """Recipient messaging is a runtime permission boundary, not a prompt
    instruction. Direct tool calls without explicit permission must be
    stopped before any WhatsApp message path can run."""
    from app.bot.tools.adapter import execute_tool, reset_context, set_context

    phone = "+919000000086"
    await _seed_user(db, phone)

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "request_recipient_preferences",
            {"recipient_phone": "9359615424"},
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") == "precondition_failed", result
    assert result.get("internal_reason") == "recipient_message_permission_missing", result
    assert "confirm" in result.get("user_message", "").lower()


@pytest.mark.asyncio
async def test_recipient_preference_request_allows_explicit_permission(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the gift-giver has explicitly confirmed permission, the same
    tool call is allowed to enter the recipient preference flow."""
    from app.bot.tools.adapter import execute_tool, reset_context, set_context
    from app.bot.tools.tool_impl import recipient_preferences

    phone = "+919000000087"
    await _seed_user(db, phone)
    monkeypatch.setattr(
        recipient_preferences,
        "_send_preference_request_message",
        AsyncMock(return_value=None),
    )

    tokens = set_context(db, phone)
    try:
        result = await execute_tool(
            "request_recipient_preferences",
            {
                "recipient_phone": "9359615424",
                "permission_confirmed": True,
            },
        )
    finally:
        reset_context(*tokens)

    assert result.get("error") != "precondition_failed", result
    assert result.get("status") == "sent", result
