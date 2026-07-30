from datetime import date

import pytest
from pydantic import ValidationError

from app.api.admin_catalog import VariantPayload
from app.api.catalog import serialize_product
from app.models.product import Product, ProductVariant
from app.services.bouquet_pricing import (
    bouquet_recipe_cost,
    effective_variant_prices,
)


def _recipe() -> dict:
    return {
        "enabled": True,
        "components": [
            {
                "name": "Red rose",
                "kind": "flower",
                "quantity": 40,
                "unit_label": "stem",
                "lot_size": 20,
                "off_season_lot_cost_paise": 20_000,
                "peak_season_lot_cost_paise": 60_000,
            }
        ],
        "making_cost_paise": 9_900,
        "packaging_cost_paise": 10_000,
        "other_cost_paise": 0,
    }


def test_bouquet_recipe_cost_rounds_materials_to_procurement_lots() -> None:
    assert bouquet_recipe_cost(_recipe()) == 59_900
    assert bouquet_recipe_cost(_recipe(), peak=True) == 139_900

    recipe = _recipe()
    recipe["components"][0]["quantity"] = 50
    # Fifty roses require three complete 20-rose lots.
    assert bouquet_recipe_cost(recipe) == 79_900


def test_bouquet_recipe_cost_is_zero_when_recipe_is_disabled() -> None:
    recipe = _recipe()
    recipe["enabled"] = False

    assert bouquet_recipe_cost(recipe) == 0
    assert bouquet_recipe_cost(recipe, peak=True) == 0


def test_seasonal_variant_price_replaces_normal_price_inside_window() -> None:
    variant = ProductVariant(
        id="var_40",
        product_id="prd_roses",
        name="40 roses",
        price_delta_paise=79_900,
        discounted_price_paise=69_900,
        seasonal_price_paise=119_900,
        seasonal_price_start=date(2026, 2, 1),
        seasonal_price_end=date(2026, 2, 14),
    )

    assert effective_variant_prices(variant, date(2026, 1, 31)) == (
        79_900,
        69_900,
        False,
    )
    assert effective_variant_prices(variant, date(2026, 2, 14)) == (
        119_900,
        119_900,
        True,
    )


def test_public_catalog_hides_costs_but_exposes_composition_and_effective_price() -> None:
    variant = ProductVariant(
        id="var_40",
        product_id="prd_roses",
        name="40 roses",
        price_delta_paise=79_900,
        seasonal_price_paise=119_900,
        bouquet_recipe=_recipe(),
        active=True,
        sort_order=1,
    )
    product = Product(
        id="prd_roses",
        vendor_id="ven_zahraa",
        name="Red Roses Bouquet",
        base_price_paise=49_900,
        active=True,
        variants=[variant],
        addons=[],
    )

    public_variant = serialize_product(product)["variants"][0]
    assert public_variant["effective_base_price_paise"] == 119_900
    assert public_variant["seasonal_price_active"] is True
    assert public_variant["bouquet_composition"] == [
        {
            "name": "Red rose",
            "kind": "flower",
            "quantity": 40,
            "unit_label": "stem",
        }
    ]
    assert "bouquet_recipe" not in public_variant
    assert "bouquet_cost_summary" not in public_variant

    admin_variant = serialize_product(product, include_vendor_name=True)["variants"][0]
    assert admin_variant["bouquet_recipe"] == _recipe()
    assert admin_variant["bouquet_cost_summary"] == {
        "off_season_paise": 59_900,
        "peak_season_paise": 139_900,
    }

    variant.active = False
    assert serialize_product(product)["variants"] == []
    assert serialize_product(product, include_vendor_name=True)["variants"][0][
        "name"
    ] == "40 roses"


def test_variant_payload_validates_recipe_and_season_window() -> None:
    payload = VariantPayload(
        name="20 roses",
        price_delta_paise=49_900,
        bouquet_recipe=_recipe(),
    )
    assert payload.bouquet_recipe is not None
    assert payload.bouquet_recipe.components[0].lot_size == 20

    with pytest.raises(ValidationError, match="seasonal price start"):
        VariantPayload(
            name="20 roses",
            price_delta_paise=49_900,
            seasonal_price_paise=79_900,
            seasonal_price_start=date(2026, 2, 15),
            seasonal_price_end=date(2026, 2, 1),
        )

    with pytest.raises(ValidationError, match="at least one component"):
        VariantPayload(
            name="20 roses",
            price_delta_paise=49_900,
            bouquet_recipe={"enabled": True, "components": []},
        )
