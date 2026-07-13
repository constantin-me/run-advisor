"""add location to activities

Revision ID: 30edaaf0343f
Revises: 99bc5fc6ff54
Create Date: 2026-07-13 17:15:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "30edaaf0343f"
down_revision: Union[str, None] = "99bc5fc6ff54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("activities", sa.Column("latitude", sa.Numeric(), nullable=True))
    op.add_column("activities", sa.Column("longitude", sa.Numeric(), nullable=True))
    op.add_column("activities", sa.Column("location_name", sa.String(), nullable=True))
    op.add_column(
        "activities",
        sa.Column(
            "location_checked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("activities", "location_checked")
    op.drop_column("activities", "location_name")
    op.drop_column("activities", "longitude")
    op.drop_column("activities", "latitude")
