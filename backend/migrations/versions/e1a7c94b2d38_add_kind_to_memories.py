"""add kind to memories

Revision ID: e1a7c94b2d38
Revises: d5c81a3f7b62
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1a7c94b2d38"
down_revision: Union[str, None] = "d5c81a3f7b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Everything stored so far is a learned fact; standing instructions are the
    # new kind, so the backfill default is correct for existing rows.
    op.add_column(
        "memories",
        sa.Column("kind", sa.String(), nullable=False, server_default="fact"),
    )


def downgrade() -> None:
    op.drop_column("memories", "kind")
