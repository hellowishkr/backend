"""Money-core: normal discounts and flash pricing use distinct inputs."""

from decimal import Decimal

from app.services.storefront_pricing import StorefrontPricing


def _split(
    base_paise: int,
    discounted_paise: int,
    shipping_paise: int,
    pricing: StorefrontPricing,
):
    """Mirror of the split in create_product_order."""
    charged_product = pricing.charge_paise(base_paise, discounted_paise)
    total = charged_product + shipping_paise
    vendor = round(discounted_paise * 0.90) + shipping_paise
    platform = total - vendor
    return total, vendor, platform


def test_flash_active_flat_4pct_over_vendor_price():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    # ₹100 base and ₹90 normal discounted price, no shipping.
    total, vendor, platform = _split(10_000, 9_000, 0, p)
    assert total == 10_400          # flash charge is ₹100 × 1.30 × 0.80
    assert vendor == 8_100          # vendor split stays on normal ₹90 price
    assert platform == 2_300
    assert vendor + platform == total


def test_split_ignores_markup_uses_raw_vendor_price():
    active = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    off = StorefrontPricing(Decimal("30"), Decimal("20"), False)
    # Vendor payout is identical whether or not the flash is charged.
    _, vendor_active, _ = _split(70_000, 63_700, 5_000, active)
    _, vendor_off, _ = _split(70_000, 63_700, 5_000, off)
    assert vendor_active == vendor_off == round(63_700 * 0.90) + 5_000


def test_flash_inactive_charges_vendor_discounted_price():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), False)
    total, vendor, platform = _split(10_000, 9_000, 0, p)
    assert total == 9_000           # global markup is ignored while flash is off
    assert vendor == 8_100
    assert platform == 900
    assert vendor + platform == total


def test_shipping_is_never_marked_up():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    total, vendor, _ = _split(10_000, 9_000, 5_000, p)
    assert total == 10_400 + 5_000  # shipping passes through unchanged
    assert vendor == 8_100 + 5_000  # vendor gets full shipping back


def test_addons_and_customization_included_in_uplift():
    # product_total already folds in variants + add-ons + customization, so a
    # larger vendor total uplifts proportionally.
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    assert p.charge_paise(25_000) == 26_000   # ₹250 → ₹260 (×1.04)


def test_charge_always_whole_rupees_rounded_up():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    # ₹637 × 1.04 = ₹662.48 → must round UP to ₹663, never show paise.
    assert p.charge_paise(63_700) == 66_300
    # ₹99 × 1.04 = ₹102.96 → ₹103.
    assert p.charge_paise(9_900) == 10_300
    # Exact whole rupee stays put (no needless +1).
    assert p.charge_paise(10_000) == 10_400
    # Every result is a whole number of rupees (multiple of 100 paise).
    for v in (1, 9_900, 63_700, 12_345, 88_801):
        assert p.charge_paise(v) % 100 == 0
        assert p.list_paise(v) % 100 == 0


def test_list_price_also_rounds_up_whole_rupee():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    # ₹637 × 1.30 = ₹828.10 → ₹829.
    assert p.list_paise(63_700) == 82_900
    assert p.list_paise(10_000) == 13_000  # clean ₹130


def test_disabled_program_charges_raw_vendor_price():
    # The safe holding state (seeded 0/0/off): no uplift at all, so checkout
    # display (raw V) and the charge agree. Guards against re-enabling by accident.
    p = StorefrontPricing(Decimal("0"), Decimal("0"), False)
    assert p.charge_paise(10_000, 9_000) == 9_000
    assert p.charge_paise(10_000) == 10_000
    assert p.list_paise(63_700) == 63_700        # no struck-through markup
    assert p.charge_multiplier == Decimal(1)


def test_active_flash_ignores_normal_discounted_price():
    p = StorefrontPricing(Decimal("30"), Decimal("20"), True)
    assert p.charge_paise(10_000, 9_000) == 10_400
    assert p.charge_paise(10_000, 7_500) == 10_400
