from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, Contact, Occasion, Vendor, VendorCategoryLink, VendorCity
from app.utils.cities import resolve_city_id

import logging
logger = logging.getLogger("wishkr.tools.functions")


async def set_contact_city(db: AsyncSession, phone: str, args: dict) -> dict:
    """Record a contact's city so the bot can suggest local vendors for them."""
    contact_id = args.get("contact_id", "").strip()
    city_input = (args.get("city") or "").strip()
    if not contact_id or not city_input:
        return {"success": False, "error": "contact_id and city are required"}

    user_res = await db.execute(select(User).where(User.phone == phone))
    user = user_res.scalar_one_or_none()
    if not user:
        return {"success": False, "error": "User not registered"}

    contact = await db.get(Contact, contact_id)
    if not contact or contact.user_id != user.id:
        return {"success": False, "error": "Contact not found"}

    resolution = await resolve_city_id(db, city_input)
    contact.city_id = resolution.city_id
    contact.city_raw = resolution.city_raw
    db.add(contact)
    await db.flush()
    return {"success": True, "contact_id": contact_id, "city": resolution.city_raw}


async def search_vendors(db: AsyncSession, phone: str, args: dict) -> dict:
    """Find active vendors that can fulfil a gift for a given occasion's recipient.

    National vendors (gift cards) always returned. Local vendors if the recipient's
    city_id matches a VendorCity row — works for both verified and pending cities,
    so admin-onboarded vendors surface even when the contact's city was auto-created.
    Never surfaces supply gaps to the user — bot stays silent on missing local options.
    """
    category = (args.get("category") or "").strip().lower()
    occasion_id = (args.get("occasion_id") or "").strip()

    recipient_city_id: str | None = None
    recipient_name: str | None = None
    if occasion_id:
        occ = await db.get(Occasion, occasion_id)
        if occ:
            contact = await db.get(Contact, occ.contact_id) if occ.contact_id else None
            if contact:
                recipient_name = contact.name
                recipient_city_id = contact.city_id

    # Single query: national vendors OR local vendors joined on VendorCity for
    # the recipient's city. Avoids N+1 across the vendor list.
    base = select(Vendor).where(Vendor.active.is_(True))
    if category:
        # Match primary category OR any linked category (multi-category vendors).
        base = base.where(
            or_(
                Vendor.category == category,
                Vendor.category_links.any(VendorCategoryLink.category_id == category),
            )
        )

    if recipient_city_id:
        local_q = (
            base.join(VendorCity, VendorCity.vendor_id == Vendor.id)
                .where(VendorCity.city_id == recipient_city_id)
                .where(Vendor.is_national.is_(False))
        )
        national_q = base.where(Vendor.is_national.is_(True))
        local_rows = (await db.execute(local_q)).scalars().unique().all()
        national_rows = (await db.execute(national_q)).scalars().all()
    else:
        local_rows = []
        national_rows = (await db.execute(base.where(Vendor.is_national.is_(True)))).scalars().all()

    national = [
        {"id": v.id, "name": v.name, "category": v.category, "scope": "national"}
        for v in national_rows
    ]
    local = [
        {"id": v.id, "name": v.name, "category": v.category, "scope": "local"}
        for v in local_rows
    ]

    return {
        "success": True,
        "recipient_name": recipient_name,
        "vendors": national + local,
        "count": len(national) + len(local),
    }
