from app.api.product_orders import PricedLine, compute_order_shipping_paise
from app.models.product import Product
from app.models.vendor import Vendor


def _line(*, shipping_fee_paise: int, qty: int) -> PricedLine:
    # Only qty and shipping_fee_paise are read by the function under test; the
    # product is required by the model but its contents are irrelevant here.
    return PricedLine(
        product=Product(id="prd_test", vendor_id="ven_test", name="x", base_price_paise=0),
        variant=None,
        selected_addons=[],
        customizations={},
        qty=qty,
        unit_price_paise=0,
        base_unit_price_paise=0,
        line_total_paise=0,
        shipping_fee_paise=shipping_fee_paise,
    )


def _vendor(extra_fee_paise: int, *, flat_fee_paise: int | None = None) -> Vendor:
    return Vendor(
        id="ven_test",
        name="Test Vendor",
        category="gifts",
        shipping_flat_fee_paise=flat_fee_paise,
        shipping_extra_item_fee_paise=extra_fee_paise,
    )


def test_single_item_charges_only_base() -> None:
    lines = [_line(shipping_fee_paise=10000, qty=1)]
    assert compute_order_shipping_paise(lines, _vendor(3000)) == 10000


def test_extra_units_add_per_unit_fee() -> None:
    # 3 total units: base 100 + 30 × 2 extra = 160 (in paise: 16000).
    lines = [_line(shipping_fee_paise=10000, qty=3)]
    assert compute_order_shipping_paise(lines, _vendor(3000)) == 16000


def test_multiple_products_use_highest_base_plus_extras() -> None:
    # 2 units total (one each): base = max(100, 150) = 150, + 30 × 1 = 180.
    lines = [
        _line(shipping_fee_paise=10000, qty=1),
        _line(shipping_fee_paise=15000, qty=1),
    ]
    assert compute_order_shipping_paise(lines, _vendor(3000)) == 18000


def test_free_shipping_items_do_not_raise_base() -> None:
    # base = max(0, 100) = 100, 2 units → + 30 × 1 = 130.
    lines = [
        _line(shipping_fee_paise=0, qty=1),
        _line(shipping_fee_paise=10000, qty=1),
    ]
    assert compute_order_shipping_paise(lines, _vendor(3000)) == 13000


def test_zero_extra_fee_is_flat_one_parcel() -> None:
    lines = [
        _line(shipping_fee_paise=10000, qty=2),
        _line(shipping_fee_paise=10000, qty=3),
    ]
    assert compute_order_shipping_paise(lines, _vendor(0)) == 10000


def test_vendor_flat_fee_overrides_product_fee_and_quantity() -> None:
    lines = [
        _line(shipping_fee_paise=10000, qty=60),
        _line(shipping_fee_paise=15000, qty=40),
    ]
    assert (
        compute_order_shipping_paise(
            lines,
            _vendor(3000, flat_fee_paise=5900),
        )
        == 5900
    )


def test_zero_vendor_flat_fee_overrides_legacy_shipping() -> None:
    lines = [_line(shipping_fee_paise=10000, qty=100)]
    assert compute_order_shipping_paise(lines, _vendor(3000, flat_fee_paise=0)) == 0
