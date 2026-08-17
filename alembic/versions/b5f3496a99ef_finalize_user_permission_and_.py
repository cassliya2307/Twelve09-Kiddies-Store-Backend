"""finalize_user_permission_and_fulfillment_design

Revision ID: b5f3496a99ef
Revises: 20260814_initial_schema
Create Date: 2026-08-16 21:44:01.614357
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'b5f3496a99ef'
down_revision: Union[str, None] = '20260814_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS staff_permissions")

    op.add_column(
    'users',
    sa.Column('permissions', sa.JSON(), nullable=True)
    )

    op.execute("""
    UPDATE users
    SET permissions = JSON_ARRAY()
    WHERE permissions IS NULL
    """)

    op.alter_column(
    'users',
    'permissions',
    existing_type=sa.JSON(),
    nullable=False
    )
    

def downgrade() -> None:
    op.drop_column('users', 'permissions')

    op.alter_column(
        'orders',
        'status',
        existing_type=sa.Enum('PENDING', 'CONFIRMED', 'PROCESSING', 'READY_FOR_PICKUP', 'OUT_FOR_DELIVERY', 'COMPLETED', 'CANCELLED', name='orderstatus'),
        type_=mysql.ENUM('PENDING', 'CONFIRMED', 'PROCESSING', 'SHIPPED', 'DELIVERED', 'CANCELLED'),
        existing_nullable=False,
        existing_server_default=sa.text("'PENDING'"),
    )
    op.alter_column('orders', 'address_id', existing_type=mysql.INTEGER(), nullable=False)

    op.drop_column('orders', 'delivery_additional_directions')
    op.drop_column('orders', 'delivery_state')
    op.drop_column('orders', 'delivery_city')
    op.drop_column('orders', 'delivery_address_line')
    op.drop_column('orders', 'delivery_phone_number')
    op.drop_column('orders', 'delivery_recipient_name')
    op.drop_column('orders', 'delivery_fee')
    op.drop_column('orders', 'fulfillment_method')

    op.add_column('addresses', sa.Column('full_name', mysql.VARCHAR(length=150), nullable=False, server_default=''))
    op.add_column('addresses', sa.Column('additional_information', mysql.TEXT(), nullable=True))
    op.add_column('addresses', sa.Column('phone', mysql.VARCHAR(length=30), nullable=False, server_default=''))
    op.drop_column('addresses', 'is_default')
    op.drop_column('addresses', 'additional_directions')
    op.drop_column('addresses', 'phone_number')
    op.drop_column('addresses', 'recipient_name')

    op.create_table(
        'staff_permissions',
        sa.Column('id', mysql.INTEGER(), autoincrement=True, nullable=False),
        sa.Column('user_id', mysql.INTEGER(), autoincrement=False, nullable=False),
        sa.Column('permission', mysql.VARCHAR(length=100), nullable=False),
        sa.Column('created_at', mysql.DATETIME(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_staff_permissions_user_id_users'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_staff_permissions_id', 'staff_permissions', ['id'], unique=False)
    op.create_index('ix_staff_permissions_user_id', 'staff_permissions', ['user_id'], unique=False)
    op.create_index('uq_staff_permission_user_permission', 'staff_permissions', ['user_id', 'permission'], unique=True)
