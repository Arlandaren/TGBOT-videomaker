"""jobs.music_key — звук рилса фоном («Музыка из рилса»)

Revision ID: 7a2e4c9b1d3f
Revises: 3f1c8a9d2b6e
Create Date: 2026-11-10 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7a2e4c9b1d3f'
down_revision: Union[str, None] = '3f1c8a9d2b6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('music_key', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'music_key')
