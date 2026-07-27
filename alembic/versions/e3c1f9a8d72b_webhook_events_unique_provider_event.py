"""webhook_events: unique (provider, event_id) for atomic dedup

Revision ID: e3c1f9a8d72b
Revises: d8e3f1a9b274
Create Date: 2026-04-28 16:00:00.000000

Replaces the non-atomic SELECT-then-INSERT dedup in the Razorpay webhook
dispatcher with a DB-level unique constraint, so two concurrent deliveries
of the same provider event id can't both create RECEIVED audit rows and
both run the handler (which previously caused double Hubble vouchers).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c1f9a8d72b"
down_revision: Union[str, None] = "d8e3f1a9b274"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{index_name}"}).scalar())


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass(:table_name) AND conname = :constraint_name
    """), {"table_name": f"public.{table_name}", "constraint_name": constraint_name}).scalar())


def upgrade() -> None:
    if _constraint_exists("webhook_events", "uq_webhook_events_provider_event_id"):
        return

    bind = op.get_bind()
    dup_count = bind.execute(
        sa.text(
            """
            SELECT count(*) FROM webhook_events a
            JOIN webhook_events b
              ON a.provider = b.provider
             AND a.event_id = b.event_id
             AND a.id > b.id
            WHERE a.event_id IS NOT NULL
            """
        )
    ).scalar()
    print(f"[alembic e3c1f9a8d72b] dropping {dup_count} duplicate webhook_events rows")

    op.execute(
        """
        DELETE FROM webhook_events a
        USING webhook_events b
        WHERE a.event_id IS NOT NULL
          AND a.provider = b.provider
          AND a.event_id = b.event_id
          AND a.id > b.id;
        """
    )

    if _index_exists("ix_webhook_events_event_id"):
        op.drop_index("ix_webhook_events_event_id", table_name="webhook_events")
    op.create_unique_constraint(
        "uq_webhook_events_provider_event_id",
        "webhook_events",
        ["provider", "event_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_webhook_events_provider_event_id", "webhook_events", type_="unique")
    op.create_index("ix_webhook_events_event_id", "webhook_events", ["event_id"], unique=False)
