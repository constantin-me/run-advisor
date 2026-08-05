"""add notification_hour to users

Revision ID: b7e4f2a91c08
Revises: f4a1c07b9d21
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e4f2a91c08"
down_revision: Union[str, None] = "f4a1c07b9d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("notification_hour", sa.Integer(), nullable=False, server_default="7"),
    )


def downgrade() -> None:
    op.drop_column("users", "notification_hour")
