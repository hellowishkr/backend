"""Compatibility facade — re-exports everything from app.bot.tools.tool_impl.*.

adapter.py and tests import from this module. All implementations live in
the tool_impl/ package; nothing should be added here directly.
"""

from app.services.razorpay_service import razorpay_service

# ── helpers (private utilities referenced by tests and adapter indirectly) ──
from app.bot.tools.tool_impl.helpers import (
    _err,
    _HINT_REGISTER,
    _HINT_OCCASION_UNKNOWN,
    _HINT_BRAND_UNKNOWN,
    _HINT_CELEBRANT_UNKNOWN,
    _get_user,
    _find_or_create_contact,
    _find_brand_id,
    _check_brand_amount,
    _user_owns_occasion,
    _can_view_occasion_gifts,
    _duplicate_brand_warning,
)

# ── users ────────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.users import (
    get_user_profile,
    register_user,
)

# ── occasions ────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.occasions import (
    add_occasion,
    list_occasions,
    get_my_occasions,
    get_occasion_details,
    get_upcoming_digest,
    snooze_occasion,
    skip_occasion_this_year,
    get_monthly_spend,
    edit_occasion,
    delete_occasion,
    delete_all_occasions,
    merge_occasions,
)

# ── gifting ──────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.gifting import (
    search_brands,
    get_brand_details,
    check_occasion_gifts,
    log_external_purchase,
    log_my_gift,
    get_past_gifts_for_contact,
    suggest_gifts,
    save_gift_plan,
    suggest_marketplace_gift_card,
    get_self_preferences,
    _FULFILLMENT_BONUS,
    _budget_overlaps,
    _score,
)

# ── marketplace checkout ─────────────────────────────────────────────────────
from app.bot.tools.tool_impl.marketplace_checkout import (
    create_checkout,
    select_gift_candidate,
)

# ── orders ───────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.orders import (
    _LINK_REUSE_WINDOW_SECONDS,
    _cancel_stale_pending_orders,
    _find_reusable_pending_order,
    _quote_hubble_gift_card,
    create_gift_order,
    list_orders,
    create_friend_gift_order,
    create_batch_gift_orders,
)

# ── cart ─────────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.cart import (
    CART_KEY,
    CART_CREATED_AT_KEY,
    CART_TTL_HOURS,
    _get_conv_state,
    _save_conv_state,
    _parse_cart_created_at,
    _cart_expired,
    _clear_cart_temp,
    _load_active_cart,
    add_to_cart,
    view_cart,
    checkout_cart,
    clear_cart,
)

# ── wishlist ─────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.wishlist import (
    add_to_wishlist,
    list_my_wishlist,
    remove_wishlist_item,
    clear_wishlist,
)
from app.bot.tools.tool_impl.wishlist_sharing import (
    get_friend_wishlist,
    reserve_wishlist_item,
    confirm_wishlist_purchase,
)
from app.bot.tools.tool_impl.recipient_preferences import (
    request_recipient_preferences,
)

# ── celebrant ────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.celebrant import (
    setup_celebrant_occasion,
    update_celebrant_occasion,
    get_celebrant_context,
)

# ── vendors ──────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.vendors import (
    set_contact_city,
    search_vendors,
)

# ── reminders ────────────────────────────────────────────────────────────────
from app.bot.tools.tool_impl.reminders import (
    _AD_HOC_DEDUP,
    _AD_HOC_DEDUP_SECONDS,
    _ad_hoc_key,
    _format_reminder_row,
    _build_target_name_map,
    get_reminder_schedule,
    get_reminder_history,
    set_reminder_schedule,
    send_reminder_now,
)

__all__ = [
    # helpers
    "_err", "_HINT_REGISTER", "_HINT_OCCASION_UNKNOWN", "_HINT_BRAND_UNKNOWN",
    "_HINT_CELEBRANT_UNKNOWN",
    "_get_user", "_find_or_create_contact", "_find_brand_id", "_check_brand_amount",
    "_user_owns_occasion", "_can_view_occasion_gifts", "_duplicate_brand_warning",
    # users
    "get_user_profile", "register_user",
    # occasions
    "add_occasion", "list_occasions", "get_my_occasions", "get_occasion_details",
    "get_upcoming_digest", "snooze_occasion", "skip_occasion_this_year",
    "get_monthly_spend", "edit_occasion", "delete_occasion", "delete_all_occasions",
    "merge_occasions",
    # gifting
    "search_brands", "get_brand_details", "check_occasion_gifts",
    "log_external_purchase", "log_my_gift", "get_past_gifts_for_contact",
    "suggest_gifts", "save_gift_plan", "suggest_marketplace_gift_card",
    "get_self_preferences", "_FULFILLMENT_BONUS", "_budget_overlaps", "_score",
    # marketplace checkout
    "select_gift_candidate", "create_checkout",
    # orders
    "_LINK_REUSE_WINDOW_SECONDS", "_cancel_stale_pending_orders",
    "_find_reusable_pending_order", "_quote_hubble_gift_card",
    "create_gift_order", "list_orders",
    "create_friend_gift_order", "create_batch_gift_orders", "razorpay_service",
    # cart
    "CART_KEY", "CART_CREATED_AT_KEY", "CART_TTL_HOURS",
    "_get_conv_state", "_save_conv_state", "_parse_cart_created_at",
    "_cart_expired", "_clear_cart_temp", "_load_active_cart",
    "add_to_cart", "view_cart", "checkout_cart", "clear_cart",
    # wishlist
    "add_to_wishlist", "list_my_wishlist", "remove_wishlist_item", "clear_wishlist",
    "get_friend_wishlist", "reserve_wishlist_item", "confirm_wishlist_purchase",
    "request_recipient_preferences",
    # celebrant
    "setup_celebrant_occasion", "update_celebrant_occasion",
    "get_celebrant_context",
    # vendors
    "set_contact_city", "search_vendors",
    # reminders
    "_AD_HOC_DEDUP", "_AD_HOC_DEDUP_SECONDS", "_ad_hoc_key",
    "_format_reminder_row", "_build_target_name_map",
    "get_reminder_schedule", "get_reminder_history",
    "set_reminder_schedule", "send_reminder_now",
]
