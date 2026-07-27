"""Vendor "new product" submission semantics.

A vendor's "new" editor always submits with product_id=None. Two vendor
proposals can therefore arrive with the same product_id=None marker, and the
backend has to decide whether a second one is:
(a) the vendor still editing their *same, not-yet-reviewed* proposal (should
    update that one pending row, so the admin reviews one item with the
    latest details), or
(b) a deliberate second, distinct new product (once the first proposal has
    been approved or rejected — should create a new pending change/product).

A previous implementation (_resolve_new_product_target, matching by "most
recent product this vendor's new-editor ever created", with no regard to
whether that proposal was still pending) got this wrong: it folded EVERY
subsequent "new" submission into an edit of the vendor's very first created
product, indefinitely — so a vendor adding a genuinely new product days
after their first approval silently overwrote the first product's fields
instead of creating anything.

A follow-up fix matched "still pending" proposals by (vendor_id, product_id
IS NULL, status=pending) alone — better, but still couldn't distinguish two
*different* new-product proposals from the same vendor if both were pending
at once (e.g. vendor starts product B before admin reviews product A): B's
submission would silently overwrite A's payload, since both are
product_id=None. It could also crash with MultipleResultsFound if more than
one such row ever existed (which historical/legacy data already had).

draft_id — a client-generated id, stable across resubmits of the same
proposal, fresh per "Add product" click — gives each proposal its own
identity so matching is exact, and a partial unique index on
(vendor_id, draft_id) enforces this at the DB level too. See
app.api.vendor_edit._find_unreviewed_new_product_change.
"""

import hashlib

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import vendor_edit
from app.api.vendor_edit import (
    PendingSubmission,
    approve_change,
    edit_pending_change,
    preview_with_draft,
    save_draft,
    submit_change,
)
from app.db.base import Base
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor
from app.models.vendor_edit import ProductPendingChange, VendorEditToken

RAW_TOKEN = "regression-token-123"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Vendor.__table__,
                Product.__table__,
                ProductVariant.__table__,
                ProductAddon.__table__,
                VendorEditToken.__table__,
                ProductPendingChange.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(db: AsyncSession) -> None:
    db.add(Vendor(id="ven_1", name="PyariSakhee", category="jewellery", active=True))
    db.add(
        VendorEditToken(
            id="vet_1",
            vendor_id="ven_1",
            token_hash=hashlib.sha256(RAW_TOKEN.encode()).hexdigest(),
        )
    )
    await db.commit()


async def _product_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(Product))).scalar_one()


@pytest.mark.asyncio
async def test_new_submission_after_approval_creates_a_second_product(session_factory):
    """Regression: a vendor submitting a genuinely new product from the "new"
    editor, days after their first new-product proposal was approved, must
    get a second distinct product — not have it folded into an edit of the
    first one (the actual reported bug: PyariSakhee's second and third new
    bracelets silently overwrote their first bracelet's name/fields)."""
    async with session_factory() as db:
        await _seed(db)

        # 1. Vendor submits a brand-new product (product_id=None) and admin approves.
        r1 = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None,
                draft_id="draft-emerald",
                fields={"name": "Emerald Link Green Stone Bracelet", "base_price_paise": 31900},
            ),
            db,
        )
        approved1 = await approve_change(r1["change_id"], db)
        created_id = approved1["product_id"]
        assert await _product_count(db) == 1

    # 2. Later, the vendor submits a *different* new product (a new draft_id,
    # e.g. from a fresh "Add product" click), still with product_id=None since
    # that's the only value the "new" editor ever sends.
    async with session_factory() as db:
        r2 = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None,
                draft_id="draft-tulip",
                fields={"name": "Tulip Bracelet - Royal Red Crystals", "base_price_paise": 42900},
            ),
            db,
        )
        # Must NOT be folded onto the already-approved first product.
        change2 = await db.get(ProductPendingChange, r2["change_id"])
        assert change2.product_id is None

        approved2 = await approve_change(r2["change_id"], db)
        assert approved2["product_id"] != created_id

        # Two distinct products now exist, and the first one's name is untouched.
        assert await _product_count(db) == 2
        first_product = await db.get(Product, created_id)
        assert first_product.name == "Emerald Link Green Stone Bracelet"


@pytest.mark.asyncio
async def test_resubmit_still_pending_new_product_updates_same_change(session_factory):
    """If the admin hasn't reviewed the vendor's new-product proposal yet and
    the vendor edits it again from the "new" editor (same draft_id — the
    editor tab wasn't closed), the second submission should update the same
    pending change (one review item with the latest details) rather than
    creating a second, separate pending proposal."""
    async with session_factory() as db:
        await _seed(db)

        r1 = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None, draft_id="draft-a", fields={"name": "Draft Name", "base_price_paise": 1000}
            ),
            db,
        )

        r2 = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None, draft_id="draft-a", fields={"name": "Final Name", "base_price_paise": 1200}
            ),
            db,
        )

        # Same pending change, updated in place.
        assert r2["change_id"] == r1["change_id"]
        pending = (
            await db.execute(
                select(ProductPendingChange).where(
                    ProductPendingChange.vendor_id == "ven_1",
                    ProductPendingChange.status == "pending",
                )
            )
        ).scalars().all()
        assert len(pending) == 1
        assert pending[0].payload["name"] == "Final Name"

        approved = await approve_change(r2["change_id"], db)
        assert await _product_count(db) == 1
        product = await db.get(Product, approved["product_id"])
        assert product.name == "Final Name"


@pytest.mark.asyncio
async def test_two_distinct_pending_new_products_do_not_collide(session_factory):
    """Regression (P1): a vendor starting a second, distinct new-product
    proposal (different draft_id) while a first one is still awaiting review
    must NOT overwrite the first proposal's payload — both must survive as
    independent pending changes, each approvable into its own product."""
    async with session_factory() as db:
        await _seed(db)

        r1 = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None, draft_id="draft-a", fields={"name": "Product A", "base_price_paise": 1000}
            ),
            db,
        )
        r2 = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None, draft_id="draft-b", fields={"name": "Product B", "base_price_paise": 2000}
            ),
            db,
        )

        assert r1["change_id"] != r2["change_id"]
        pending = (
            await db.execute(
                select(ProductPendingChange).where(
                    ProductPendingChange.vendor_id == "ven_1",
                    ProductPendingChange.status == "pending",
                )
            )
        ).scalars().all()
        assert {p.payload["name"] for p in pending} == {"Product A", "Product B"}

        await approve_change(r1["change_id"], db)
        await approve_change(r2["change_id"], db)
        assert await _product_count(db) == 2


@pytest.mark.asyncio
async def test_concurrent_submit_same_draft_id_recovers_instead_of_500(session_factory, monkeypatch):
    """Regression (P2): two concurrent submissions for the same draft_id can
    both observe "no pending row yet" before either commits, then race to
    insert — the partial unique index on (vendor_id, draft_id) rejects the
    second at commit. That must recover by falling back to updating the
    now-existing row, not surface as a raw 500."""
    async with session_factory() as db:
        await _seed(db)

        # Simulate "request B's" pre-insert check racing ahead of request A's
        # commit: patch the lookup to report "nothing pending yet" once, then
        # actually commit a conflicting row — via a SEPARATE session, so it
        # persists independently of request A's own transaction/rollback,
        # exactly like a genuinely concurrent request would — before
        # submit_change's own insert attempt reaches the DB.
        original_finder = vendor_edit._find_unreviewed_new_product_change
        call_count = 0

        async def racy_finder(db_, vendor_id_, draft_id_):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async with session_factory() as other_db:
                    other_db.add(
                        ProductPendingChange(
                            id="ppc_race_winner",
                            vendor_id=vendor_id_,
                            product_id=None,
                            draft_id=draft_id_,
                            payload={"name": "Won The Race"},
                            status="pending",
                        )
                    )
                    await other_db.commit()
                return None
            return await original_finder(db_, vendor_id_, draft_id_)

        monkeypatch.setattr(vendor_edit, "_find_unreviewed_new_product_change", racy_finder)

        result = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None, draft_id="draft-race", fields={"name": "Lost The Race", "base_price_paise": 500}
            ),
            db,
        )

        # No 500: recovered by falling back to the row that won the race, and
        # updated it with this request's payload rather than losing it.
        assert result["change_id"] == "ppc_race_winner"
        pending = (
            await db.execute(
                select(ProductPendingChange).where(
                    ProductPendingChange.vendor_id == "ven_1",
                    ProductPendingChange.status == "pending",
                )
            )
        ).scalars().all()
        assert len(pending) == 1
        assert pending[0].payload["name"] == "Lost The Race"


@pytest.mark.asyncio
async def test_concurrent_save_draft_same_draft_id_recovers_instead_of_500(session_factory, monkeypatch):
    """Regression (P2): two concurrent autosaves for the same new-product
    draft_id can both observe "no draft row yet" before either commits, then
    race to insert — the partial unique index on (vendor_id, draft_id) where
    status='draft' rejects the second at commit. That must recover by
    falling back to updating the now-existing row, not surface as a 500 or
    leave a duplicate draft row behind."""
    async with session_factory() as db:
        await _seed(db)

        original_finder = vendor_edit._find_existing_draft
        call_count = 0

        async def racy_finder(db_, vendor_id_, *, product_id, draft_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async with session_factory() as other_db:
                    other_db.add(
                        ProductPendingChange(
                            id="ppc_draft_race_winner",
                            vendor_id=vendor_id_,
                            product_id=None,
                            draft_id=draft_id,
                            payload={"name": "Won The Draft Race"},
                            status="draft",
                        )
                    )
                    await other_db.commit()
                return None
            return await original_finder(db_, vendor_id_, product_id=product_id, draft_id=draft_id)

        monkeypatch.setattr(vendor_edit, "_find_existing_draft", racy_finder)

        result = await save_draft(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None, draft_id="draft-race-2", fields={"name": "Lost The Draft Race"}
            ),
            db,
        )

        # No 500: recovered by falling back to the row that won the race, and
        # updated it with this request's payload rather than leaving a
        # duplicate draft row.
        assert result["change_id"] == "ppc_draft_race_winner"
        drafts = (
            await db.execute(
                select(ProductPendingChange).where(
                    ProductPendingChange.vendor_id == "ven_1",
                    ProductPendingChange.status == "draft",
                )
            )
        ).scalars().all()
        assert len(drafts) == 1
        assert drafts[0].payload["name"] == "Lost The Draft Race"


@pytest.mark.asyncio
async def test_first_new_product_still_creates(session_factory):
    """Guard: the folding must not swallow a genuine first new-product proposal."""
    async with session_factory() as db:
        await _seed(db)
        r = await submit_change(
            RAW_TOKEN,
            PendingSubmission(product_id=None, draft_id="draft-first", fields={"name": "First Product"}),
            db,
        )
        change = await db.get(ProductPendingChange, r["change_id"])
        assert change.product_id is None  # nothing created yet -> genuine create
        await approve_change(r["change_id"], db)
        assert await _product_count(db) == 1


@pytest.mark.asyncio
async def test_preview_with_draft_variants_does_not_crash(session_factory):
    """Regression: previewing a live product with a draft that edits variants
    used to raise AttributeError('dict' object has no attribute
    '_sa_instance_state') because the draft's raw variant dicts were setattr'd
    straight onto product.variants, a session-tracked ORM relationship."""
    async with session_factory() as db:
        await _seed(db)
        product = Product(
            id="prd_1",
            vendor_id="ven_1",
            name="Emerald Link Green Stone Bracelet",
            base_price_paise=31900,
            active=True,
            variants=[
                ProductVariant(id="var_1", name="Small", price_delta_paise=0, sort_order=1)
            ],
            addons=[],
        )
        db.add(product)
        await db.commit()

        await save_draft(
            RAW_TOKEN,
            PendingSubmission(
                product_id="prd_1",
                fields={
                    "name": "Emerald Link Green Stone Bracelet",
                    "variants": [
                        {"id": "var_1", "name": "Small", "price_delta_paise": 0, "sort_order": 1},
                        {"name": "Large", "price_delta_paise": 500, "sort_order": 2},
                    ],
                },
            ),
            db,
        )

    async with session_factory() as db:
        result = await preview_with_draft(RAW_TOKEN, "prd_1", db)

    variant_names = {v["name"] for v in result["product"]["variants"]}
    assert variant_names == {"Small", "Large"}


@pytest.mark.asyncio
async def test_admin_can_edit_pending_change_before_approving(session_factory):
    """Admin review previously only offered approve/reject verbatim. Admins must
    be able to correct a vendor's submission (e.g. a typo'd price) before it's
    applied to the live product."""
    async with session_factory() as db:
        await _seed(db)
        r = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id=None,
                draft_id="draft-emerald",
                fields={"name": "Emerald Link Green Stone Bracelet", "base_price_paise": 31900},
            ),
            db,
        )
        change_id = r["change_id"]

        edited = await edit_pending_change(
            change_id,
            PendingSubmission(
                fields={"name": "Emerald Link Green Stone Bracelet", "base_price_paise": 29900},
            ),
            db,
        )
        assert edited["payload"]["base_price_paise"] == 29900

        approved = await approve_change(change_id, db)
        product = await db.get(Product, approved["product_id"])
        assert product.base_price_paise == 29900


@pytest.mark.asyncio
async def test_approve_addon_edit_without_id_updates_in_place(session_factory):
    """Regression: a vendor's edit form dropped the existing add-on's id (a
    stale merged draft overwrote it), so approving a price-only change to an
    existing add-on tried to INSERT a new row and hit the (product_id, name)
    unique constraint, surfacing as a 500. sync_product_children must fall
    back to matching by name when id is missing/unmatched.

    This product also carries a pre-existing variant that the edit does NOT
    touch, to guard against a second regression: an addon-only submission
    must not wipe variants (see test_approve_addon_only_edit_preserves_variants
    for the dedicated case)."""
    async with session_factory() as db:
        await _seed(db)
        product = Product(
            id="prd_1",
            vendor_id="ven_1",
            name="Braided Round Decorative Candle",
            base_price_paise=19900,
            active=True,
            variants=[ProductVariant(id="var_1", name="Pink", price_delta_paise=0, sort_order=1)],
            addons=[ProductAddon(id="add_1", name="Gift Wrap", price_paise=2000, active=True, sort_order=1)],
        )
        db.add(product)
        await db.commit()

        r = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id="prd_1",
                fields={
                    "name": "Braided Round Decorative Candle",
                    "addons": [
                        # No "id" — mirrors the real-world payload that caused the 500.
                        {"name": "Gift Wrap", "price_paise": 2800, "active": True, "sort_order": 1},
                    ],
                },
            ),
            db,
        )

        approved = await approve_change(r["change_id"], db)
        assert approved["product_id"] == "prd_1"

    async with session_factory() as db:
        refreshed = await db.get(Product, "prd_1")
        await db.refresh(refreshed, attribute_names=["addons", "variants"])
        assert len(refreshed.addons) == 1
        assert refreshed.addons[0].id == "add_1"
        assert refreshed.addons[0].price_paise == 2800
        # The addon-only edit must not have touched the untouched variant.
        assert len(refreshed.variants) == 1
        assert refreshed.variants[0].id == "var_1"


@pytest.mark.asyncio
async def test_approve_addon_only_edit_preserves_variants(session_factory):
    """Regression: approve_change only skipped sync_product_children entirely
    when NEITHER variants nor addons were submitted. A submission with only
    addons still called sync_product_children(variants=[], addons=[...]),
    and treating an empty variants list as "the vendor wants zero variants"
    deleted every existing variant. Each collection must be reconciled
    independently of whether the other one was submitted."""
    async with session_factory() as db:
        await _seed(db)
        product = Product(
            id="prd_2",
            vendor_id="ven_1",
            name="Braided Round Decorative Candle",
            base_price_paise=19900,
            active=True,
            variants=[
                ProductVariant(id="var_a", name="Pink", price_delta_paise=0, sort_order=1),
                ProductVariant(id="var_b", name="Blue", price_delta_paise=0, sort_order=2),
            ],
            addons=[],
        )
        db.add(product)
        await db.commit()

        r = await submit_change(
            RAW_TOKEN,
            PendingSubmission(
                product_id="prd_2",
                fields={
                    "name": "Braided Round Decorative Candle",
                    "addons": [{"name": "Gift Wrap", "price_paise": 2800, "active": True, "sort_order": 1}],
                },
            ),
            db,
        )

        await approve_change(r["change_id"], db)

    async with session_factory() as db:
        refreshed = await db.get(Product, "prd_2")
        await db.refresh(refreshed, attribute_names=["variants", "addons"])
        assert {v.id for v in refreshed.variants} == {"var_a", "var_b"}
        assert len(refreshed.addons) == 1
