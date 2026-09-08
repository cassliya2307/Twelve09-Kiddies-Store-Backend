"""merge delivery_config branch with paystack branch

Revision ID: 20260826_merge_heads
Revises: ('11351', '20260825_add_paystack_fields_to_payment')
Create Date: 2026-08-26 00:00:00.000000

This is a structural merge only - it applies no schema changes.
It unifies the two heads created when the cost_price and paystack
migrations were branched from b5f3496a99ef instead of chaining.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260826_merge_heads"
down_revision = ("11351", "20260825_add_paystack_fields_to_payment")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass


_ = op  # op imported for convention; merge revision performs no operations
