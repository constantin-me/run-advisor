"""add garmin profile to users

Revision ID: c6960c0d3882
Revises: cc5a3dae338c
Create Date: 2026-07-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c6960c0d3882"
down_revision: Union[str, None] = "cc5a3dae338c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("garmin_profile", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("profile_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_progress_eval_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_progress_eval_at")
    op.drop_column("users", "profile_synced_at")
    op.drop_column("users", "garmin_profile")
