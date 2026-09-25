"""add nudge state to users

Revision ID: f2b6d81c4a90
Revises: e1a7c94b2d38
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b6d81c4a90"
down_revision: Union[str, None] = "e1a7c94b2d38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_nudge_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("nudges_muted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("nudges_muted_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "nudges_muted_reason")
    op.drop_column("users", "nudges_muted_at")
    op.drop_column("users", "last_nudge_at")
