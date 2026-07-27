"""cities, vendors, vendor_cities + city_id FKs on users/contacts,
city_raw fields, and vendor_leads with DPDP consent versioning.

Revision ID: d7c94a1f2e58
Revises: e9f1c27a8b04
Create Date: 2026-04-21 19:00:00.000000

"""
import json as _json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7c94a1f2e58"
down_revision: Union[str, None] = "e9f1c27a8b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEED_CITIES = [
    ("pune", "Pune", "Maharashtra", ["Poona"]),
    ("mumbai", "Mumbai", "Maharashtra", ["Bombay"]),
    ("delhi-ncr", "Delhi NCR", "Delhi", ["Delhi", "New Delhi", "Gurgaon", "Gurugram", "Noida"]),
    ("bangalore", "Bangalore", "Karnataka", ["Bengaluru"]),
    ("hyderabad", "Hyderabad", "Telangana", []),
    ("chennai", "Chennai", "Tamil Nadu", ["Madras"]),
    ("kolkata", "Kolkata", "West Bengal", ["Calcutta"]),
    ("ahmedabad", "Ahmedabad", "Gujarat", []),
    ("jaipur", "Jaipur", "Rajasthan", []),
]


def upgrade() -> None:
    # cities — includes status/auto_created/country from day one
    op.create_table(
        "cities",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("state", sa.String(length=60), nullable=True),
        sa.Column("country", sa.String(length=60), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="verified"),
        sa.Column("auto_created", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_cities_status_country", "cities", ["status", "country"])

    # vendors
    op.create_table(
        "vendors",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=60), nullable=False),
        sa.Column("contact_info", sa.Text(), nullable=True),
        sa.Column("is_national", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_vendors_category", "vendors", ["category"])
    op.create_index("ix_vendors_is_national", "vendors", ["is_national"])
    op.create_index("ix_vendors_active", "vendors", ["active"])

    # vendor_cities (many-to-many)
    op.create_table(
        "vendor_cities",
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("city_id", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["city_id"], ["cities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vendor_id", "city_id"),
    )

    # Seed verified Indian cities
    cities_table = sa.table(
        "cities",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("state", sa.String),
        sa.column("country", sa.String),
        sa.column("aliases", sa.JSON),
        sa.column("status", sa.String),
        sa.column("auto_created", sa.Boolean),
    )
    op.bulk_insert(
        cities_table,
        [
            {
                "id": cid, "name": name, "state": state,
                "country": "India", "aliases": aliases,
                "status": "verified", "auto_created": False,
            }
            for cid, name, state, aliases in SEED_CITIES
        ],
    )

    # users.city (string) → users.city_id (FK) + city_raw preservation
    op.add_column("users", sa.Column("city_id", sa.String(length=40), nullable=True))
    op.add_column("users", sa.Column("city_raw", sa.String(length=60), nullable=True))
    op.create_foreign_key("fk_users_city_id", "users", "cities", ["city_id"], ["id"])
    op.execute(
        """
        UPDATE users u
        SET city_id = c.id, city_raw = u.city
        FROM cities c
        WHERE LOWER(u.city) = LOWER(c.name)
        """
    )
    # Preserve raw input even if no city matched
    op.execute("UPDATE users SET city_raw = city WHERE city_raw IS NULL AND city IS NOT NULL")
    op.drop_column("users", "city")

    # contacts.city_id + city_raw
    op.add_column("contacts", sa.Column("city_id", sa.String(length=40), nullable=True))
    op.add_column("contacts", sa.Column("city_raw", sa.String(length=60), nullable=True))
    op.create_foreign_key("fk_contacts_city_id", "contacts", "cities", ["city_id"], ["id"])

    # vendor_leads — partner intake inbox (DPDP consent versioning included)
    op.create_table(
        "vendor_leads",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("brand_name", sa.String(length=120), nullable=False),
        sa.Column("contact_name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("instagram", sa.String(length=100), nullable=True),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("cities_raw", sa.Text(), nullable=False),
        sa.Column("price_range", sa.String(length=60), nullable=True),
        sa.Column("monthly_capacity", sa.String(length=60), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("consent_given", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.String(length=40), nullable=True),
        sa.Column("consent_text_hash", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_vendor_leads_status", "vendor_leads", ["status"])
    op.create_index("ix_vendor_leads_email", "vendor_leads", ["email"])


def downgrade() -> None:
    op.drop_index("ix_vendor_leads_email", table_name="vendor_leads")
    op.drop_index("ix_vendor_leads_status", table_name="vendor_leads")
    op.drop_table("vendor_leads")

    op.drop_constraint("fk_contacts_city_id", "contacts", type_="foreignkey")
    op.drop_column("contacts", "city_raw")
    op.drop_column("contacts", "city_id")

    op.add_column("users", sa.Column("city", sa.String(length=50), nullable=True))
    op.execute(
        """
        UPDATE users u
        SET city = c.name
        FROM cities c
        WHERE u.city_id = c.id
        """
    )
    op.drop_constraint("fk_users_city_id", "users", type_="foreignkey")
    op.drop_column("users", "city_raw")
    op.drop_column("users", "city_id")

    op.drop_table("vendor_cities")
    op.drop_index("ix_vendors_active", table_name="vendors")
    op.drop_index("ix_vendors_is_national", table_name="vendors")
    op.drop_index("ix_vendors_category", table_name="vendors")
    op.drop_table("vendors")

    op.drop_index("ix_cities_status_country", table_name="cities")
    op.drop_table("cities")
