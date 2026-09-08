"""add_paystack_fields_to_payment

Revision ID: 20260825_add_paystack_fields_to_payment
Revises: 20260823_add_cost_price_to_products
Create Date: 2026-08-25 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '20260825_add_paystack_fields_to_payment'
down_revision = '20260823_add_cost_price_to_products'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'payments',
        sa.Column('provider', sa.String(50), nullable=True)
    )
    op.add_column(
        'payments',
        sa.Column('provider_reference', sa.String(255), nullable=True)
    )
    op.add_column(
        'payments',
        sa.Column('payment_metadata', sa.JSON(), nullable=True)
    )
    op.add_column(
        'payments',
        sa.Column('paid_at', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'payments',
        sa.Column('currency', sa.String(3), nullable=True)
    )
    op.create_index(
        'ix_payments_provider_reference',
        'payments',
        ['provider_reference'],
        unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_payments_provider_reference', table_name='payments')
    op.drop_column('payments', 'currency')
    op.drop_column('payments', 'paid_at')
    op.drop_column('payments', 'payment_metadata')
    op.drop_column('payments', 'provider_reference')
    op.drop_column('payments', 'provider')