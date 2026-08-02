from app.api.product_orders import (
    FREE_DELIVERY_THRESHOLD_PAISE,
    compute_order_shipping_paise,
)
from app.models.vendor import Vendor


def _vendor(extra_fee_paise: int, *, flat_fee_paise: int | None = None) -> Vendor:
    return Vendor(
        id="ven_test",
        name="Test Vendor",
        category="gifts",
        shipping_flat_fee_paise=flat_fee_paise,
        shipping_extra_item_fee_paise=extra_fee_paise,
    )


def test_vendor_flat_fee_applies_below_threshold() -> None:
    assert compute_order_shipping_paise(49_900, _vendor(3000, flat_fee_paise=5900)) == 5900


def test_delivery_is_free_at_threshold() -> None:
    assert (
        compute_order_shipping_paise(
            FREE_DELIVERY_THRESHOLD_PAISE,
            _vendor(3000, flat_fee_paise=5900),
        )
        == 0
    )


def test_delivery_is_free_above_threshold() -> None:
    assert compute_order_shipping_paise(50_100, _vendor(3000, flat_fee_paise=5900)) == 0


def test_unconfigured_vendor_is_free_below_threshold() -> None:
    assert compute_order_shipping_paise(10_000, _vendor(3000)) == 0


def test_legacy_extra_item_fee_is_ignored() -> None:
    assert compute_order_shipping_paise(49_900, _vendor(3000)) == 0


def test_explicit_zero_flat_fee_remains_free() -> None:
    assert compute_order_shipping_paise(49_900, _vendor(3000, flat_fee_paise=0)) == 0
