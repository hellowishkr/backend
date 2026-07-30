"""Bouquet recipe costing and seasonal variant pricing.

Buyer-facing bouquet sizes are regular ``ProductVariant`` rows. This keeps
inventory and checkout pricing on the existing authoritative path while the
admin-only recipe explains how each option is constructed and costed.
"""

from __future__ import annotations

from datetime import date, datetime
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo


_IST = ZoneInfo("Asia/Kolkata")


def seasonal_price_is_active(variant: Any, on_date: date | None = None) -> bool:
    price = getattr(variant, "seasonal_price_paise", None)
    if price is None:
        return False
    # Seasonal catalogue dates are configured by the India-based operations
    # team and change at local midnight, independent of the server's timezone.
    current = on_date or datetime.now(_IST).date()
    starts = getattr(variant, "seasonal_price_start", None)
    ends = getattr(variant, "seasonal_price_end", None)
    return (starts is None or current >= starts) and (ends is None or current <= ends)


def effective_variant_prices(
    variant: Any,
    on_date: date | None = None,
) -> tuple[int, int | None, bool]:
    """Return effective base, discount, and seasonal-active state.

    A seasonal override is the complete selling amount, so it replaces both
    normal base and discounted values. Global storefront campaigns may still
    apply later, consistently in catalogue and checkout.
    """

    if seasonal_price_is_active(variant, on_date):
        seasonal = int(variant.seasonal_price_paise)
        return seasonal, seasonal, True
    return (
        int(variant.price_delta_paise),
        getattr(variant, "discounted_price_paise", None),
        False,
    )


def bouquet_recipe_cost(recipe: dict | None, *, peak: bool = False) -> int:
    """Calculate conservative per-bouquet cost from procurement lots.

    Materials are bought in whole lots. A 50-stem recipe with a 20-stem lot
    therefore consumes three lots for costing unless the admin models shared
    procurement elsewhere.
    """

    if not isinstance(recipe, dict) or not recipe.get("enabled", True):
        return 0

    material_total = 0
    for component in recipe.get("components") or []:
        if not isinstance(component, dict):
            continue
        quantity = max(0, int(component.get("quantity") or 0))
        lot_size = max(1, int(component.get("lot_size") or 1))
        base_cost = max(0, int(component.get("off_season_lot_cost_paise") or 0))
        peak_cost = component.get("peak_season_lot_cost_paise")
        lot_cost = (
            max(0, int(peak_cost))
            if peak and peak_cost is not None
            else base_cost
        )
        material_total += ceil(quantity / lot_size) * lot_cost

    return material_total + sum(
        max(0, int(recipe.get(field) or 0))
        for field in (
            "making_cost_paise",
            "packaging_cost_paise",
            "other_cost_paise",
        )
    )


def public_bouquet_composition(recipe: dict | None) -> list[dict]:
    """Return buyer-safe recipe lines without any vendor cost information."""

    if not isinstance(recipe, dict) or not recipe.get("enabled", True):
        return []
    public: list[dict] = []
    for component in recipe.get("components") or []:
        if not isinstance(component, dict):
            continue
        name = str(component.get("name") or "").strip()
        quantity = int(component.get("quantity") or 0)
        if not name or quantity <= 0:
            continue
        public.append(
            {
                "name": name,
                "kind": str(component.get("kind") or "flower"),
                "quantity": quantity,
                "unit_label": str(component.get("unit_label") or "stem"),
            }
        )
    return public
