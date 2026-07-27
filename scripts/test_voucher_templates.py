"""Send the two approved voucher utility templates to a test phone.

Run BEFORE flipping ``WHATSAPP_USE_VOUCHER_TEMPLATE=true`` to catch any
template-param-shape mismatch (Meta rejects with cryptic 132xxx codes when
positional params don't line up with the approved body).

Usage:
  python -m scripts.test_voucher_templates <phone_e164_no_plus>

Costs: two utility-template sends (~₹0.23 in IN). The phone you target must
have opted into receiving messages from your WhatsApp Business number — Meta
won't deliver to a stranger's number even with a template.

If either send returns a non-2xx, the script prints Meta's full error body
so you can compare against the approved template definition in Meta Business
Manager → WhatsApp Manager → Message templates.
"""

from __future__ import annotations

import asyncio
import sys

from app.services.whatsapp import whatsapp_service


_VOUCHER_DELIVERED_BODY = [
    "Amazon",          # {{1}} brand
    "500",             # {{2}} amount
    "AMZN-1234-5678",  # {{3}} code
    "4827",            # {{4}} pin
    "28 Apr 2027",     # {{5}} expiry
]

_VOUCHER_DELIVERED_FRIEND_HEADER = ["Karan Dumbre"]
_VOUCHER_DELIVERED_FRIEND_BODY = [
    "Karan Dumbre",                  # {{1}} sender
    "500",                           # {{2}} amount
    "your birthday",                 # {{3}} occasion phrase
    "Have an amazing year ahead!",   # {{4}} personal message
    "AMZN-1234-5678",                # {{5}} code
    "4827",                          # {{6}} pin
    "28 Apr 2027",                   # {{7}} expiry
]


async def main(phone: str) -> None:
    print(f"→ Sending voucher_delivered to {phone} …")
    try:
        r1 = await whatsapp_service.send_template(
            phone, "voucher_delivered", body_params=_VOUCHER_DELIVERED_BODY,
        )
        msg_id = (r1.get("messages") or [{}])[0].get("id", "?")
        print(f"  ✅ accepted, wamid={msg_id}")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        return

    print(f"→ Sending voucher_delivered_friend to {phone} …")
    try:
        r2 = await whatsapp_service.send_template(
            phone, "voucher_delivered_friend",
            header_params=_VOUCHER_DELIVERED_FRIEND_HEADER,
            body_params=_VOUCHER_DELIVERED_FRIEND_BODY,
        )
        msg_id = (r2.get("messages") or [{}])[0].get("id", "?")
        print(f"  ✅ accepted, wamid={msg_id}")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        return

    print()
    print("Both templates accepted by Meta. Open WhatsApp on the test phone and")
    print("confirm the rendered messages match the approved Meta previews exactly.")
    print("If they look right → safe to flip WHATSAPP_USE_VOUCHER_TEMPLATE=true.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m scripts.test_voucher_templates <phone_e164_no_plus>")
    asyncio.run(main(sys.argv[1]))
