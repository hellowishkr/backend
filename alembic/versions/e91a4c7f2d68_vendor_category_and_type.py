"""add vendor_categories lookup table (seeded from existing vendor categories) and vendor_type column

Revision ID: e91a4c7f2d68
Revises: d7f2b3a8e145
Create Date: 2026-06-24 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e91a4c7f2d68"
down_revision: Union[str, None] = "d7f2b3a8e145"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "uncategorised"


def upgrade() -> None:
    op.create_table(
        "vendor_categories",
        sa.Column("id", sa.String(length=60), primary_key=True),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("auto_created", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "vendors",
        sa.Column("vendor_type", sa.String(length=20), nullable=False, server_default="normal"),
    )

    # Seed the lookup table from whatever category strings vendors already use,
    # so the new dropdown isn't empty on day one.
    conn = op.get_bind()
    existing = conn.execute(sa.text("SELECT DISTINCT category FROM vendors WHERE category IS NOT NULL")).fetchall()
    table = sa.table(
        "vendor_categories",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("auto_created", sa.Boolean),
    )
    seen_ids: set[str] = set()
    rows = []
    for (category,) in existing:
        if not category:
            continue
        slug = _slugify(category)
        if slug in seen_ids:
            continue
        seen_ids.add(slug)
        rows.append({"id": slug, "name": category, "auto_created": False})
    if rows:
        op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_column("vendors", "vendor_type")
    op.drop_table("vendor_categories")
