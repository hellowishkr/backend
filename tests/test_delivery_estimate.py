from datetime import datetime, timezone

from app.models.vendor import Vendor
from app.services.delivery_estimate import (
    DEFAULT_LEAD_TIME_HOURS,
    SAFETY_BUFFER_DAYS,
    earliest_delivery_date,
    express_delivery_available_today,
    supports_express_delivery,
)


def _vendor(*, lead_hours: int | None, cutoff_hour: int | None = None) -> Vendor:
    return Vendor(
        id="ven_test",
        name="Test Vendor",
        category="gifts",
        delivery_lead_time_hours=lead_hours,
        same_day_cutoff_hour=cutoff_hour,
    )


def test_no_lead_time_configured_falls_back_to_default() -> None:
    vendor = _vendor(lead_hours=None)
    now = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now)
    delta_days = (result - now.date()).days
    assert delta_days == DEFAULT_LEAD_TIME_HOURS // 24 + SAFETY_BUFFER_DAYS


def test_multi_day_lead_time_adds_correct_days() -> None:
    vendor = _vendor(lead_hours=72)
    now = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now)
    assert (result - now.date()).days == 3 + SAFETY_BUFFER_DAYS


def test_same_day_before_cutoff_allows_today() -> None:
    vendor = _vendor(lead_hours=0, cutoff_hour=14)
    # 04:30 UTC is 10:00 in India.
    now = datetime(2026, 6, 21, 4, 30, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now)
    assert result == now.date()


def test_same_day_after_cutoff_starts_tomorrow() -> None:
    vendor = _vendor(lead_hours=0, cutoff_hour=14)
    # 10:30 UTC is 16:00 in India.
    now = datetime(2026, 6, 21, 10, 30, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now)
    assert (result - now.date()).days == 1


def test_sub_day_lead_before_cutoff_allows_today() -> None:
    vendor = _vendor(lead_hours=4, cutoff_hour=18)
    # 06:30 UTC is 12:00 in India.
    now = datetime(2026, 6, 21, 6, 30, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now)
    assert result == now.date()


def test_sub_day_lead_after_cutoff_starts_tomorrow() -> None:
    vendor = _vendor(lead_hours=4, cutoff_hour=18)
    # 13:30 UTC is 19:00 in India.
    now = datetime(2026, 6, 21, 13, 30, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now)
    assert (result - now.date()).days == 1


def test_sub_day_vendor_is_express_before_but_not_available_after_cutoff() -> None:
    vendor = _vendor(lead_hours=4, cutoff_hour=18)
    before = datetime(2026, 6, 21, 11, 30, tzinfo=timezone.utc)  # 17:00 IST
    after = datetime(2026, 6, 21, 13, 30, tzinfo=timezone.utc)  # 19:00 IST

    assert supports_express_delivery(vendor) is True
    assert express_delivery_available_today(vendor, now=before) is True
    assert express_delivery_available_today(vendor, now=after) is False


def test_multi_day_product_override_is_not_express() -> None:
    vendor = _vendor(lead_hours=4, cutoff_hour=18)

    assert supports_express_delivery(vendor, product_lead_time_days=2) is False
    assert express_delivery_available_today(vendor, product_lead_time_days=2) is False


def test_product_lead_time_overrides_unset_vendor_default() -> None:
    # Regression: a vendor with no delivery_lead_time_hours configured (the
    # common case — most vendors never set it) previously always fell back to
    # the 5-day DEFAULT_LEAD_TIME_HOURS, even when every product had a 1-day
    # dispatch_lead_time_days set in the admin/vendor editor.
    vendor = _vendor(lead_hours=None)
    now = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now, product_lead_time_days=1)
    assert (result - now.date()).days == 1 + SAFETY_BUFFER_DAYS


def test_product_lead_time_overrides_configured_vendor_default() -> None:
    # The product-level figure is a more specific vendor claim than the
    # vendor-wide fallback, so it wins even when the vendor field IS set.
    vendor = _vendor(lead_hours=72)
    now = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
    result = earliest_delivery_date(vendor, now=now, product_lead_time_days=1)
    assert (result - now.date()).days == 1 + SAFETY_BUFFER_DAYS
