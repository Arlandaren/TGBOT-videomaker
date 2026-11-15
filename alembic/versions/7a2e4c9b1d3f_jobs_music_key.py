"""jobs.music_key — звук рилса фоном («Музыка из рилса»)

Revision ID: 7a2e4c9b1d3f
Revises: c4e7a2d9f1b3
Create Date: 2026-11-10 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7a2e4c9b1d3f'
down_revision: Union[str, None] = 'c4e7a2d9f1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('music_key', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'music_key')
