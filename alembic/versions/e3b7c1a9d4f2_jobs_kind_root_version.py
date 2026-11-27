"""jobs.kind, jobs.root_id, jobs.version — версии ролика из редактора сцен и обложки

Revision ID: e3b7c1a9d4f2
Revises: 2d8f6b3a5e71
Create Date: 2026-11-27 19:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e3b7c1a9d4f2'
down_revision: Union[str, None] = '2d8f6b3a5e71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Значения по умолчанию — на стороне базы: прежние строки становятся
    # обычными роликами первой версии, а код прошлой выкладки, который этих
    # колонок не знает, продолжает вставлять задачи.
    op.add_column('jobs', sa.Column('kind', sa.String(length=16), server_default='video', nullable=False))
    op.add_column('jobs', sa.Column('root_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('jobs', sa.Column('version', sa.Integer(), server_default='1', nullable=False))
    op.create_index('ix_jobs_root_id', 'jobs', ['root_id'])


def downgrade() -> None:
    op.drop_index('ix_jobs_root_id', table_name='jobs')
    op.drop_column('jobs', 'version')
    op.drop_column('jobs', 'root_id')
    op.drop_column('jobs', 'kind')
