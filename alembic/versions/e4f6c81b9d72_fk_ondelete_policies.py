"""Apply ON DELETE policies to every FK that lacked one.

Until now most FKs had no ondelete clause — Postgres' default is NO ACTION,
which raises a hard error on any parent delete that has children. This is
fine for everyday operation but blocks the DPDP "delete my account" flow:
removing a user with occasions / contacts / orders crashes mid-transaction.

Revision ID: e4f6c81b9d72
Revises: d31e8a479c2b
Create Date: 2026-04-29 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4f6c81b9d72"
down_revision: Union[str, None] = "d31e8a479c2b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FKS_TO_REWRITE = [
    ("occasions", "user_id", "occasions_user_id_fkey", "users", "CASCADE"),
    ("occasions", "contact_id", "occasions_contact_id_fkey", "contacts", "SET NULL"),
    ("contacts", "user_id", "contacts_user_id_fkey", "users", "CASCADE"),
    ("contacts", "city_id", "fk_contacts_city_id", "cities", "SET NULL"),
    ("users", "city_id", "fk_users_city_id", "cities", "SET NULL"),
    ("orders", "user_id", "orders_user_id_fkey", "users", "CASCADE"),
    ("purchases", "order_id", "purchases_order_id_fkey", "orders", "CASCADE"),
    ("purchases", "occasion_id", "purchases_occasion_id_fkey", "occasions", "SET NULL"),
    ("reminders", "occasion_id", "reminders_occasion_id_fkey", "occasions", "CASCADE"),
    ("celebrant_occasions", "occasion_id", "celebrant_occasions_occasion_id_fkey", "occasions", "CASCADE"),
    ("celebrant_friends", "celebrant_occasion_id", "celebrant_friends_celebrant_occasion_id_fkey", "celebrant_occasions", "CASCADE"),
    ("celebrant_friends", "user_id", "celebrant_friends_user_id_fkey", "users", "SET NULL"),
    ("external_gift_claims", "occasion_id", "external_gift_claims_occasion_id_fkey", "occasions", "CASCADE"),
    ("wishlist_items", "user_id", "wishlist_items_user_id_fkey", "users", "CASCADE"),
    ("disputes", "order_id", "disputes_order_id_fkey", "orders", "RESTRICT"),
]


def _constraint_def(table: str, name: str) -> str | None:
    bind = op.get_bind()
    return bind.execute(sa.text("""
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass(:table_name) AND conname = :constraint_name
    """), {"table_name": f"public.{table}", "constraint_name": name}).scalar()


def upgrade() -> None:
    for table, column, old_name, ref_table, ondelete in _FKS_TO_REWRITE:
        new_name = f"{table}_{column}_fkey"
        current = _constraint_def(table, new_name) or _constraint_def(table, old_name)
        if current and f"ON DELETE {ondelete}" in current:
            continue
        op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{old_name}"')
        if new_name != old_name:
            op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{new_name}"')
        op.create_foreign_key(new_name, table, ref_table, [column], ["id"], ondelete=ondelete)


def downgrade() -> None:
    for table, column, _old_name, ref_table, _ondelete in _FKS_TO_REWRITE:
        new_name = f"{table}_{column}_fkey"
        op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{new_name}"')
        op.create_foreign_key(new_name, table, ref_table, [column], ["id"])
