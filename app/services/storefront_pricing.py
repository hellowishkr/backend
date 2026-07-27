"""Storefront base/discounted pricing plus an optional global flash campaign.

Normal mode charges the vendor's product-level discounted price (or base price
when no discount exists). Flash mode temporarily ignores that normal discounted
price and applies the admin markup + flash discount to the vendor base price.
The 90/10 vendor split still runs on the normal discounted price.
"""

from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_user import PricingSetting

# Fallback if the row is missing (fresh DB pre-migration): program DISABLED, so
# the customer is charged the raw vendor price. Never default to a live uplift —
# the storefront display would silently disagree with the charge.
_DEFAULT_MARKUP_PCT = Decimal("0")
_DEFAULT_FLASH_OFF_PCT = Decimal("0")
_DEFAULT_FLASH_ACTIVE = False


@dataclass(frozen=True)
class StorefrontPricing:
    markup_pct: Decimal
    flash_off_pct: Decimal
    flash_active: bool

    @property
    def list_multiplier(self) -> Decimal:
        """Flash list multiplier; neutral while the campaign is inactive."""
        if not self.flash_active:
            return Decimal(1)
        return Decimal(1) + self.markup_pct / Decimal(100)

    @property
    def charge_multiplier(self) -> Decimal:
        """Flash charge multiplier; neutral while the campaign is inactive."""
        if not self.flash_active:
            return Decimal(1)
        return self.list_multiplier * (Decimal(1) - self.flash_off_pct / Decimal(100))

    def _uplift_to_whole_rupee(self, vendor_paise: int, multiplier: Decimal) -> int:
        """Uplift a vendor amount (paise) and round UP to the next whole rupee.

        Customers must always see/pay whole rupees — no paise. Rounding up (never
        down) means the sub-rupee remainder is always Wishkr's margin, never a
        shave off it. Result is a multiple of 100 paise.
        """
        rupees = ceil(Decimal(vendor_paise) * multiplier / 100)
        return rupees * 100

    def charge_paise(
        self,
        base_paise: int,
        discounted_paise: int | None = None,
    ) -> int:
        """Customer charge in whole rupees.

        Flash active: base × (1 + markup) × (1 - flash discount).
        Flash inactive: vendor discounted price, falling back to base.
        """
        if not self.flash_active:
            normal_price = (
                discounted_paise if discounted_paise is not None else base_paise
            )
            return self._uplift_to_whole_rupee(normal_price, Decimal(1))
        return self._uplift_to_whole_rupee(base_paise, self.charge_multiplier)

    def list_paise(self, base_paise: int) -> int:
        """Vendor base normally; marked-up base during an active flash."""
        return self._uplift_to_whole_rupee(base_paise, self.list_multiplier)


_DISABLED = StorefrontPricing(_DEFAULT_MARKUP_PCT, _DEFAULT_FLASH_OFF_PCT, _DEFAULT_FLASH_ACTIVE)


async def get_storefront_pricing(db: AsyncSession) -> StorefrontPricing:
    # The pricing_settings table + seed row are guaranteed by migration
    # b3f8a1d47c60; a missing table is a deployment error that should surface,
    # not be silently masked as "no flash" (and masking it aborted the order
    # write's transaction on Postgres). If the row is somehow absent, fall back
    # to disabled — that path never leaves the session in an aborted state.
    row = (await db.execute(select(PricingSetting).limit(1))).scalar_one_or_none()
    if row is None:
        return _DISABLED
    return StorefrontPricing(
        markup_pct=Decimal(row.markup_pct),
        flash_off_pct=Decimal(row.flash_off_pct),
        flash_active=bool(row.flash_active),
    )
