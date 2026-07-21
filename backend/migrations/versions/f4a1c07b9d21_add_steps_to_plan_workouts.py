"""add steps to plan workouts

Revision ID: f4a1c07b9d21
Revises: c6960c0d3882
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a1c07b9d21"
down_revision: Union[str, None] = "c6960c0d3882"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plan_workouts", sa.Column("steps", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_workouts", "steps")
