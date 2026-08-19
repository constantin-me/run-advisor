"""add sport to plan workouts

Revision ID: d5c81a3f7b62
Revises: b7e4f2a91c08
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5c81a3f7b62"
down_revision: Union[str, None] = "b7e4f2a91c08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows are all runs — that was the only sport the coach could
    # create — so backfill them with the server default.
    op.add_column(
        "plan_workouts",
        sa.Column("sport", sa.String(), nullable=False, server_default="running"),
    )


def downgrade() -> None:
    op.drop_column("plan_workouts", "sport")
