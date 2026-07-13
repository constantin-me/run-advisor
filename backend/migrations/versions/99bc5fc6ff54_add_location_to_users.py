"""add location to users

Revision ID: 99bc5fc6ff54
Revises: a03e9bf9b7c0
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '99bc5fc6ff54'
down_revision: Union[str, None] = 'a03e9bf9b7c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('latitude', sa.Numeric(), nullable=True))
    op.add_column('users', sa.Column('longitude', sa.Numeric(), nullable=True))
    op.add_column('users', sa.Column('location_name', sa.String(), nullable=True))
    op.add_column('users', sa.Column('timezone', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'timezone')
    op.drop_column('users', 'location_name')
    op.drop_column('users', 'longitude')
    op.drop_column('users', 'latitude')
