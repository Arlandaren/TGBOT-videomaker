"""jobs.source_parts — сколько ещё видео ждут склейки с исходником

Revision ID: 2d8f6b3a5e71
Revises: 7a2e4c9b1d3f
Create Date: 2026-11-17 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2d8f6b3a5e71'
down_revision: Union[str, None] = '7a2e4c9b1d3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('source_parts', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'source_parts')
