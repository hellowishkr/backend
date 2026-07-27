"""serialize_product exposes the storefront markup/flash multipliers so the web
app can render struck-through + flash prices. Admin/vendor callers pass no
pricing and get a neutral (1.0) block."""

from decimal import Decimal

from app.api.catalog import serialize_pricing, serialize_product
from app.models.product import Product
from app.services.storefront_pricing import StorefrontPricing


def _product() -> Product:
    return Product(
        id="prd_x",
        vendor_id="ven_x",
        name="Candle",
        base_price_paise=12_500,
        discounted_price_paise=10_000,
        inventory_quantity=3,
    )


def test_pricing_block_reflects_active_flash():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    block = serialize_product(_product(), pricing=p)["pricing"]
    # Strings are stringified Decimals (exact digits depend on the source
    # Decimal's scale); the web app parseFloat's them, so value is what matters.
    assert block["flash_active"] is True
    assert float(block["flash_off_pct"]) == 20.0
    assert float(block["list_multiplier"]) == 1.30
    assert float(block["charge_multiplier"]) == 1.04


def test_pricing_block_neutral_when_disabled():
    p = StorefrontPricing(Decimal("0"), Decimal("0"), False)
    block = serialize_product(_product(), pricing=p)["pricing"]
    assert block["flash_active"] is False
    assert block["list_multiplier"] == "1"
    assert block["charge_multiplier"] == "1"


def test_pricing_block_neutral_for_admin_no_pricing():
    # Admin/vendor callers pass no pricing → raw prices, no flash theatre.
    block = serialize_product(_product())["pricing"]
    assert block["flash_active"] is False
    assert block["list_multiplier"] == "1"
    assert block["charge_multiplier"] == "1"


def test_serialize_pricing_matches_embedded_block():
    # The live /catalog/pricing endpoint and the block embedded in products must
    # be identical (both call serialize_pricing), so the cart's fresh rate lines
    # up exactly with product listings.
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    assert serialize_pricing(p) == serialize_product(_product(), pricing=p)["pricing"]


def test_product_specific_base_discount_and_inventory_are_serialized():
    body = serialize_product(_product())
    assert body["base_price_paise"] == 12_500
    assert body["discounted_price_paise"] == 10_000
    assert body["discount_pct"] == "20.00"
    assert body["inventory_quantity"] == 3
    assert body["in_stock"] is True


def test_zero_inventory_is_effectively_out_of_stock():
    product = _product()
    product.in_stock = True
    product.inventory_quantity = 0
    assert serialize_product(product)["in_stock"] is False
