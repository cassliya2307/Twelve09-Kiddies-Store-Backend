"""Add receipt fields to payments table

Revision ID: add_receipt_fields
Revises: pg_baseline
Create Date: 2025-01-15 00:00:00.000000

Add receipt fields to payments table for bank-transfer receipt workflow.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "add_receipt_fields"
down_revision: Union[str, None] = "pg_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add receipt fields to payments table
    op.add_column(
        "payments",
        sa.Column("receipt_data", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_filename", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_mime_type", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_uploaded_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_reviewed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_reviewed_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_rejection_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Remove receipt fields from payments table
    op.drop_column("payments", "receipt_rejection_reason")
    op.drop_column("payments", "receipt_reviewed_by")
    op.drop_column("payments", "receipt_reviewed_at")
    op.drop_column("payments", "receipt_size")
    op.drop_column("payments", "receipt_mime_type")
    op.drop_column("payments", "receipt_filename")
    op.drop_column("payments", "receipt_data")