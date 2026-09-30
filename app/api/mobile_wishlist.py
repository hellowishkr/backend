"""Mobile Wishlist API routes for wishkr_app.

Endpoints:
  GET    /api/v1/wishlist                      — List current user's personal wishlist
  POST   /api/v1/wishlist/link                 — Add item via link or manual details
  DELETE /api/v1/wishlist/{item_id}            — Remove item from wishlist
  GET    /api/v1/occasions/{id}/shared-wishlist — View friend's shared wishlist (enforces allowlist)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.mobile_auth import get_current_mobile_user
from app.db.session import get_db
from app.models.celebrant_occasion import CelebrantOccasion
from app.models.occasion import Occasion
from app.models.user import User
from app.models.wishlist_item import WishlistItem
from app.utils.id_gen import generate_id
from app.utils.link_utils import extract_product_id, guess_product_name, normalize_url

logger = logging.getLogger("wishkr.mobile_wishlist")

router = APIRouter(tags=["mobile_wishlist"])


def _clean_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    raw_digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not raw_digits.startswith("+"):
        if len(raw_digits) == 10:
            return "+91" + raw_digits
        elif raw_digits.startswith("91") and len(raw_digits) == 12:
            return "+" + raw_digits
        return raw_digits
    return raw_digits


# ── Pydantic Schemas ──────────────────────────────────────────────────────────


class WishlistItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    url: str
    product_id: str | None = None
    price: float | None = None
    image_url: str | None = None
    purpose: str = "self"
    recipient_occasion_id: str | None = None
    visibility: str = "friends"
    share_status: str = "available"
    created_at: str


class AddWishlistPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str | None = Field(None, description="Product URL e.g. Amazon, Flipkart, Myntra")
    title: str | None = Field(None, max_length=200, description="Product title")
    price: float | None = Field(None, ge=0, description="Estimated price in INR")
    image_url: str | None = Field(None, description="Product thumbnail URL")
    own_occasion_id: str | None = Field(None, description="Optional linked personal occasion")
    purpose: str = Field(
        "self", description="'self' for personal wishlist, 'gift' for recipient ideas"
    )
    recipient_occasion_id: str | None = Field(
        None, description="Associated occasion ID if purpose is gift"
    )
    visibility: str = Field("friends", description="private, friends, or public")


class SharedWishlistItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    url: str
    price: float | None = None
    image_url: str | None = None
    share_status: str
    reservation_expires_at: str | None = None
    is_reserved_by_me: bool = False
    is_purchased_by_me: bool = False
    is_taken: bool = False


class ReserveWishlistPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_id: str
    occasion_id: str


class ConfirmWishlistPurchasePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_id: str


class CancelWishlistReservationPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_id: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/v1/wishlist", response_model=list[WishlistItemResponse])
@router.get("/v1/wishlists/items", response_model=list[WishlistItemResponse])
async def list_my_wishlist(
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    purpose: str = "self",
    occasion_id: str | None = None,
) -> list[WishlistItemResponse]:
    """Retrieve personal wishlist items or recipient gift shortlist for the authenticated user."""
    stmt = select(WishlistItem).where(
        WishlistItem.user_id == user.id,
        WishlistItem.purpose == purpose,
    )
    if purpose == "gift" and occasion_id:
        stmt = stmt.where(WishlistItem.recipient_occasion_id == occasion_id)

    stmt = stmt.order_by(WishlistItem.created_at.desc())
    result = await db.scalars(stmt)
    items = list(result.all())

    return [
        WishlistItemResponse(
            id=item.id,
            title=item.title,
            url=item.url,
            product_id=item.product_id,
            price=None,  # extracted from URL / scraped if available
            image_url=None,
            purpose=item.purpose,
            recipient_occasion_id=item.recipient_occasion_id,
            visibility=item.visibility,
            share_status=item.share_status,
            created_at=item.created_at.isoformat()
            if item.created_at
            else datetime.now(timezone.utc).isoformat(),
        )
        for item in items
    ]


@router.post(
    "/v1/wishlist/link", response_model=WishlistItemResponse, status_code=status.HTTP_201_CREATED
)
@router.post(
    "/v1/wishlists/items", response_model=WishlistItemResponse, status_code=status.HTTP_201_CREATED
)
async def add_wishlist_item(
    payload: AddWishlistPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WishlistItemResponse:
    """Add a product link or manual item to the user's wishlist."""
    raw_url = payload.url.strip() if payload.url else None
    if not raw_url and not payload.title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Either a valid product URL or item title is required.",
        )

    final_url = normalize_url(raw_url) if raw_url else f"manual://item/{generate_id('itm')}"
    product_id = extract_product_id(raw_url) if raw_url else None

    # Derive title if not explicitly provided
    title = payload.title.strip() if payload.title else None
    if not title and raw_url:
        title = guess_product_name(raw_url) or "Wishlist Item"
    if not title:
        title = "Wishlist Item"

    item_id = generate_id("wsh")
    new_item = WishlistItem(
        id=item_id,
        user_id=user.id,
        product_id=product_id,
        url=final_url,
        title=title[:160],
        purpose=payload.purpose if payload.purpose in ("self", "gift") else "self",
        recipient_occasion_id=payload.recipient_occasion_id,
        own_occasion_id=payload.own_occasion_id,
        visibility=payload.visibility
        if payload.visibility in ("private", "friends", "public")
        else "friends",
        share_status="available",
    )
    db.add(new_item)
    await db.commit()
    await db.refresh(new_item)

    logger.info("User %s added wishlist item %s: %s", user.id, new_item.id, new_item.title)
    return WishlistItemResponse(
        id=new_item.id,
        title=new_item.title,
        url=new_item.url,
        product_id=new_item.product_id,
        price=payload.price,
        image_url=payload.image_url,
        purpose=new_item.purpose,
        recipient_occasion_id=new_item.recipient_occasion_id,
        visibility=new_item.visibility,
        share_status=new_item.share_status,
        created_at=new_item.created_at.isoformat()
        if new_item.created_at
        else datetime.now(timezone.utc).isoformat(),
    )


@router.delete("/v1/wishlist/{item_id}")
@router.delete("/v1/wishlists/items/{item_id}")
async def delete_wishlist_item(
    item_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Delete a wishlist item owned by the current user."""
    item = await db.scalar(
        select(WishlistItem).where(
            WishlistItem.id == item_id,
            WishlistItem.user_id == user.id,
        )
    )
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wishlist item not found",
        )

    await db.delete(item)
    await db.commit()
    logger.info("User %s deleted wishlist item %s", user.id, item_id)
    return {"status": "ok", "message": "Item deleted from wishlist"}


@router.get(
    "/v1/occasions/{occasion_id}/shared-wishlist",
    response_model=list[SharedWishlistItemResponse],
)
@router.get(
    "/v1/friends/wishlist/{occasion_id}",
    response_model=list[SharedWishlistItemResponse],
)
async def get_occasion_shared_wishlist(
    occasion_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SharedWishlistItemResponse]:
    """View a celebrant's shared wishlist for an occasion.

    🔒 Strictly enforces the Occasion Allowlist:
    If the celebration is selective and the requesting user is not in the
    authorized circle, raises HTTP 403 Forbidden.
    """
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occasion not found")

    is_owner = occ.user_id == user.id

    # If caller is not owner, check allowlist permissions
    if not is_owner:
        cel_occ = await db.scalar(
            select(CelebrantOccasion)
            .where(CelebrantOccasion.occasion_id == occasion_id)
            .options(selectinload(CelebrantOccasion.friends))
        )
        if cel_occ and cel_occ.friends:
            user_phone = _clean_phone(user.phone)
            allowed_phones = {f.phone for f in cel_occ.friends if f.phone}
            allowed_users = {f.user_id for f in cel_occ.friends if f.user_id}

            caller_authorized = (user_phone in allowed_phones) or (user.id in allowed_users)

            if not caller_authorized:
                logger.warning(
                    "Access denied for user %s (phone %s) to occasion %s wishlist",
                    user.id,
                    user_phone,
                    occasion_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "access_denied",
                        "message": "This celebration wishlist is private to invited circle members.",
                    },
                )

    # Fetch celebrant's self-wishlist items
    now = datetime.now(timezone.utc)
    stmt = (
        select(WishlistItem)
        .where(
            WishlistItem.user_id == occ.user_id,
            WishlistItem.purpose == "self",
        )
        .order_by(WishlistItem.created_at.desc())
    )
    result = await db.scalars(stmt)
    items = list(result.all())

    response_items = []
    for item in items:
        status_val = item.share_status or "available"
        # Auto-expire reservations past 48h
        if status_val == "reserved" and item.reservation_expires_at:
            exp = item.reservation_expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now >= exp:
                status_val = "available"

        if is_owner:
            # 🎁 Surprise-safe: celebrant sees neutral "on_wishlist" status; never who claimed or bought!
            response_items.append(
                SharedWishlistItemResponse(
                    id=item.id,
                    title=item.title,
                    url=item.url,
                    price=None,
                    image_url=None,
                    share_status="on_wishlist",
                    is_reserved_by_me=False,
                    is_purchased_by_me=False,
                    is_taken=False,
                    reservation_expires_at=None,
                )
            )
        else:
            is_reserved_by_me = item.reserved_by_user_id == user.id and status_val == "reserved"
            is_purchased_by_me = item.purchased_by_user_id == user.id
            is_taken = (
                status_val in ("reserved", "purchased")
                and not is_reserved_by_me
                and not is_purchased_by_me
            )
            expires_str = (
                item.reservation_expires_at.isoformat()
                if (item.reservation_expires_at and status_val == "reserved")
                else None
            )

            response_items.append(
                SharedWishlistItemResponse(
                    id=item.id,
                    title=item.title,
                    url=item.url,
                    price=None,
                    image_url=None,
                    share_status=status_val,
                    is_reserved_by_me=is_reserved_by_me,
                    is_purchased_by_me=is_purchased_by_me,
                    is_taken=is_taken,
                    reservation_expires_at=expires_str,
                )
            )

    return response_items


@router.post("/v1/friends/wishlist/reserve")
async def reserve_wishlist_item(
    payload: ReserveWishlistPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Atomically reserve a friend's wishlist item for 48 hours."""
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == payload.occasion_id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occasion not found")

    # If celebration has selective sharing, enforce allowlist
    cel_occ = await db.scalar(
        select(CelebrantOccasion)
        .where(CelebrantOccasion.occasion_id == payload.occasion_id)
        .options(selectinload(CelebrantOccasion.friends))
    )
    if cel_occ and cel_occ.friends:
        user_phone = _clean_phone(user.phone)
        allowed_phones = {f.phone for f in cel_occ.friends if f.phone}
        allowed_users = {f.user_id for f in cel_occ.friends if f.user_id}
        if (user_phone not in allowed_phones) and (user.id not in allowed_users):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "access_denied",
                    "message": "You are not authorized for this celebration.",
                },
            )

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=48)

    # Atomic conditional update: only succeeds if item is available or its reservation has expired.
    # This guarantees race-free execution across concurrent requests in ANY database.
    update_stmt = (
        update(WishlistItem)
        .where(
            WishlistItem.id == payload.item_id,
            WishlistItem.user_id == occ.user_id,
            WishlistItem.purpose == "self",
            or_(
                WishlistItem.share_status.is_(None),
                WishlistItem.share_status == "available",
                and_(
                    WishlistItem.share_status == "reserved",
                    WishlistItem.reservation_expires_at.is_not(None),
                    WishlistItem.reservation_expires_at <= now,
                ),
                and_(
                    WishlistItem.share_status == "reserved",
                    WishlistItem.reserved_by_user_id == user.id,
                ),
            ),
        )
        .values(
            share_status="reserved",
            reserved_by_user_id=user.id,
            reserved_at=now,
            reservation_expires_at=expires,
        )
    )
    result = await db.execute(update_stmt)
    await db.commit()

    if result.rowcount == 0:
        # Determine specific failure reason (not found, already purchased, or reserved by another)
        current = await db.scalar(
            select(WishlistItem).where(
                WishlistItem.id == payload.item_id,
                WishlistItem.user_id == occ.user_id,
                WishlistItem.purpose == "self",
            )
        )
        if not current:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wishlist item not found")
        if current.share_status == "purchased":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This item has already been purchased by a friend.",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This item is currently reserved by another friend.",
        )

    logger.info("User %s reserved wishlist item %s until %s", user.id, payload.item_id, expires.isoformat())
    return {
        "status": "reserved",
        "item_id": payload.item_id,
        "reservation_expires_at": expires.isoformat(),
        "message": "Item reserved for 48 hours! Other friends will see it as taken.",
    }


@router.post("/v1/friends/wishlist/confirm")
async def confirm_wishlist_purchase(
    payload: ConfirmWishlistPurchasePayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Confirm purchase of a reserved wishlist item."""
    item = await db.scalar(
        select(WishlistItem).where(
            WishlistItem.id == payload.item_id,
            WishlistItem.purpose == "self",
        )
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wishlist item not found")

    now = datetime.now(timezone.utc)
    effective = item.share_status or "available"
    if effective == "reserved" and item.reservation_expires_at:
        exp = item.reservation_expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now >= exp:
            effective = "available"

    if effective == "purchased" and item.purchased_by_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This item was already purchased by another friend.",
        )
    if effective == "reserved" and item.reserved_by_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your reservation has expired and the item was claimed by another friend.",
        )

    item.purchased_by_user_id = user.id
    item.purchased_at = now
    item.share_status = "purchased"

    await db.commit()
    logger.info("User %s confirmed purchase for wishlist item %s", user.id, item.id)
    return {
        "status": "purchased",
        "item_id": item.id,
        "message": "Purchase confirmed! The surprise is kept safe from the celebrant.",
    }


@router.post("/v1/friends/wishlist/cancel")
async def cancel_wishlist_reservation(
    payload: CancelWishlistReservationPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Release a 48-hour reservation so other friends can claim the item."""
    item = await db.scalar(
        select(WishlistItem).where(
            WishlistItem.id == payload.item_id,
            WishlistItem.purpose == "self",
        )
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wishlist item not found")

    if item.reserved_by_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only cancel your own reservation.",
        )

    item.reserved_by_user_id = None
    item.reserved_at = None
    item.reservation_expires_at = None
    item.share_status = "available"

    await db.commit()
    logger.info("User %s cancelled reservation for wishlist item %s", user.id, item.id)
    return {
        "status": "cancelled",
        "item_id": item.id,
        "message": "Reservation released.",
    }
