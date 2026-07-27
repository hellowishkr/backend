"""catalog taxonomy tables — product categories + gifting occasions

Categories and occasions were hardcoded constants duplicated in wishkr-admin
(lib.ts) and wishkr-web (ShopClient.tsx). This makes the DB the single source
of truth (served by GET /api/catalog/taxonomy, Redis-cached) and seeds it with
the existing values plus the full Indian gifting-occasion list.

Slugs "birthday", "anniversary", "diwali", "rakhi", "just-because" and
"corporate" already exist on live products' occasions arrays — they are kept
verbatim so no product data needs rewriting.

Revision ID: a4f8c2d1e6b9
Revises: e91b3c5f7a20
Create Date: 2026-07-06 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4f8c2d1e6b9"
down_revision: Union[str, None] = "e91b3c5f7a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CATEGORIES = [
    "Flowers",
    "Cakes",
    "Chocolates",
    "Candles",
    "Jewellery",
    "Hampers",
    "Personalized",
    "Experiences",
]

OCCASIONS = [
    # (slug, label) — romance & life events first, then festivals, then the rest.
    ("birthday", "Birthday"),
    ("anniversary", "Anniversary"),
    ("wedding", "Wedding"),
    ("engagement", "Engagement"),
    ("proposal", "Proposal"),
    ("romantic-surprise", "Romantic Surprise"),
    ("valentines-day", "Valentine's Day"),
    ("mothers-day", "Mother's Day"),
    ("fathers-day", "Father's Day"),
    ("diwali", "Diwali"),
    ("rakhi", "Rakhi"),
    ("bhai-dooj", "Bhai Dooj"),
    ("karwa-chauth", "Karwa Chauth"),
    ("holi", "Holi"),
    ("eid", "Eid"),
    ("christmas", "Christmas"),
    ("new-year", "New Year"),
    ("housewarming", "Housewarming"),
    ("baby-shower", "Baby Shower"),
    ("congratulations", "Congratulations"),
    ("thank-you", "Thank You"),
    ("get-well-soon", "Get Well Soon"),
    ("farewell", "Farewell"),
    ("corporate", "Corporate"),
    ("premium-gifting", "Premium Gifting"),
    ("just-because", "Just Because"),
]


def upgrade() -> None:
    categories = op.create_table(
        "product_categories",
        sa.Column("id", sa.String(length=60), primary_key=True),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    occasions = op.create_table(
        "gifting_occasions",
        sa.Column("id", sa.String(length=60), primary_key=True),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.bulk_insert(
        categories,
        [
            {"id": name.lower(), "name": name, "sort_order": i}
            for i, name in enumerate(CATEGORIES)
        ],
    )
    op.bulk_insert(
        occasions,
        [
            {"id": slug, "name": label, "sort_order": i}
            for i, (slug, label) in enumerate(OCCASIONS)
        ],
    )


def downgrade() -> None:
    op.drop_table("gifting_occasions")
    op.drop_table("product_categories")
