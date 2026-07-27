"""webhook audit log and disputes

Revision ID: a7f2d91b4c80
Revises: 64cd798145e6
Create Date: 2026-04-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7f2d91b4c80'
down_revision: Union[str, None] = '64cd798145e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add DISPUTED to existing orderstatus enum
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'DISPUTED'")

    # webhook_events — immutable audit log
    op.create_table(
        'webhook_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('provider', sa.Enum('RAZORPAY', 'WHATSAPP', 'HUBBLE', name='webhookprovider'), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('event_id', sa.String(length=100), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('signature', sa.String(length=255), nullable=True),
        sa.Column('signature_valid', sa.Boolean(), nullable=True),
        sa.Column(
            'status',
            sa.Enum('RECEIVED', 'PROCESSED', 'IGNORED', 'FAILED', 'DUPLICATE', name='webhookeventstatus'),
            nullable=False,
        ),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('order_id', sa.String(length=30), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_webhook_events_event_id', 'webhook_events', ['event_id'])
    op.create_index('ix_webhook_events_order_id', 'webhook_events', ['order_id'])
    op.create_index('ix_webhook_events_provider_type', 'webhook_events', ['provider', 'event_type'])
    op.create_index('ix_webhook_events_received_at', 'webhook_events', ['received_at'])

    # disputes
    op.create_table(
        'disputes',
        sa.Column('id', sa.String(length=50), nullable=False),
        sa.Column('order_id', sa.String(length=30), nullable=False),
        sa.Column('razorpay_payment_id', sa.String(length=50), nullable=True),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
        sa.Column('reason_code', sa.String(length=50), nullable=True),
        sa.Column('reason_description', sa.Text(), nullable=True),
        sa.Column(
            'status',
            sa.Enum('OPEN', 'UNDER_REVIEW', 'ACTION_REQUIRED', 'WON', 'LOST', 'CLOSED', name='disputestatus'),
            nullable=False,
        ),
        sa.Column('raised_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('respond_by', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_disputes_order_id', 'disputes', ['order_id'])
    op.create_index('ix_disputes_razorpay_payment_id', 'disputes', ['razorpay_payment_id'])


def downgrade() -> None:
    op.drop_index('ix_disputes_razorpay_payment_id', table_name='disputes')
    op.drop_index('ix_disputes_order_id', table_name='disputes')
    op.drop_table('disputes')
    op.execute('DROP TYPE IF EXISTS disputestatus')

    op.drop_index('ix_webhook_events_received_at', table_name='webhook_events')
    op.drop_index('ix_webhook_events_provider_type', table_name='webhook_events')
    op.drop_index('ix_webhook_events_order_id', table_name='webhook_events')
    op.drop_index('ix_webhook_events_event_id', table_name='webhook_events')
    op.drop_table('webhook_events')
    op.execute('DROP TYPE IF EXISTS webhookeventstatus')
    op.execute('DROP TYPE IF EXISTS webhookprovider')
    # Cannot cleanly remove DISPUTED from orderstatus — leave it (benign)
