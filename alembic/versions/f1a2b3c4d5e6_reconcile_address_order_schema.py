"""reconcile_address_and_order_schema

Revision ID: f1a2b3c4d5e6
Revises: b5f3496a99ef
Create Date: 2026-08-17 00:00:00.000000

This idempotent migration brings the Alembic migration chain
in line with the current ORM and production MySQL schema.

On a fresh M1+M2 database it applies all missing changes.
On the existing production-shaped database it is a no-op.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'b5f3496a99ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── helper: raw-column-existence check via information_schema ───────────

def _col_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return row is not None and row > 0


def _col_nullable(table: str, column: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row is not None and row[0] == "YES"


def _enum_has(table: str, column: str, value: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    if not row:
        return False
    return f"'{value}'" in row[0]


def _count_rows(table: str, column: str, value: str) -> int:
    bind = op.get_bind()
    return bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :v"),
        {"v": value},
    ).scalar() or 0


def _has_nulls(table: str, column: str) -> bool:
    bind = op.get_bind()
    cnt = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")
    ).scalar()
    return cnt is not None and cnt > 0


# ── UPGRADE ─────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── Phase 1: address column renames ────────────────────────────────

    if _col_exists("addresses", "full_name") and not _col_exists("addresses", "recipient_name"):
        op.alter_column(
            "addresses", "full_name",
            new_column_name="recipient_name",
            existing_type=sa.String(150),
            nullable=False,
        )

    if _col_exists("addresses", "phone") and not _col_exists("addresses", "phone_number"):
        op.alter_column(
            "addresses", "phone",
            new_column_name="phone_number",
            existing_type=sa.String(30),
            nullable=False,
        )

    if _col_exists("addresses", "additional_information") and not _col_exists("addresses", "additional_directions"):
        op.alter_column(
            "addresses", "additional_information",
            new_column_name="additional_directions",
            existing_type=sa.Text(),
            nullable=True,
        )

    # ── Phase 2: add addresses.is_default ──────────────────────────────

    if not _col_exists("addresses", "is_default"):
        op.add_column(
            "addresses",
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )

    # ── Phase 3: make orders.address_id nullable ───────────────────────

    if _col_exists("orders", "address_id") and not _col_nullable("orders", "address_id"):
        op.alter_column(
            "orders", "address_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    # ── Phase 4: replace orders.status enum ────────────────────────────

    if _enum_has("orders", "status", "SHIPPED"):
        op.alter_column(
            "orders", "status",
            existing_type=mysql.ENUM(
                "PENDING", "CONFIRMED", "PROCESSING",
                "SHIPPED", "DELIVERED", "CANCELLED",
            ),
            type_=sa.String(50),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        )

        shipped = _count_rows("orders", "status", "SHIPPED")
        if shipped > 0:
            op.execute("UPDATE orders SET status = 'OUT_FOR_DELIVERY' WHERE status = 'SHIPPED'")

        delivered = _count_rows("orders", "status", "DELIVERED")
        if delivered > 0:
            op.execute("UPDATE orders SET status = 'COMPLETED' WHERE status = 'DELIVERED'")

        op.alter_column(
            "orders", "status",
            existing_type=sa.String(50),
            type_=mysql.ENUM(
                "PENDING", "CONFIRMED", "PROCESSING",
                "READY_FOR_PICKUP", "OUT_FOR_DELIVERY", "COMPLETED", "CANCELLED",
            ),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        )

    # ── Phase 5: add new order columns ─────────────────────────────────

    if not _col_exists("orders", "fulfillment_method"):
        op.add_column(
            "orders",
            sa.Column(
                "fulfillment_method",
                mysql.ENUM("STORE_DELIVERY", "CUSTOMER_DISPATCH", "STORE_PICKUP"),
                nullable=False,
                server_default=sa.text("'STORE_PICKUP'"),
            ),
        )

    if not _col_exists("orders", "delivery_fee"):
        op.add_column(
            "orders",
            sa.Column("delivery_fee", sa.DECIMAL(precision=10, scale=2), nullable=False, server_default=sa.text("0.00")),
        )

    nullable_delivery_cols = [
        ("delivery_recipient_name", sa.String(150)),
        ("delivery_phone_number",   sa.String(30)),
        ("delivery_address_line",   sa.String(255)),
        ("delivery_city",           sa.String(100)),
        ("delivery_state",          sa.String(100)),
        ("delivery_additional_directions", sa.Text()),
    ]
    for col_name, col_type in nullable_delivery_cols:
        if not _col_exists("orders", col_name):
            op.add_column("orders", sa.Column(col_name, col_type, nullable=True))


# ── DOWNGRADE ───────────────────────────────────────────────────────────

def downgrade() -> None:

    # ── Reverse Phase 5: drop order delivery columns ───────────────────

    for col_name in [
        "delivery_additional_directions",
        "delivery_state",
        "delivery_city",
        "delivery_address_line",
        "delivery_phone_number",
        "delivery_recipient_name",
    ]:
        if _col_exists("orders", col_name):
            op.drop_column("orders", col_name)

    if _col_exists("orders", "delivery_fee"):
        op.drop_column("orders", "delivery_fee")

    if _col_exists("orders", "fulfillment_method"):
        op.drop_column("orders", "fulfillment_method")

    # ── Reverse Phase 4: restore legacy status enum ────────────────────

    if _enum_has("orders", "status", "OUT_FOR_DELIVERY"):
        pickup_count = _count_rows("orders", "status", "READY_FOR_PICKUP")
        if pickup_count > 0:
            raise RuntimeError(
                "DOWNGRADE ABORTED: orders contains rows with status "
                f"'READY_FOR_PICKUP' ({pickup_count} row(s)). "
                "Resolve or remap these orders to a compatible status "
                "(PENDING, CONFIRMED, PROCESSING, COMPLETED, or CANCELLED) "
                "before downgrading."
            )

        op.alter_column(
            "orders", "status",
            existing_type=mysql.ENUM(
                "PENDING", "CONFIRMED", "PROCESSING",
                "READY_FOR_PICKUP", "OUT_FOR_DELIVERY", "COMPLETED", "CANCELLED",
            ),
            type_=sa.String(50),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        )

        out_count = _count_rows("orders", "status", "OUT_FOR_DELIVERY")
        if out_count > 0:
            op.execute("UPDATE orders SET status = 'SHIPPED' WHERE status = 'OUT_FOR_DELIVERY'")

        comp_count = _count_rows("orders", "status", "COMPLETED")
        if comp_count > 0:
            op.execute("UPDATE orders SET status = 'DELIVERED' WHERE status = 'COMPLETED'")

        op.alter_column(
            "orders", "status",
            existing_type=sa.String(50),
            type_=mysql.ENUM(
                "PENDING", "CONFIRMED", "PROCESSING",
                "SHIPPED", "DELIVERED", "CANCELLED",
            ),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        )

    # ── Reverse Phase 3: restore orders.address_id NOT NULL ────────────

    if _col_exists("orders", "address_id") and _col_nullable("orders", "address_id"):
        if _has_nulls("orders", "address_id"):
            raise RuntimeError(
                "DOWNGRADE ABORTED: orders.address_id contains NULL values. "
                "Update or delete rows with NULL address_id before downgrading."
            )
        op.alter_column(
            "orders", "address_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    # ── Reverse Phase 2: drop addresses.is_default ─────────────────────

    if _col_exists("addresses", "is_default"):
        op.drop_column("addresses", "is_default")

    # ── Reverse Phase 1: restore old address column names ──────────────

    if _col_exists("addresses", "additional_directions") and not _col_exists("addresses", "additional_information"):
        op.alter_column(
            "addresses", "additional_directions",
            new_column_name="additional_information",
            existing_type=sa.Text(),
            nullable=True,
        )

    if _col_exists("addresses", "phone_number") and not _col_exists("addresses", "phone"):
        op.alter_column(
            "addresses", "phone_number",
            new_column_name="phone",
            existing_type=sa.String(30),
            nullable=False,
        )

    if _col_exists("addresses", "recipient_name") and not _col_exists("addresses", "full_name"):
        op.alter_column(
            "addresses", "recipient_name",
            new_column_name="full_name",
            existing_type=sa.String(150),
            nullable=False,
        )
