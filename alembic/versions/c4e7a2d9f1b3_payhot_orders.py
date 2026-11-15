"""payhot_orders и payments.rub — оплата пакетов через PayHot

Revision ID: c4e7a2d9f1b3
Revises: 9b4e2f7a1c35
Create Date: 2026-11-15 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c4e7a2d9f1b3'
down_revision: Union[str, None] = '9b4e2f7a1c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('payments', sa.Column('rub', sa.Integer(), server_default='0', nullable=False))
    op.create_table(
        'payhot_orders',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('rub', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(length=16), nullable=False),
        sa.Column('currency', sa.String(length=16), nullable=False),
        sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('payment_id', sa.String(length=64), nullable=True, unique=True),
        sa.Column('status', sa.String(length=32), server_default='created', nullable=False),
        sa.Column('credited', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_payhot_orders_user_id', 'payhot_orders', ['user_id'])
    op.create_index('ix_payhot_orders_created_at', 'payhot_orders', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_payhot_orders_created_at', table_name='payhot_orders')
    op.drop_index('ix_payhot_orders_user_id', table_name='payhot_orders')
    op.drop_table('payhot_orders')
    op.drop_column('payments', 'rub')
