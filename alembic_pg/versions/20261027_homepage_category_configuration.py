"""Create persisted homepage category configuration.

Revision ID: 20261027_homepage_category_configuration
Revises: add_receipt_fields
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261027_homepage_category_configuration"
down_revision: Union[str, None] = "add_receipt_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "homepage_category_configurations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("icon_key", sa.String(length=40), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "display_order >= 0 AND display_order <= 4",
            name="ck_homepage_category_configurations_display_order_range",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"],
            name="fk_homepage_category_configurations_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "category_id",
            name="uq_homepage_category_configurations_category_id",
        ),
        sa.UniqueConstraint(
            "display_order",
            name="uq_homepage_category_configurations_display_order",
        ),
    )
    op.create_index(
        "ix_homepage_category_configurations_id",
        "homepage_category_configurations",
        ["id"],
        unique=False,
    )
    op.create_index(
        "ix_homepage_category_configurations_category_id",
        "homepage_category_configurations",
        ["category_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_homepage_category_configurations_category_id",
        table_name="homepage_category_configurations",
    )
    op.drop_index(
        "ix_homepage_category_configurations_id",
        table_name="homepage_category_configurations",
    )
    op.drop_table("homepage_category_configurations")
