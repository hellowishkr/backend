"""merge cities_and_partner_leads with snooze_skip heads

Revision ID: cf0285589097
Revises: d7c94a1f2e58, f4a2b08c5d91
Create Date: 2026-04-21 14:30:00.000000

"""
from typing import Sequence, Union


revision: str = "cf0285589097"
down_revision: Union[str, Sequence[str], None] = ("d7c94a1f2e58", "f4a2b08c5d91")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
