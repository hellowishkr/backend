"""Add vendor_app_downloads table for desktop-app download gate leads.

Revision ID: a7d3e92f81c4
Revises: 95b02584a118
Create Date: 2026-05-21

Lightweight contact-capture distinct from vendor_leads (full partner
application). Stores who downloaded the desktop installer + which build,
so we can follow up about how the app is performing.
"""

from alembic import op
import sqlalchemy as sa

revision = "a7d3e92f81c4"
down_revision = "95b02584a118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_app_downloads",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("contact_name", sa.String(100), nullable=False),
        sa.Column("brand_name", sa.String(120), nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("platform", sa.String(20), nullable=True),
        sa.Column("app_version", sa.String(20), nullable=True),
        sa.Column("consent_given", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.String(40), nullable=True),
        sa.Column("consent_text_hash", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_vendor_app_downloads_email", "vendor_app_downloads", ["email"])
    op.create_index("ix_vendor_app_downloads_status", "vendor_app_downloads", ["status"])
    op.create_index(
        "ix_vendor_app_downloads_created_at", "vendor_app_downloads", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_vendor_app_downloads_created_at", table_name="vendor_app_downloads")
    op.drop_index("ix_vendor_app_downloads_status", table_name="vendor_app_downloads")
    op.drop_index("ix_vendor_app_downloads_email", table_name="vendor_app_downloads")
    op.drop_table("vendor_app_downloads")
