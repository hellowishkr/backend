"""Validation for public product-order checkout input."""

import pytest
from pydantic import ValidationError

from app.api.product_orders import ProductOrderPayload


def _payload() -> dict:
    return {
        "customer": {"name": "Buyer", "phone": "9876543210"},
        "shipping_address": {
            "name": "Recipient",
            "phone": "9876543211",
            "line1": "1 Test Street",
            "city": "Pune",
            "state": "Maharashtra",
            "pincode": "411001",
        },
        "items": [{"product_id": "prd_test", "qty": 1}],
        "delivery_date": "2026-07-10",
        "attribution": {
            "first_touch": {
                "utm_source": "instagram",
                "utm_campaign": "pune_launch",
                "landing_path": "/shop?utm_source=instagram",
                "captured_at": "2026-07-06T09:00:00.000Z",
            }
        },
    }


def test_product_order_accepts_six_digit_pincode_and_attribution():
    payload = ProductOrderPayload.model_validate(_payload())

    assert payload.shipping_address.pincode == "411001"
    assert payload.attribution is not None
    assert payload.attribution.first_touch is not None
    assert payload.attribution.first_touch.utm_campaign == "pune_launch"


@pytest.mark.parametrize("pincode", ["41100", "4110012", "411-01", "PUNE01"])
def test_product_order_rejects_invalid_pincode(pincode: str):
    data = _payload()
    data["shipping_address"]["pincode"] = pincode

    with pytest.raises(ValidationError):
        ProductOrderPayload.model_validate(data)
