"""add_cost_price_to_products

Revision ID: 20260823_add_cost_price_to_products
Revises: b5f3496a99ef
Create Date: 2026-08-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '20260823_add_cost_price_to_products'
down_revision = 'b5f3496a99ef'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'products',
        sa.Column('cost_price', sa.DECIMAL(10, 2), nullable=True)
    )
    op.create_check_constraint(
        'ck_products_cost_price_non_negative',
        'products',
        'cost_price IS NULL OR cost_price >= 0'
    )


def downgrade() -> None:
    op.drop_constraint('ck_products_cost_price_non_negative', 'products', type_='check')
    op.drop_column('products', 'cost_price')