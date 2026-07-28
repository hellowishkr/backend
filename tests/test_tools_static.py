"""Static-analysis regression tests for the schema-driven tool layer.

These run without Gemini and without a DB — they guard the invariants that
the agent layer depends on at runtime. Any change to tool schemas, wrappers,
or phase composition that breaks these invariants fails CI before reaching
production.

Scope: schema well-formedness, schema↔signature alignment, phase selection,
and the no-one-imports-the-old-registry regression.

Full Gemini replay tests are out of scope here — they need live API access
to be meaningful.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest

# Stub ADK's sync DB session service so ``agent`` can import without a DB —
# agent.py instantiates DatabaseSessionService at module load time.
import google.adk.sessions as _adk_sessions


class _StubSessionService:  # noqa: D401 — test stub
    def __init__(self, **_kw):
        pass

    async def list_sessions(self, **_kw):
        return type("SessionList", (), {"sessions": []})()

    async def create_session(self, **_kw):
        return type(
            "Session",
            (),
            {
                "id": "ses_test",
                "last_update_time": 0,
                "events": [],
            },
        )()

    async def delete_session(self, **_kw):
        return None


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import agent  # noqa: E402 — after stub
from app.bot import intent_classifier  # noqa: E402
from app.jobs import send_reminders  # noqa: E402
from app.bot.tools import TOOL_BY_NAME, TOOL_DECLARATIONS, TOOL_SPECS, get_tool_spec  # noqa: E402
from app.bot.tools import adapter  # noqa: E402


# ─────────────────────────────────────────────────────────
# Schema well-formedness
# ─────────────────────────────────────────────────────────

def test_every_tool_has_a_declaration():
    """Every wrapper in adapter.ALL_TOOLS must map to a hand-written schema."""
    for tool in adapter.ALL_TOOLS:
        assert tool.name in TOOL_DECLARATIONS, f"wrapper '{tool.name}' has no declaration"


def test_every_declaration_has_a_wrapper():
    """Every declared tool must have an executable Python wrapper."""
    for name in TOOL_DECLARATIONS:
        assert hasattr(adapter, name), f"declaration '{name}' has no wrapper"


def test_declaration_names_match_callable_names():
    """Wrapper __name__ must equal its declaration name (used by TOOL_BY_NAME)."""
    for name, tool in TOOL_BY_NAME.items():
        assert tool.name == name
        assert tool.func.__name__ == name


def test_declarations_have_description_and_name():
    for name, decl in TOOL_DECLARATIONS.items():
        assert decl.name == name
        assert decl.description and len(decl.description) > 10, f"{name} has weak description"


@pytest.mark.parametrize("name", list(TOOL_DECLARATIONS.keys()))
def test_declaration_parameters_are_well_formed(name):
    decl = TOOL_DECLARATIONS[name]
    params = decl.parameters
    assert params is not None, f"{name} missing parameters"
    assert params.type.value == "OBJECT", f"{name} parameters.type must be OBJECT"
    # required may be empty but must be a list, not None.
    required = params.required or []
    # Every required field must exist in properties.
    props = params.properties or {}
    for r in required:
        assert r in props, f"{name} marks '{r}' required but it's not a declared property"


# ─────────────────────────────────────────────────────────
# Schema ↔ Python signature alignment
# ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", list(TOOL_DECLARATIONS.keys()))
def test_schema_params_match_wrapper_signature(name):
    """Schema property set must equal wrapper parameter set. Prevents the
    drift that happened pre-refactor where Gemini would send args the
    wrapper can't accept (or the wrapper would expect args the schema
    doesn't declare).
    """
    func = getattr(adapter, name)
    sig = inspect.signature(func)
    fn_params = set(sig.parameters)
    schema_params = set(((TOOL_DECLARATIONS[name].parameters.properties) or {}).keys())
    assert schema_params == fn_params, (
        f"{name}: schema_only={schema_params - fn_params} "
        f"fn_only={fn_params - schema_params}"
    )


# ─────────────────────────────────────────────────────────
# Phase selection contract
# ─────────────────────────────────────────────────────────

class _FakeUser:
    """Just enough to satisfy `_pick_phase`'s None-check."""
    phone = "+919999900000"
    name = "Test"


def test_pick_phase_unregistered():
    assert agent._pick_phase(None, None, 0) == "unregistered"


def test_pick_phase_friend_dominates_cart():
    # A friend-flow id must win even if a cart is present from a prior thread.
    assert agent._pick_phase(_FakeUser(), "occ_abc", 3) == "friend"


def test_pick_phase_friend_dominates_unregistered():
    # Friend context overrides lack of registration — the user might be new.
    assert agent._pick_phase(None, "occ_abc", 0) == "friend"


def test_pick_phase_checkout_when_cart_present():
    assert agent._pick_phase(_FakeUser(), None, 1) == "checkout"


def test_pick_phase_browse_default():
    assert agent._pick_phase(_FakeUser(), None, 0) == "browse"


@pytest.mark.parametrize(
    "phase,expected_min,expected_max",
    [
        ("unregistered", 1, 3),
        ("friend", 9, 13),
        # Checkout is now a SUPERSET of browse + clear_cart (see Gap G7 fix).
        ("checkout", 20, 25),
        ("browse", 20, 24),
    ],
)
def test_phase_tool_surface_within_bounds(phase, expected_min, expected_max):
    """Tool counts per phase must stay inside the bands we intentionally set.
    Catches accidental additions that would regress selection accuracy
    (>15 tools in a phase materially degrades Gemini tool selection)."""
    tools = agent._PHASE_TOOLS[phase]
    assert expected_min <= len(tools) <= expected_max, (
        f"phase '{phase}' has {len(tools)} tools, outside [{expected_min},{expected_max}]"
    )


def _rough_token_estimate(text: str) -> int:
    """Conservative estimate for prompt/schema size guardrails in CI."""
    return (len(text) + 3) // 4


def _tool_schema_payload(name: str) -> str:
    decl = TOOL_DECLARATIONS[name]
    params = decl.parameters.model_dump(mode="json") if decl.parameters else {}
    return json.dumps(
        {
            "name": decl.name,
            "description": decl.description,
            "parameters": params,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def test_static_prompt_token_estimate_budget():
    from app.bot.prompt import STATIC_SYSTEM_PROMPT

    est = _rough_token_estimate(STATIC_SYSTEM_PROMPT)
    # Guardrail only: if this balloons, we regress cost/latency quickly.
    # Raised 2000 -> 2200 when the ONBOARDING section was added (re-prompt for
    # name instead of dead-ending new users on the safety deflection).
    # Raised 2200 -> 2300 when the RESPONSE STYLE brevity rules were added
    # (1-2 line replies, one question per message).
    # Raised 2300 -> 2400 for the catalogue_options rule (show name/price/
    # browse_url only, never the vendor). Baseline was flush at 2300, so any
    # added line tripped this; the rule is a leak guard, not a nice-to-have.
    assert est <= 2400, f"static prompt too large: ~{est} tokens"


def test_tool_count_per_phase_expected():
    counts = {phase: len(tools) for phase, tools in agent._PHASE_TOOLS.items()}
    assert counts == {
        "unregistered": 2,
        "friend": 12,
        "checkout": 25,
        "browse": 24,
    }


def test_mvp_phase_tools_hide_marketplace_payment_tools():
    blocked = {
        "select_gift_candidate",
        "create_checkout",
        "search_brands",
        "get_brand_details",
        "suggest_marketplace_gift_card",
        "create_gift_order",
        "create_friend_gift_order",
        "create_batch_gift_orders",
        "add_to_cart",
        "checkout_cart",
        "search_vendors",
        "set_contact_city",
        "list_orders",
        "get_monthly_spend",
        "check_occasion_gifts",
    }
    for phase in ("browse", "checkout", "friend"):
        names = {t.name for t in agent._PHASE_TOOLS[phase]}
        assert not names.intersection(blocked), f"{phase} exposes {names.intersection(blocked)}"


def test_mvp_friend_tools_keep_reminder_flow_only():
    friend_names = {t.name for t in agent._PHASE_TOOLS["friend"]}
    assert "get_celebrant_context" in friend_names
    assert "suggest_gifts" in friend_names
    assert "get_reminder_schedule" in friend_names
    assert "send_reminder_now" in friend_names
    assert "create_friend_gift_order" not in friend_names
    assert "create_checkout" not in friend_names


def test_browse_phase_tool_schema_budget():
    names = [t.name for t in agent._PHASE_TOOLS["browse"]]
    payload = "".join(_tool_schema_payload(name) for name in names)
    est = _rough_token_estimate(payload)
    assert est <= 23500, f"browse schema payload too large: ~{est} tokens"


def test_friend_phase_tool_schema_budget():
    names = [t.name for t in agent._PHASE_TOOLS["friend"]]
    payload = "".join(_tool_schema_payload(name) for name in names)
    est = _rough_token_estimate(payload)
    assert est <= 15000, f"friend schema payload too large: ~{est} tokens"


def test_all_phase_tools_are_registered():
    names = set(TOOL_BY_NAME.keys())
    for phase, tools in agent._PHASE_TOOLS.items():
        for tool in tools:
            assert tool.name in names, f"{phase} includes unregistered tool {tool.name}"


def test_gemini_temperature_setting_is_used_by_agent():
    src = Path("app/bot/agent.py").read_text(encoding="utf-8")
    assert "temperature=settings.gemini_temperature" in src


def test_critical_turn_classifier_protects_payment_paths():
    assert agent._is_critical_turn("checkout payment now", "browse", 0) is True
    assert agent._is_critical_turn("show me gift ideas", "browse", 0) is False
    assert agent._is_critical_turn("anything", "checkout", 0) is True


def test_selective_tool_injection_keeps_minimum_surface():
    tools, subflow = agent._select_tools_for_turn(
        "browse",
        "show my wishlist",
        intent_classifier.IntentResult("show_wishlist", 0.95, "rule"),
    )
    assert subflow in {"wishlist", "default"}
    assert len(tools) >= 3


def test_selective_tool_injection_exposes_gift_plan_updates():
    tools, subflow = agent._select_tools_for_turn(
        "browse",
        "suggest gifts for Swaraj but no clothes",
        intent_classifier.IntentResult("product_question", 0.95, "rule"),
    )
    names = {tool.name for tool in tools}
    assert subflow == "product_discovery"
    assert "save_gift_plan" in names


def test_selective_tool_injection_exposes_celebrant_updates():
    tools, subflow = agent._select_tools_for_turn(
        "browse",
        "remove a friend from my birthday occasion reminders",
        intent_classifier.IntentResult("other", 0.8, "rule"),
    )
    names = {tool.name for tool in tools}
    assert subflow == "occasions"
    assert "update_celebrant_occasion" in names


def test_suggest_gifts_vibes_enum_matches_taxonomy():
    """G9: the enum on suggest_gifts.vibes is auto-derived from
    gift_taxonomy.all_vibe_ids(). Any drift between the two would let
    Gemini pick an ID the server doesn't understand, or silently narrow
    the option set. This test locks them together."""
    from app.bot.gift_taxonomy import all_vibe_ids

    decl = TOOL_DECLARATIONS["suggest_gifts"]
    vibes_prop = decl.parameters.properties["vibes"]
    assert vibes_prop.items is not None, "vibes items schema missing"
    enum = vibes_prop.items.enum or []
    taxonomy = set(all_vibe_ids())
    assert set(enum) == taxonomy, (
        f"vibes enum drifted from taxonomy: "
        f"enum_only={set(enum) - taxonomy} taxonomy_only={taxonomy - set(enum)}"
    )
    assert len(enum) >= 50, "vibes enum suspiciously small"


def test_suggest_gifts_vibes_description_is_substantive():
    """The enum alone lets Gemini pick *some* ID. The description gives
    it a map from natural-language words to IDs so it picks the RIGHT
    ones. Description must be non-trivial and name a sampling of
    canonical IDs so Gemini can anchor."""
    decl = TOOL_DECLARATIONS["suggest_gifts"]
    desc = decl.parameters.properties["vibes"].description or ""
    assert len(desc) > 500, "vibes description too thin to guide Gemini"
    # Spot-check a few IDs get a hint (not every one needs one, but
    # the load-bearing non-obvious ones must).
    for must_have_hint in ["gen_z_streetwear", "ayurvedic_natural", "book_lover", "home_decor_modern"]:
        assert must_have_hint in desc, f"{must_have_hint} should appear in vibes description"


def test_resolve_one_fallback_still_works():
    """Gemini-driven enum is the primary path, but centralized language
    fallback must still map natural tokens like 'fashion', 'cook', and
    'read' to canonical vibe IDs."""
    from app.bot.gift_taxonomy import all_vibe_ids
    from app.bot.language_map import resolve_vibe_keywords

    known = set(all_vibe_ids())
    assert resolve_vibe_keywords(["fashion"], known)
    assert resolve_vibe_keywords(["cook"], known)
    assert resolve_vibe_keywords(["read"], known)

    service_src = Path("app/services/gift_recommendation.py").read_text(encoding="utf-8")
    assert "resolve_vibe_keywords" in service_src


def test_suggest_gifts_keyword_map_targets_real_vibes():
    """Gap G8 regression: the centralized keyword map must only
    map to vibe IDs that actually exist in gift_taxonomy. A dangling
    reference would silently return fewer results than expected."""
    from app.bot.gift_taxonomy import all_vibe_ids
    from app.bot.language_map import VIBE_KEYWORD_MAP

    known = set(all_vibe_ids())
    targets = {target for mapped in VIBE_KEYWORD_MAP.values() for target in mapped}
    invalid = {t for t in targets if t not in known}
    assert not invalid, f"keyword_map references unknown vibe IDs: {invalid}"


def test_suggest_gifts_keyword_map_covers_common_model_vocabulary():
    """The model will emit natural-language vibes like 'cooking', 'reading',
    'married'. If the map doesn't include those, Gemini's `vibes=[...]`
    calls silently resolve to nothing and the turn dead-ends with
    `no_vibes_resolved` (observed on Swaraj chat 24 Apr 12:17 UTC)."""
    from app.bot.language_map import VIBE_KEYWORD_MAP

    # Keys that are load-bearing for real conversations we've seen.
    must_have = ["cook", "read", "married", "fashion", "fitness",
                 "foodie", "book", "kitchen", "home", "tech", "pet"]
    for kw in must_have:
        assert kw in VIBE_KEYWORD_MAP, f"keyword_map missing a key matching '{kw}'"


def test_response_boundary_adds_safe_error_message():
    from app.bot.tools.response_boundary import with_safe_response

    result = with_safe_response({
        "error": "occasion_not_found",
        "message": "Could not find occ_secret_123",
        "internal_reason": "raw occ_ id missing",
    })

    assert result["internal_reason"] == "raw occ_ id missing"
    assert result["safe_response"]["error"] == "occasion_not_found"
    assert "occ_" not in result["safe_response"]["user_message"]


def test_checkout_includes_suggest_gifts_and_browse_superset():
    """Gap G7: a stale cart used to trap users in a narrow checkout phase
    without `suggest_gifts`, and Gemini would call it anyway → ADK raised
    ValueError → "I got a bit stuck" fallback.

    Checkout must now be a superset of browse so a shopper with an open
    cart can still ask for gift ideas.
    """
    browse_names = {t.name for t in agent._PHASE_TOOLS["browse"]}
    checkout_names = {t.name for t in agent._PHASE_TOOLS["checkout"]}
    missing = browse_names - checkout_names
    assert not missing, (
        f"checkout must be a superset of browse; missing: {missing}"
    )
    assert "suggest_gifts" in checkout_names
    assert "get_my_occasions" in checkout_names
    assert "clear_cart" in checkout_names  # still the checkout-only addition


def test_phase_tools_are_schema_function_tools():
    for phase, tools in agent._PHASE_TOOLS.items():
        for t in tools:
            assert isinstance(t, adapter.SchemaFunctionTool), (
                f"phase {phase}: {t!r} is not a SchemaFunctionTool — ADK would "
                f"fall back to auto-generated schema and defeat Fix 1"
            )


# ─────────────────────────────────────────────────────────
# Regression guards
# ─────────────────────────────────────────────────────────

def test_no_stringly_typed_json_params():
    """Fix 9: `items`, `preferences`, `friend_phones`, `wishlist_urls` must
    be real typed arrays — never strings that the model has to JSON-encode.
    """
    array_fields = {
        ("create_batch_gift_orders", "items"),
        ("setup_celebrant_occasion", "preferences"),
        ("setup_celebrant_occasion", "friend_phones"),
        ("setup_celebrant_occasion", "wishlist_urls"),
        ("suggest_gifts", "vibes"),
    }
    for tool_name, field in array_fields:
        schema = TOOL_DECLARATIONS[tool_name].parameters.properties[field]
        assert schema.type.value == "ARRAY", (
            f"{tool_name}.{field} must be ARRAY, got {schema.type.value}"
        )


def test_generate_content_config_is_explicit():
    """Fix 3: every cached runner must carry an explicit low-temperature
    generation config. Default (~1.0) causes tool-arg hallucination.
    """
    runner = agent._get_runner_for_phase("browse")
    cfg = runner.agent.generate_content_config
    assert cfg is not None
    assert cfg.temperature is not None and cfg.temperature <= 0.6
    assert cfg.max_output_tokens is not None and cfg.max_output_tokens > 0


def test_intent_classifier_uses_fast_model_setting():
    """The cheap preflight classifier must not consume the main agent model.

    Main bot turns need stronger tool-selection/reasoning, while classifier
    turns are narrow label selection. Keep the model split explicit.

    The classifier was refactored to a Gemini-primary / DeepSeek-fallback
    architecture. The fast model must appear in _gemini_classify (primary),
    and _llm_classify must delegate to _gemini_classify first.
    """
    from app.bot import intent_classifier
    from app.core.config import settings

    # Fast model used in the Gemini-primary path.
    gemini_src = inspect.getsource(intent_classifier._gemini_classify)
    assert "model=settings.gemini_fast_model" in gemini_src, (
        "_gemini_classify must use settings.gemini_fast_model, not a hard-coded string"
    )

    # _llm_classify must call _gemini_classify as its primary path.
    llm_src = inspect.getsource(intent_classifier._llm_classify)
    assert "_gemini_classify" in llm_src, (
        "_llm_classify must delegate to _gemini_classify as primary"
    )

    # DeepSeek fallback must be gated on deepseek_api_key being set.
    assert "_deepseek_classify" in llm_src, (
        "_llm_classify must have a DeepSeek fallback branch"
    )
    assert "deepseek_api_key" in llm_src, (
        "DeepSeek fallback must be gated on settings.deepseek_api_key"
    )

    # Model identity checks.
    assert settings.gemini_fast_model == "gemini-2.5-flash-lite"
    assert settings.gemini_model != settings.gemini_fast_model


def test_timeout_is_generous_enough_for_tool_chains():
    """Fix 5: 12s budget — anything <10 silently degrades multi-tool turns."""
    assert agent.AGENT_TURN_TIMEOUT_SECONDS >= 10.0


def test_advisory_lock_key_is_stable_and_fits_bigint():
    """Fix 10: lock key must be deterministic and fit Postgres bigint."""
    k = agent._advisory_lock_key("+919876543210")
    assert isinstance(k, int)
    assert 0 <= k < 2**63
    # Deterministic across calls.
    assert k == agent._advisory_lock_key("+919876543210")
    # Different phones → different keys (with overwhelming probability).
    assert k != agent._advisory_lock_key("+919876543211")


def test_salvage_reply_from_tool_refuses_unsafe_inputs():
    """_salvage_reply_from_tool must not invent content when the tool
    response has no user-facing message. Prevents the salvage path from
    becoming a new hallucination source.
    """
    # No tool name → no salvage.
    assert agent._salvage_reply_from_tool(None, {"message": "hi"}) is None
    # Non-dict response → no salvage.
    assert agent._salvage_reply_from_tool("foo", None) is None
    # Error response → no salvage (caller shouldn't reach here, but belt & braces).
    assert agent._salvage_reply_from_tool("foo", {"error": "bad"}) is None
    # Raw list result without a `message` → no salvage.
    assert agent._salvage_reply_from_tool("get_my_occasions", {"occasions": [1, 2, 3]}) is None


def test_salvage_reply_from_tool_surfaces_server_message():
    msg = "Your gift card ₹500 for Amazon is ready 🎁"
    assert agent._salvage_reply_from_tool("create_gift_order", {"message": msg}) == msg


# ─────────────────────────────────────────────────────────
# Reminder-system gaps G1 / G2 / G3 (issue #009 §5b)
# ─────────────────────────────────────────────────────────


def test_reminder_introspection_tools_exist():
    """G1: agent MUST be able to see reminder state, not guess 'a few days before'."""
    for name in ("get_reminder_schedule", "get_reminder_history"):
        assert name in TOOL_DECLARATIONS, f"{name} missing from declarations"
        assert hasattr(adapter, name), f"{name} missing wrapper"


def test_send_reminder_now_tool_exists():
    """G3: agent MUST be able to dispatch a reminder on user request."""
    assert "send_reminder_now" in TOOL_DECLARATIONS
    assert hasattr(adapter, "send_reminder_now")


def test_tool_descriptions_expose_hidden_contracts():
    """Tool schemas must carry concise Gemini-facing selection guidance.

    Runtime contracts live in ToolSpec/adapter. FunctionDeclaration descriptions
    stay focused on when to choose a tool and how to avoid adjacent tools.
    """
    descriptions = {name: decl.description for name, decl in TOOL_DECLARATIONS.items()}
    for name, desc in descriptions.items():
        for section in ("WHEN TO USE:", "USE WHEN:", "DO NOT USE WHEN:", "IMPORTANT:", "OUTPUT:"):
            assert section in desc, f"{name} declaration missing {section}"

    expectations = {
        "send_reminder_now": [
            "Send an immediate reminder",
            "setup_celebrant_occasion has succeeded",
            "linked that phone",
            "rate_limited",
        ],
        "add_to_wishlist": [
            "Save a product URL",
            "valid recipient_occasion_id/FriendContext",
            "otherwise omit purpose or use self",
        ],
        "add_occasion": [
            "Save an occasion",
            "setup_celebrant_occasion",
            "tight-window warning",
        ],
        "setup_celebrant_occasion": [
            "friends can be reminded",
            "Name (919876543210)",
            "wishlist_urls explicitly",
        ],
        "create_gift_order": [
            "payment link",
            "brand and amount are confirmed",
            "duplicate_acknowledged",
        ],
        "create_friend_gift_order": [
            "friend buying for a celebrant",
            "buyer_name",
            "duplicate_acknowledged",
        ],
        "register_user": [
            "Create or complete",
            "Pass city only when",
            "Already-registered",
        ],
        "delete_occasion": [
            "Permanently delete",
            "after confirmation",
            "cancels pending reminders",
        ],
        "edit_occasion": [
            "Update an existing",
            "Pass only fields",
            "duplicate_recent",
        ],
        "get_celebrant_context": [
            "Friend-flow entrypoint",
            "reminder link",
            "Do not ask for or reveal celebrant phone",
        ],
        "check_occasion_gifts": [
            "privacy_note",
            "already claimed",
            "avoid duplicates",
        ],
        "checkout_cart": [
            "Checkout staged cart items",
            "Each cart item can succeed or fail independently",
        ],
        "search_vendors": [
            "local options",
            "recipient city",
            "city is missing",
        ],
        "suggest_marketplace_gift_card": [
            "marketplace products",
            "Do not invent marketplace products",
        ],
        "get_self_preferences": [
            "current user's own gift preferences",
            "self-gift recommendations",
        ],
        "get_reminder_history": [
            "sent or past reminders",
            "back-to-back",
        ],
        "list_orders": [
            "recent Wishkr orders",
            "not a full archive",
        ],
        "get_upcoming_digest": [
            "upcoming occasions",
            "Set include_own=true",
        ],
        "set_contact_city": [
            "contact_id",
            "get_occasion_details first",
        ],
        "get_user_profile": [
            "registration state",
            "guest status",
        ],
        "search_brands": [
            "specific brand, category, or voucher",
            "vouchers exist under a budget",
            "Use query for named brands or categories",
        ],
        "suggest_gifts": [
            "Open-ended gift recommendation",
            "specific brand or voucher; use search_brands",
            "Preserve recipient_vibe",
        ],
    }

    missing: list[str] = []
    for tool_name, needles in expectations.items():
        desc = descriptions[tool_name]
        for needle in needles:
            if needle.lower() not in desc.lower():
                missing.append(f"{tool_name}: {needle!r}")

    assert not missing, "Missing hidden-contract schema hints: " + ", ".join(missing)

    forbidden_internal_terms = ("ToolSpec", "adapter", "database", "validators")
    leaked = [
        f"{tool_name}: {term}"
        for tool_name, desc in descriptions.items()
        for term in forbidden_internal_terms
        if term.lower() in desc.lower()
    ]
    assert not leaked, "Gemini-facing descriptions leaked internals: " + ", ".join(leaked)


def test_system_prompt_covers_cross_tool_response_patterns():
    """Shared response policy belongs in the prompt, not every tool schema."""
    from app.bot import prompt

    text = prompt.STATIC_SYSTEM_PROMPT
    expected = [
        "Tools are the source of truth",
        "Never fabricate gift availability, prices, codes, payment links",
        "Never claim an action is done unless a tool succeeded",
        "Ask one clarifying question when required information is missing",
        "does not sell, purchase, fulfil, or deliver gifts inside WhatsApp",
        "Use `suggest_gifts` for open-ended gift ideas",
        "For purchase language like",
        "do not invent products",
        "Never invent or mutate IDs",
        "`cel_*` FriendContext IDs are not `occasion_id`s",
        "use the FriendContext and fetch celebrant context",
        "use the celebrant setup flow before targeted reminders",
        "Read structured tool errors and follow their required action",
        "Do not blindly retry the same failed call",
        "For missing data or invalid input",
        "For permission/not-invited/not-found errors",
        "For rate limits",
    ]
    missing = [needle for needle in expected if needle not in text]
    assert not missing, "Missing prompt response-policy sections: " + ", ".join(missing)


def test_prompt_does_not_duplicate_tool_spec_summary():
    from app.bot import prompt

    assert "TOOL_SPEC_SUMMARY" not in dir(prompt)
    assert prompt.SYSTEM_PROMPT == (
        prompt.STATIC_SYSTEM_PROMPT
        + prompt.DYNAMIC_CONTEXT_TEMPLATE
    )
    assert "TOOL SPEC SUMMARY" not in prompt.STATIC_SYSTEM_PROMPT

    # Runtime dependencies stay in ToolSpec, not duplicated into prompt.py.
    assert "setup_celebrant_occasion" in TOOL_SPECS["send_reminder_now"].depends_on
    assert "get_brand_details" in TOOL_SPECS["create_gift_order"].depends_on
    assert "get_celebrant_context" in TOOL_SPECS["create_friend_gift_order"].depends_on
    assert "set_contact_city" in TOOL_SPECS["search_vendors"].depends_on
    assert "cart_nonempty_unexpired" in TOOL_SPECS["checkout_cart"].conditional_requires["cart"]


def test_tool_specs_cover_declared_tools_and_risky_edges():
    declared = set(TOOL_DECLARATIONS)
    assert set(TOOL_SPECS) == declared

    for tool_name, spec in TOOL_SPECS.items():
        assert get_tool_spec(tool_name) is spec
        assert spec.declaration is TOOL_DECLARATIONS[tool_name]
        assert isinstance(spec.requires, list), f"{tool_name} requires must be a list"
        assert isinstance(spec.conditional_requires, dict), (
            f"{tool_name} conditional_requires must be a dict"
        )
        assert isinstance(spec.depends_on, list), f"{tool_name} depends_on must be a list"
        assert isinstance(spec.produces, list), f"{tool_name} produces must be a list"

    assert "setup_celebrant_occasion" in TOOL_SPECS["send_reminder_now"].depends_on
    assert "get_celebrant_context" in TOOL_SPECS["create_friend_gift_order"].depends_on
    assert "get_brand_details" in TOOL_SPECS["create_gift_order"].depends_on
    assert "get_occasion_details" in TOOL_SPECS["set_contact_city"].depends_on
    assert (
        "recipient_occasion_id_present"
        in TOOL_SPECS["add_to_wishlist"].conditional_requires["purpose=gift"]
    )


def test_adapter_uses_tool_spec_as_validator_source():
    src = Path("app/bot/tools/adapter.py").read_text()
    assert "get_tool_spec(tool_name)" in src
    assert "_tool" + "_graph" not in src
    assert "TOOL" + "_GRAPH.json" not in src
    assert "logger.warning" not in src
    assert "conditional_requires" in src
    assert "Missing required field(s)" in src
    assert "tool_exception" not in src
    assert '"system_error"' in src


def test_adapter_fails_fast_for_unknown_contract_entries():
    with pytest.raises(RuntimeError, match="Unknown ToolSpec condition"):
        adapter._condition_applies("unknown_condition", {})

    with pytest.raises(RuntimeError, match="Unknown ToolSpec condition field"):
        adapter._condition_applies("unknown_field=value", {}, tool_name="get_user_profile")

    with pytest.raises(RuntimeError, match="Unhandled ToolSpec requirement"):
        asyncio.run(
            adapter._check_runtime_requirement(
                "unknown requirement",
                tool_name="get_user_profile",
                args={},
                db=None,
                phone="+919999900000",
            )
        )


def test_tool_spec_conditions_are_explicitly_registered():
    for tool_name, spec in TOOL_SPECS.items():
        schema_props = TOOL_DECLARATIONS[tool_name].parameters.properties or {}
        for condition in spec.conditional_requires:
            if "=" in condition:
                field, _expected = condition.split("=", 1)
                assert field in schema_props, (
                    f"{tool_name} has ToolSpec condition {condition!r} for "
                    f"undeclared field {field!r}"
                )
                continue
            assert condition in adapter.CONDITION_HANDLERS, (
                f"{tool_name} has unregistered ToolSpec condition {condition!r}"
            )


def test_tool_spec_requirements_are_explicitly_handled():
    intentionally_deferred_conditions = (
        adapter._RESPONSE_TIME_CONDITIONS | adapter._HANDLED_ELSEWHERE_CONDITIONS
    )

    for tool_name, spec in TOOL_SPECS.items():
        schema_props = TOOL_DECLARATIONS[tool_name].parameters.properties or {}

        for requirement in spec.requires:
            assert requirement in schema_props, (
                f"{tool_name} has required field {requirement!r} outside its schema"
            )

        for condition, requirements in spec.conditional_requires.items():
            if condition in intentionally_deferred_conditions:
                continue
            for requirement in requirements:
                assert requirement in schema_props or requirement in adapter.RUNTIME_CHECKS, (
                    f"{tool_name} has unhandled ToolSpec requirement {requirement!r} "
                    f"under condition {condition!r}"
                )


def test_all_conditional_requirement_values_are_resolvable():
    """Every value in conditional_requires must be resolvable at runtime.

    A requirement that is neither a declared schema parameter nor a key in
    RUNTIME_CHECKS will cause _check_runtime_requirement to raise
    RuntimeError('Unhandled ToolSpec requirement …') the first time that
    condition fires in production.

    Compared to test_tool_spec_requirements_are_explicitly_handled, this test
    does NOT skip _RESPONSE_TIME_CONDITIONS — even a condition that cannot be
    evaluated pre-execution (fires=False) still has its requirements routed
    through _check_runtime_requirement if the condition ever becomes active.
    Those requirements must be registered as _req_noop (or a real handler).

    _HANDLED_ELSEWHERE_CONDITIONS (currently just 'each_item') ARE skipped:
    their values bypass _check_runtime_requirement entirely and are validated
    by a dedicated item-level loop in _validate_tool_preconditions, so the
    standard resolvability check does not apply to them.

    Import note: adapter is already imported at the top of this file via the
    ADK stub pattern. If adapter ever becomes un-importable in a test context,
    define a test-local frozenset mirroring RUNTIME_CHECKS.keys() rather than
    importing adapter, and keep it in sync with adapter.RUNTIME_CHECKS.
    """
    known_runtime_keys = set(adapter.RUNTIME_CHECKS)
    # Only skip conditions whose values go through a completely different code
    # path — the dedicated item loop in _validate_tool_preconditions.
    bypass_conditions = adapter._HANDLED_ELSEWHERE_CONDITIONS

    bad: list[str] = []
    for tool_name, spec in TOOL_SPECS.items():
        schema_props = TOOL_DECLARATIONS[tool_name].parameters.properties or {}
        for condition, requirements in spec.conditional_requires.items():
            if condition in bypass_conditions:
                continue
            for requirement in requirements:
                if requirement not in schema_props and requirement not in known_runtime_keys:
                    bad.append(
                        f"  {tool_name}[condition={condition!r}] → {requirement!r}"
                    )

    assert not bad, (
        "Requirement values not in schema props or RUNTIME_CHECKS "
        "(will raise RuntimeError when condition fires in production):\n"
        + "\n".join(bad)
    )


def test_runtime_requirement_keys_are_canonical():
    key_re = re.compile(r"^[a-z][a-z0-9_]*$")

    for key in adapter.RUNTIME_CHECKS:
        assert key_re.fullmatch(key), f"Runtime requirement key is not canonical: {key!r}"

    for tool_name, spec in TOOL_SPECS.items():
        schema_props = TOOL_DECLARATIONS[tool_name].parameters.properties or {}
        for condition, requirements in spec.conditional_requires.items():
            for requirement in requirements:
                if requirement in schema_props:
                    continue
                assert key_re.fullmatch(requirement), (
                    f"{tool_name} has non-canonical requirement {requirement!r} "
                    f"under condition {condition!r}"
                )


def test_reminder_tools_available_in_browse_and_friend_phases():
    """Reminder tools must reach the agent on the turns where users ask
    about them — browse (owner asking about their occasion) and friend
    (friend asking when they'll be reminded)."""
    browse_names = {t.name for t in agent._PHASE_TOOLS["browse"]}
    friend_names = {t.name for t in agent._PHASE_TOOLS["friend"]}

    assert "get_reminder_schedule" in browse_names
    assert "get_reminder_history" in browse_names
    assert "send_reminder_now" in browse_names
    # Friend flow gets the read + ad-hoc but not history (less relevant).
    assert "get_reminder_schedule" in friend_names
    assert "send_reminder_now" in friend_names


# ─────────────────────────────────────────────────────────
# Gap G6 — poisoned ADK session replay (issue #009 §5b)
# ─────────────────────────────────────────────────────────


class _FakePart:
    def __init__(self, *, text=None, function_call=None, function_response=None):
        self.text = text
        self.function_call = function_call
        self.function_response = function_response


class _FakeContent:
    def __init__(self, parts, role="model"):
        self.parts = parts
        self.role = role


class _FakeEvent:
    def __init__(self, *, content, author="model", event_id="evt"):
        self.content = content
        self.author = author
        self.id = event_id


class _FakeFunctionCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakeFunctionResponse:
    def __init__(self, name, response):
        self.name = name
        self.response = response


class _FakeSession:
    def __init__(self, events):
        self.events = events
        self.id = "sess_test"


def _user_text_event(text):
    return _FakeEvent(
        content=_FakeContent([_FakePart(text=text)], role="user"),
        author="user",
    )


def _call_event(tool_name, args):
    return _FakeEvent(
        content=_FakeContent([_FakePart(function_call=_FakeFunctionCall(tool_name, args))]),
    )


def _resp_event(tool_name, response):
    return _FakeEvent(
        content=_FakeContent([_FakePart(function_response=_FakeFunctionResponse(tool_name, response))], role="user"),
        author="user",
    )


def _model_text_event(text):
    return _FakeEvent(content=_FakeContent([_FakePart(text=text)]))


def test_scrub_drops_tool_exception_pair():
    """A function_response with error='tool_exception' and its preceding
    function_call must both be removed so Gemini doesn't keep skipping
    the tool on later turns after a deploy fixes the bug."""
    session = _FakeSession(events=[
        _user_text_event("can u send reminder"),
        _call_event("send_reminder_now", {"occasion_id": "occ_1"}),
        _resp_event("send_reminder_now", {"error": "tool_exception", "message": "old bug"}),
        _model_text_event("I'm sorry…"),
    ])
    removed = agent._scrub_poisoned_events(session)
    assert removed == 2
    # User message + model apology remain; the poisoned call/response pair is gone.
    assert len(session.events) == 2


def test_scrub_preserves_legitimate_structured_errors():
    """`occasion_not_found`, `user_not_registered`, etc. carry recovery
    hints that the model SHOULD see and act on. Never scrub those."""
    session = _FakeSession(events=[
        _user_text_event("send reminder"),
        _call_event("send_reminder_now", {"occasion_id": "bad"}),
        _resp_event("send_reminder_now", {"error": "occasion_not_found", "hint": "call get_my_occasions first"}),
        _model_text_event("I don't see that occasion…"),
    ])
    removed = agent._scrub_poisoned_events(session)
    assert removed == 0
    assert len(session.events) == 4


def test_scrub_preserves_successful_tool_calls():
    session = _FakeSession(events=[
        _user_text_event("send reminder"),
        _call_event("send_reminder_now", {"occasion_id": "occ_1"}),
        _resp_event("send_reminder_now", {"success": True, "sent_to": "919..."}),
        _model_text_event("Done ✓"),
    ])
    removed = agent._scrub_poisoned_events(session)
    assert removed == 0
    assert len(session.events) == 4


def test_scrub_handles_mixed_sessions():
    """A session with a poisoned pair earlier and a good pair later —
    only the poisoned pair is dropped, pairing stays valid for the good one."""
    session = _FakeSession(events=[
        _user_text_event("try 1"),
        _call_event("send_reminder_now", {"occasion_id": "occ_1"}),
        _resp_event("send_reminder_now", {"error": "tool_exception", "message": "crash"}),
        _model_text_event("sorry"),
        _user_text_event("try 2"),
        _call_event("send_reminder_now", {"occasion_id": "occ_1"}),
        _resp_event("send_reminder_now", {"success": True}),
        _model_text_event("done ✓"),
    ])
    removed = agent._scrub_poisoned_events(session)
    assert removed == 2
    # Remaining: user1, apology, user2, call, success, done
    assert len(session.events) == 6
    # The remaining call/response pair must still be paired.
    names = [
        (getattr(getattr(e.content.parts[0], "function_call", None), "name", None),
         getattr(getattr(e.content.parts[0], "function_response", None), "name", None))
        for e in session.events
    ]
    assert ("send_reminder_now", None) in names
    assert (None, "send_reminder_now") in names


def test_scrub_is_noop_on_empty_session():
    session = _FakeSession(events=[])
    assert agent._scrub_poisoned_events(session) == 0


def test_reminder_templates_resolve_and_format():
    """Regression for the 23 Apr 2026 incident where
    `'CelebrantOccasion' object has no attribute 'budget_label'` and
    `msg.FRIEND_REMINDER` didn't exist — both would fire the moment a
    friend reminder fell back from the Meta template to raw send_text.

    These asserts check: every reminder-body template referenced by the
    cron exists on the module AND format-resolves with the kwargs the
    cron supplies.
    """
    from app.bot import messages as msg

    assert hasattr(msg, "REMINDER"), "send_reminders.py references msg.REMINDER"
    assert hasattr(msg, "REMINDER_TODAY")
    assert hasattr(msg, "FRIEND_REMINDER")
    assert hasattr(msg, "FRIEND_REMINDER_TODAY")

    # Kwarg contracts must match what send_reminders.py actually passes.
    msg.REMINDER.format(
        subject="x's birthday",
        days=5,
        date="28 Apr",
        suggestions="• a",
    )
    msg.REMINDER_TODAY.format(subject="x's birthday")
    msg.FRIEND_REMINDER.format(
        celebrant_name="x", occasion="birthday", days=5, date="28 Apr",
        preferences="• a", budget="Any budget",
    )
    msg.FRIEND_REMINDER_TODAY.format(
        celebrant_name="x", occasion="birthday", preferences="• a", budget="Any budget",
    )


def test_self_occasion_reminder_subject_uses_owner_language():
    """Self occasions should not fall back to "Someone's Anniversary"."""
    user = type("UserStub", (), {"name": "Karan"})()
    occasion = type("OccasionStub", (), {"contact_id": None})()

    assert (
        send_reminders._user_reminder_subject(occasion, user, None, "Anniversary")
        == "your Anniversary"
    )


def test_contact_occasion_reminder_subject_uses_contact_name():
    user = type("UserStub", (), {"name": "Karan"})()
    occasion = type("OccasionStub", (), {"contact_id": "con_1"})()
    contact = type("ContactStub", (), {"name": "Rutuja"})()

    assert (
        send_reminders._user_reminder_subject(occasion, user, contact, "Birthday")
        == "Rutuja's Birthday"
    )


def test_orphaned_contact_reminder_subject_uses_owner_language():
    user = type("UserStub", (), {"name": "Karan"})()
    occasion = type("OccasionStub", (), {"contact_id": "con_missing"})()

    assert (
        send_reminders._user_reminder_subject(occasion, user, None, "Birthday")
        == "your Birthday"
    )


def test_friend_reminder_celebrant_name_includes_masked_phone_suffix():
    """Friend reminders need an identity cue for common duplicate names."""
    celebrant = type("UserStub", (), {"name": "Karan", "phone": "919359615424"})()

    assert (
        send_reminders._friend_visible_celebrant_name(celebrant)
        == "Karan (phone ending 5424)"
    )


def test_celebrant_occasion_budget_access_is_defensive():
    """Same incident, other half: friend-reminder dispatch must not access
    `cel_occ.budget_label` as a hard attribute. If the model adds it later,
    great — but removing it must not re-break the cron."""
    import inspect
    from app.jobs import send_reminders
    src = inspect.getsource(send_reminders)
    # Either the attribute isn't referenced, or it's referenced via getattr
    # with a default. Raw attribute access is forbidden — it's the bug we
    # just shipped a fix for.
    assert (
        "cel_occ.budget_label" not in src
        or 'getattr(cel_occ, "budget_label"' in src
    ), "friend-reminder dispatch must guard budget_label access via getattr"


def test_reminder_row_exposes_target_name_and_wamid():
    """Reminder rows surfaced to the agent must include the resolved friendly
    name (so the bot says 'Alisha' not '919359615424') and the Meta wamid
    (so audits / delivery-status correlation work)."""
    from app.bot.tools import functions as fns
    from app.models import Reminder, ReminderStatus
    import datetime as _dt
    rem = Reminder(
        id="rem_test",
        occasion_id="occ_test",
        scheduled_at=_dt.datetime(2026, 4, 28, 3, 30, tzinfo=_dt.timezone.utc),
        sent_at=None,
        status=ReminderStatus.PENDING,
        message_type="tight_window_reminder",
        target_phone="919359615424",
        whatsapp_msg_id="wamid.abc123",
    )
    row = fns._format_reminder_row(rem, target_name="Alisha")
    assert row["target_name"] == "Alisha"
    assert row["whatsapp_msg_id"] == "wamid.abc123"
    assert row["target_phone"] == "919359615424"


def test_no_interakt_references_remain_in_python():
    """We migrated off Interakt. Guards against an accidental re-add.

    Allowlisted files can mention the name in comments explaining the move —
    that's historical context, not functional dependency.
    """
    import pathlib
    allow_suffixes = (
        "services/whatsapp.py",       # header comment citing BSP pricing context
        "models/reminder.py",         # migration note on the whatsapp_msg_id column
    )
    root = pathlib.Path("app")
    for py in root.rglob("*.py"):
        lower = py.read_text().lower()
        if "interakt" not in lower:
            continue
        if any(str(py).endswith(s) for s in allow_suffixes):
            continue
        raise AssertionError(f"stray Interakt reference in {py}")


def test_hubble_order_payload_uses_pseudonymised_customer_details(monkeypatch):
    """Hubble must receive Wishkr reference/support data, not end-user PII."""
    from app.services import hubble_fulfillment

    monkeypatch.setattr(hubble_fulfillment.settings, "hubble_customer_phone", "919999999999")
    details = hubble_fulfillment.hubble_customer_details("pur_test123")

    assert details["customer_name"] == "Wishkr Order pur_test123"
    assert details["customer_email"] == ""
    assert details["customer_phone"] == "9999999999"


def test_hubble_create_order_call_does_not_use_user_pii():
    """Static guard for the Apr 30 / May 1 MSA data-flow decision."""
    import inspect
    from app.webhooks import razorpay_handlers

    src = inspect.getsource(razorpay_handlers.handle_payment_link_paid)
    forbidden = [
        "customer_name=user.name",
        "customer_phone=user.phone",
        "customer_email=user.email",
        "customer_email=user.email if user",
        "customer_phone=user.phone if user",
        "customer_name=user.name if user",
    ]
    for token in forbidden:
        assert token not in src
    assert "should_defer_hubble_issue(p)" in src
    assert "issue_hubble_voucher(p)" in src


def test_future_scheduled_gifts_are_not_issued_in_paid_webhook():
    """Friend gifts scheduled for later must buy the voucher in the cron."""
    import inspect
    from app.webhooks import razorpay_handlers
    from app.jobs import deliver_gifts
    from app.jobs import reconcile_payments

    paid_src = inspect.getsource(razorpay_handlers.handle_payment_link_paid)
    delivery_src = inspect.getsource(deliver_gifts.deliver_scheduled_gifts)
    process_src = inspect.getsource(deliver_gifts._process_due_purchase)
    reconcile_src = inspect.getsource(reconcile_payments.reconcile_payments)

    assert "should_defer_hubble_issue(p)" in paid_src
    assert "OrderStatus.PAID" in paid_src
    assert "should_defer_hubble_issue(p, now=now)" in reconcile_src
    assert "Purchase.status.in_([PurchaseStatus.PENDING, PurchaseStatus.VOUCHER_ISSUED])" in delivery_src
    assert "Purchase.fulfillment_due_at <= now" in delivery_src
    assert "await issue_hubble_voucher(purchase)" in process_src
    # Concurrent delivery: all purchases processed in parallel, not sequentially.
    assert "asyncio.gather" in delivery_src
    # Row-level locking: concurrent workers skip rows already being processed.
    assert "with_for_update" in process_src
    assert "skip_locked" in process_src


def test_cart_ttl_single_source_of_truth():
    """CART_TTL_HOURS must be the only TTL definition — agent.py must import it, not hardcode."""
    import inspect
    from app.bot import agent
    from app.bot.tools.functions import CART_TTL_HOURS

    agent_src = inspect.getsource(agent)
    assert "CART_TTL_HOURS" in agent_src, "agent.py must import and use CART_TTL_HOURS"
    # Ensure the literal 24 is not used as a standalone hours value in cart freshness logic.
    # The TTL must come from the imported constant, not a hardcoded magic number.
    assert "timedelta(hours=24)" not in agent_src
    assert isinstance(CART_TTL_HOURS, int) and CART_TTL_HOURS > 0


def test_deliver_gifts_uses_template_name_constant():
    """Avoid hardcoded Meta template names drifting from whatsapp_templates.py."""
    import inspect
    from app.jobs import deliver_gifts

    src = inspect.getsource(deliver_gifts)
    assert "TEMPLATE_VOUCHER_DELIVERED_FRIEND" in src
    assert '"voucher_delivered_friend"' not in src


def test_reminder_schedule_constants_are_sane():
    """G2: codified policy. Moving these silently would change user behaviour."""
    from app.services import reminder as rem_svc
    assert rem_svc.MIN_PLANNING_DAYS == 10
    assert 10 in rem_svc.REMINDER_SCHEDULE_DAYS
    assert 0 in rem_svc.REMINDER_SCHEDULE_DAYS  # day-of reminder
    # Strictly decreasing (standard cron behaviour).
    assert list(rem_svc.REMINDER_SCHEDULE_DAYS) == sorted(rem_svc.REMINDER_SCHEDULE_DAYS, reverse=True)


def test_tight_window_warning_shape():
    """add_occasion / setup_celebrant_occasion must emit warning='tight_window'
    when days_until < MIN_PLANNING_DAYS. The agent keys on that literal, so
    its spelling is load-bearing."""
    import glob as _glob
    # functions.py is now a facade; scan all tool_impl source files.
    tool_impl_files = _glob.glob("app/bot/tools/tool_impl/*.py")
    src = "".join(open(f).read() for f in tool_impl_files)
    # There are two callsites that must surface the warning — one in
    # add_occasion, one in setup_celebrant_occasion. Each writes the literal
    # "tight_window" into a `warning` key.
    occurrences = src.count('"tight_window"')
    assert occurrences >= 2, (
        f'Expected at least 2 occurrences of the "tight_window" literal '
        f'(add_occasion + setup_celebrant_occasion), found {occurrences}'
    )


def test_salvage_reply_from_tool_emits_payment_link():
    out = agent._salvage_reply_from_tool(
        "create_gift_order",
        {"payment_url": "https://rzp.io/abc", "brand_name": "Amazon", "amount": 500},
    )
    assert out is not None
    assert "https://rzp.io/abc" in out
    assert "Amazon" in out
