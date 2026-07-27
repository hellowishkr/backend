"""vendor_category_links join table (vendor many-to-many categories)

Revision ID: e7b2c4a9d105
Revises: d3a9f1c7b502
Create Date: 2026-06-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b2c4a9d105"
down_revision: Union[str, None] = "d3a9f1c7b502"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendor_category_links",
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("category_id", sa.String(length=60), nullable=False),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["vendor_categories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vendor_id", "category_id"),
    )
    op.create_index(
        "ix_vendor_category_links_category_id",
        "vendor_category_links",
        ["category_id"],
    )

    # Backfill: seed the link table from each vendor's existing primary
    # category. The vendor_categories row is created if missing (slug = lower,
    # spaces/punct -> underscore) so the FK holds.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, category FROM vendors WHERE category IS NOT NULL AND category <> ''")
    ).fetchall()
    for vendor_id, category in rows:
        slug = _slugify(category)
        conn.execute(
            sa.text(
                "INSERT INTO vendor_categories (id, name, auto_created) "
                "VALUES (:id, :name, true) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": slug, "name": category},
        )
        conn.execute(
            sa.text(
                "INSERT INTO vendor_category_links (vendor_id, category_id) "
                "VALUES (:v, :c) ON CONFLICT DO NOTHING"
            ),
            {"v": vendor_id, "c": slug},
        )


def _slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug[:60] or "uncategorised"


def downgrade() -> None:
    op.drop_index("ix_vendor_category_links_category_id", table_name="vendor_category_links")
    op.drop_table("vendor_category_links")
