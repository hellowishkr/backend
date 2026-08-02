from app.api.product_orders import (
    FREE_DELIVERY_THRESHOLD_PAISE,
    STANDARD_DELIVERY_FEE_PAISE,
    compute_order_shipping_paise,
)


def test_standard_49_rupee_fee_applies_below_threshold() -> None:
    assert compute_order_shipping_paise(49_900) == STANDARD_DELIVERY_FEE_PAISE


def test_delivery_is_free_at_threshold() -> None:
    assert (
        compute_order_shipping_paise(
            FREE_DELIVERY_THRESHOLD_PAISE,
        )
        == 0
    )


def test_delivery_is_free_above_threshold() -> None:
    assert compute_order_shipping_paise(50_100) == 0


def test_small_bouquet_order_also_pays_standard_fee() -> None:
    assert compute_order_shipping_paise(34_900) == 4_900


def test_zero_value_order_still_uses_standard_fee() -> None:
    assert compute_order_shipping_paise(0) == 4_900
