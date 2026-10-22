"""Model-first conversation wiring; inference quality is checked in live replays."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bot import agent
from app.bot.intent_router import DEGRADED, RouteResult
from app.bot.state_utils import get_state, set_state
from app.models import Order, OrderStatus, User, WishlistItem


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,reply",
    [
        ("menu", "What would you like help with?"),
        ("show my orders", "Which order would you like to discuss?"),
        ("मुझे अपने ऑर्डर की जानकारी चाहिए", "मैं आपके ऑर्डर की जानकारी देख सकती हूँ।"),
        ("ನನ್ನ ಆರ್ಡರ್ ಹಿಂದಿರುಗಿಸಬೇಕು", "ನಿಮ್ಮ ಆರ್ಡರ್ ವಿವರಗಳನ್ನು ನೋಡೋಣ."),
        ("எனது ஆர்டரைத் திருப்பித் தர வேண்டும்", "உங்கள் ஆர்டர் விவரங்களைச் சரிபார்க்கிறேன்."),
        ("माझे नाव पूजा आहे", "पूजा, मी तुमची मदत करते."),
        ("¿Puedes hablar español?", "Sí, podemos hablar en español."),
        ("شكراً، هذا كل شيء", "على الرحب والسعة."),
        ("はい、お願いします", "承知しました。"),
        ("[media:image] ये वाला चाहिए", "मुझे तस्वीर मिली है; कृपया उत्पाद का लिंक भेजें।"),
        ("Can I talk to Karan?", "You can reach the team at hello@wishkr.com."),
        ("thanks", "You're welcome."),
        ("save this https://example.com/product/123", "I can save that link."),
    ],
)
async def test_every_conversational_message_reaches_model(db, model_turn, text, reply):
    db.add(User(id="usr_language", phone="919000000001", name="Buyer"))
    await db.flush()
    seen = model_turn(reply)
    actual = await agent.handle_message(db, "919000000001", text)
    assert actual == reply
    assert seen["messages"][0].endswith(text)


async def test_guest_order_tool_verifies_alias_ownership_then_model_phrases_reply(db, model_turn):
    db.add(User(id="guest", phone="+919000000001", name="Buyer", is_guest=True))
    await db.flush()
    db.add(
        Order(id="ord_owned", user_id="guest", total_amount=Decimal("999"), status=OrderStatus.PAID)
    )
    await db.flush()
    seen = model_turn(
        "आपके ऑर्डर के रिटर्न की जाँच हमारी टीम करेगी।",
        [("get_order_support", {"order_id": "ord_owned"})],
    )
    reply = await agent.handle_message(db, "919000000001", "ये ऑर्डर वापस करना है")
    assert reply.startswith("आपके")
    assert seen["results"][0]["order_verified"]
    assert seen["results"][0]["orders"][0]["status"] == "PAID"
    assert (await db.get(Order, "ord_owned")).status == OrderStatus.PAID


async def test_language_and_name_changes_use_tools_instead_of_english_patterns(db, model_turn):
    db.add(User(id="profile", phone="919000000001", name="Old Name"))
    await db.flush()
    seen = model_turn(
        "नाम और भाषा अपडेट कर दी है।",
        [
            ("register_user", {"name": "पूजा"}),
            ("set_chat_preferences", {"language": "Hindi"}),
        ],
    )
    reply = await agent.handle_message(db, "919000000001", "मेरा नाम पूजा है और हिंदी में बात करो")
    assert reply == "नाम और भाषा अपडेट कर दी है।"
    assert all(r.get("success") for r in seen["results"])
    assert (await db.get(User, "profile")).name == "पूजा"
    assert (await get_state(db, "919000000001"))["preferred_language"] == "Hindi"


async def test_multilingual_link_save_is_performed_only_by_the_called_tool(db, model_turn):
    db.add(User(id="profile", phone="919000000001", name="Buyer"))
    await db.flush()
    seen = model_turn(
        "Enlace guardado.", [("add_to_wishlist", {"url": "https://example.com/product/123"})]
    )
    assert (
        await agent.handle_message(
            db, "919000000001", "Guarda este enlace https://example.com/product/123"
        )
        == "Enlace guardado."
    )
    assert seen["results"][0]["success"]
    assert (await db.scalar(select(WishlistItem))).url == "https://example.com/product/123"


async def test_closure_tool_clears_pending_state_but_preserves_language(db, model_turn):
    await set_state(db, "919000000001", "awaiting", "occasion_slots")
    await set_state(db, "919000000001", "preferred_language", "Marathi")
    model_turn("ठीक आहे.", [("set_chat_preferences", {"end_conversation": True})])
    assert await agent.handle_message(db, "919000000001", "नको, एवढेच") == "ठीक आहे."
    state = await get_state(db, "919000000001")
    assert "awaiting" not in state and state["preferred_language"] == "Marathi"


@pytest.mark.parametrize(
    "text", ["suggest gifts", "मेरे ऑर्डर", "எனது பட்டியல்", "wishlist", "payment", "記念日"]
)
def test_unavailable_router_never_narrows_by_message_language(text):
    tools, _ = agent._select_tools_for_turn("browse", text, DEGRADED)
    assert tools == agent._PHASE_TOOLS["browse"]


def test_payment_budget_priority_uses_model_tool_intent():
    route = RouteResult(
        intent="other", confidence=0.99, source="llm", likely_tools=("get_order_support",)
    )
    assert agent._is_critical_turn("திரும்பப் பெற வேண்டும்", "browse", 0, route)


async def test_multilingual_confirmation_saves_pending_date_via_action_tool(db, model_turn):
    from app.bot.tools.tool_impl.occasions import add_occasion
    from app.models import Occasion, Reminder

    db.add(User(id="profile", phone="919000000001", name="Buyer"))
    await db.flush()
    args = {
        "contact_name": "आशा",
        "occasion_type": "BIRTHDAY",
        "date_day": 8,
        "date_month": 4,
        "date_input_numeric": True,
    }
    assert (await add_occasion(db, "919000000001", args))["error"] == "date_ambiguous"
    seen = model_turn(
        "आशाचा वाढदिवस जतन केला.", [("add_occasion", {**args, "date_confirmed": True})]
    )
    assert (
        await agent.handle_message(db, "919000000001", "होय, आठ एप्रिल बरोबर आहे")
        == "आशाचा वाढदिवस जतन केला."
    )
    assert seen["results"][0]["success"]
    assert (await db.scalar(select(Occasion))).date.month == 4
    assert await db.scalar(select(Reminder.id))
    assert "pending_occasion_confirmation" not in await get_state(db, "919000000001")


async def test_unlimited_budget_is_extracted_by_tool_arguments_and_can_be_replaced(
    db, model_turn, monkeypatch
):
    from app.bot.tools.tool_impl import gifting
    from app.bot.state_utils import update_active_gift_context
    from app.core.config import settings

    monkeypatch.setattr(gifting, "SOURCES", [])
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)
    await update_active_gift_context(
        db, "919000000001", {"purpose": "self", "budget_max": 500}, sync_gift_plan=False
    )
    model_turn(
        "Presupuesto sin límite.",
        [
            (
                "suggest_gifts",
                {"budget_unlimited": True, "recipient_vibe": "wall art", "vibes": ["wall_art"]},
            )
        ],
    )
    assert (
        await agent.handle_message(db, "919000000001", "El precio no importa")
        == "Presupuesto sin límite."
    )
    state = await get_state(db, "919000000001")
    assert (
        state["active_gift_context"]["budget_unlimited"]
        and state["active_gift_context"].get("budget_max") is None
    )
    model_turn(
        "Máximo mil rupias.", [("suggest_gifts", {"budget_max": 1000, "vibes": ["wall_art"]})]
    )
    await agent.handle_message(db, "919000000001", "Ahora el límite es mil rupias")
    state = await get_state(db, "919000000001")
    assert not state["active_gift_context"]["budget_unlimited"] and state["budget"]["max"] == 1000


async def test_preference_reply_tool_uses_semantic_fields_and_checks_pending_owner(
    db, model_turn, monkeypatch
):
    from app.core.config import settings
    from app.models.recipient_preference import PreferenceRequest, RecipientPreference
    from app.bot.tools.tool_impl.recipient_preferences import record_recipient_preferences

    monkeypatch.setattr(settings, "preference_interceptor_enabled", True)
    db.add(
        PreferenceRequest(
            id="req_test",
            recipient_phone="+919000000001",
            requester_phone="919000000002",
            status="pending",
        )
    )
    await db.flush()
    denied = await record_recipient_preferences(
        db, "919000000003", {"raw_response": "मुझे किताबें पसंद हैं"}
    )
    assert denied["error"] == "no_pending_preference_request"
    seen = model_turn(
        "तुमच्या आवडी जतन केल्या.",
        [
            (
                "record_recipient_preferences",
                {
                    "raw_response": "मला पुस्तके आवडतात, कपडे नको",
                    "preferred_categories": ["books"],
                    "disliked_categories": ["clothing"],
                },
            )
        ],
    )
    await agent.handle_message(db, "919000000001", "मला पुस्तके आवडतात, कपडे नको")
    assert seen["results"][0]["saved"]
    pref = await db.scalar(select(RecipientPreference))
    assert pref.preferred_categories == "books" and pref.disliked_categories == "clothing"
    assert (await db.get(PreferenceRequest, "req_test")).status == "responded"


async def test_read_only_support_stays_available_at_registration_cap(db, model_turn, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "launch_user_cap", 1)
    db.add(User(id="existing", phone="919000000002", name="Existing"))
    await db.flush()
    seen = model_turn("Please contact hello@wishkr.com.", [("get_order_support", {})])
    await agent.handle_message(db, "919000000001", "मदद चाहिए")
    assert seen["results"][0]["support_email"] == "hello@wishkr.com"


async def test_inferred_language_hint_reaches_main_model(db, model_turn):
    route = RouteResult(
        "other",
        0.99,
        "llm",
        likely_tools=("get_order_support",),
        slots={"reply_language": "Kannada"},
    )
    seen = model_turn("ನಿಮ್ಮ ಆರ್ಡರ್ ವಿವರಗಳು ಇಲ್ಲಿವೆ.", route=route)
    await agent.handle_message(db, "919000000001", "ನನ್ನ ಆರ್ಡರ್ ಎಲ್ಲಿ?")
    assert 'Reply language for this turn: "Kannada"' in seen["messages"][0]
