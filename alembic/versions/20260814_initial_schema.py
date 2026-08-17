"""initial schema

Revision ID: 20260814_initial_schema
Revises: 
Create Date: 2026-08-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260814_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("CUSTOMER", "STAFF", "ADMIN", name="userrole"),
            nullable=False,
            server_default="CUSTOMER",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)

    op.create_table(
        "staff_permissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_staff_permissions_user_id_users"),
        sa.UniqueConstraint("user_id", "permission", name="uq_staff_permission_user_permission"),
    )
    op.create_index(op.f("ix_staff_permissions_id"), "staff_permissions", ["id"], unique=False)
    op.create_index(op.f("ix_staff_permissions_user_id"), "staff_permissions", ["user_id"], unique=False)

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_index(op.f("ix_categories_id"), "categories", ["id"], unique=False)
    op.create_index(op.f("ix_categories_name"), "categories", ["name"], unique=True)

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column("stock_quantity", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], name="fk_products_category_id_categories"),
        sa.CheckConstraint("stock_quantity >= 0", name="ck_products_stock_quantity_non_negative"),
    )
    op.create_index(op.f("ix_products_id"), "products", ["id"], unique=False)
    op.create_index(op.f("ix_products_name"), "products", ["name"], unique=False)
    op.create_index(op.f("ix_products_category_id"), "products", ["category_id"], unique=False)

    op.create_table(
        "addresses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=150), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=False),
        sa.Column("address_line", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("additional_information", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_addresses_user_id_users"),
    )
    op.create_index(op.f("ix_addresses_id"), "addresses", ["id"], unique=False)
    op.create_index(op.f("ix_addresses_user_id"), "addresses", ["user_id"], unique=False)

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("address_id", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED", "CANCELLED", name="orderstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_orders_user_id_users"),
        sa.ForeignKeyConstraint(["address_id"], ["addresses.id"], name="fk_orders_address_id_addresses"),
    )
    op.create_index(op.f("ix_orders_id"), "orders", ["id"], unique=False)
    op.create_index(op.f("ix_orders_user_id"), "orders", ["user_id"], unique=False)
    op.create_index(op.f("ix_orders_address_id"), "orders", ["address_id"], unique=False)

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column("subtotal", sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_items_order_id_orders"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_order_items_product_id_products"),
    )
    op.create_index(op.f("ix_order_items_id"), "order_items", ["id"], unique=False)
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_index(op.f("ix_order_items_product_id"), "order_items", ["product_id"], unique=False)

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SUCCESS", "FAILED", "REFUNDED", name="paymentstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("payment_method", sa.String(length=100), nullable=False),
        sa.Column("transaction_reference", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_payments_order_id_orders"),
    )
    op.create_index(op.f("ix_payments_id"), "payments", ["id"], unique=False)
    op.create_index(op.f("ix_payments_order_id"), "payments", ["order_id"], unique=False)

    op.create_table(
        "expenses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recorded_by", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount", sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), onupdate=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], name="fk_expenses_recorded_by_users"),
    )
    op.create_index(op.f("ix_expenses_id"), "expenses", ["id"], unique=False)
    op.create_index(op.f("ix_expenses_recorded_by"), "expenses", ["recorded_by"], unique=False)


def downgrade() -> None:
    op.drop_table("expenses")
    op.drop_table("payments")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("addresses")
    op.drop_table("products")
    op.drop_table("categories")
    op.drop_table("staff_permissions")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS userrole")
    op.execute("DROP TYPE IF EXISTS orderstatus")
    op.execute("DROP TYPE IF EXISTS paymentstatus")
