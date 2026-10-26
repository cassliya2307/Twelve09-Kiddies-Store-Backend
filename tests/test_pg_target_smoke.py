import json
from decimal import Decimal

import pytest
from sqlalchemy import text


def test_pgnative_json_roundtrip(pgconn):
    conn, text, _ = pgconn
    conn.execute(text("CREATE TEMP TABLE _smoke_json(payload JSON)"))
    conn.execute(text("INSERT INTO _smoke_json VALUES (:p)"), {"p": '{"a": 1, "b": [1,2]}'})
    back = conn.execute(text("SELECT payload FROM _smoke_json")).scalar()
    # Assert the JSON value is correctly round-tripped.
    # psycopg2 may return a dict or a JSON string; normalise to dict.
    if isinstance(back, str):
        back = json.loads(back)
    expected = {"a": 1, "b": [1, 2]}
    assert back == expected, f"JSON round-trip failed: got {back}"
    # Also verify nested array and scalar values are preserved.
    assert back["a"] == 1, "Expected a=1"
    assert back["b"] == [1, 2], "Expected b=[1,2]"


def test_pgnative_enum_labels(pgconn):
    """Verify the userrole enum contains exactly the expected labels."""
    conn, text, _ = pgconn
    result = conn.execute(
        text("""
            SELECT enumlabel
            FROM pg_enum
            WHERE enumtypid = (SELECT oid FROM pg_type WHERE typname = 'userrole')
            ORDER BY enumsortorder
        """)
    ).fetchall()
    labels = [r[0] for r in result]
    assert labels == ["CUSTOMER", "STAFF", "ADMIN"], f"userrole enum labels: {labels}"


def test_pgnative_numeric_precision(pgconn):
    """Verify NUMERIC(10,2) precision is enforced (8 integer digits + 2 fractional)."""
    conn, text, _ = pgconn
    conn.execute(text("CREATE TEMP TABLE _smoke_num(x NUMERIC(10,2))"))
    # Valid: 8 integer digits, 2 fractional
    conn.execute(text("INSERT INTO _smoke_num VALUES (:v)"), {"v": Decimal("99999999.99")})
    # Invalid: 9 integer digits should fail (overflows precision)
    with pytest.raises(Exception):
        conn.execute(text("INSERT INTO _smoke_num VALUES (:v)"), {"v": Decimal("100000000.00")})
    # Invalid: value that rounds to more than 2 fractional digits (e.g., 10.129 -> 10.13 is OK,
    # but 10.999 -> 11.00 would overflow if integer part is at max)
    # Test: integer part at max with fractional that rounds up
    with pytest.raises(Exception):
        conn.execute(text("INSERT INTO _smoke_num VALUES (:v)"), {"v": Decimal("99999999.999")})


def test_pgnative_fk_enforcement(pgconn):
    """Verify foreign key constraints are enforced."""
    conn, text, _ = pgconn
    # Try to insert order with non-existent user_id
    with pytest.raises(Exception):
        conn.execute(
            text("""
                INSERT INTO orders (user_id, address_id, total_amount, status,
                                   fulfillment_method, delivery_fee, created_at, updated_at)
                VALUES (99999, NULL, 10.00, 'PENDING', 'STORE_PICKUP', 0.00, NOW(), NOW())
            """)
        )
    # Try to insert order_item with non-existent order_id
    with pytest.raises(Exception):
        conn.execute(
            text("""
                INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
                VALUES (99999, 1, 1, 10.00, 10.00)
            """)
        )


def test_pgnative_check_enforcement(pgconn):
    """Verify CHECK constraints are enforced."""
    conn, text, _ = pgconn
    # stock_quantity >= 0
    with pytest.raises(Exception):
        conn.execute(
            text("""
                INSERT INTO products (category_id, name, price, stock_quantity, is_active, created_at, updated_at)
                VALUES (1, 'Test', 10.00, -1, true, NOW(), NOW())
            """)
        )
    # cost_price IS NULL OR cost_price >= 0
    with pytest.raises(Exception):
        conn.execute(
            text("""
                INSERT INTO products (category_id, name, price, cost_price, stock_quantity, is_active, created_at, updated_at)
                VALUES (1, 'Test', 10.00, -5.00, 10, true, NOW(), NOW())
            """)
        )


def test_pgnative_serial_reset_empty_and_nonempty(pgconn):
    """Verify SERIAL sequence behavior on empty and non-empty tables."""
    conn, text, _ = pgconn
    # Create a temp table with SERIAL
    conn.execute(text("CREATE TEMP TABLE _smoke_seq (id SERIAL PRIMARY KEY, data TEXT)"))
    
    # Empty table: first insert gets id=1
    conn.execute(text("INSERT INTO _smoke_seq (data) VALUES ('first')"))
    row = conn.execute(text("SELECT id FROM _smoke_seq")).fetchone()
    assert row[0] == 1, f"First insert id should be 1, got {row[0]}"
    
    conn.execute(text("INSERT INTO _smoke_seq (data) VALUES ('second')"))
    row = conn.execute(text("SELECT id FROM _smoke_seq ORDER BY id DESC LIMIT 1")).fetchone()
    assert row[0] == 2, f"Second insert id should be 2, got {row[0]}"
    
    # Test setval behavior (simulating post-import sequence reset)
    # setval(seq, val, is_called=false) means next nextval() returns val
    conn.execute(text("SELECT setval(pg_get_serial_sequence('_smoke_seq', 'id'), 42, false)"))
    conn.execute(text("INSERT INTO _smoke_seq (data) VALUES ('third')"))
    row = conn.execute(text("SELECT id FROM _smoke_seq ORDER BY id DESC LIMIT 1")).fetchone()
    assert row[0] == 42, f"After setval(42, false), next id should be 42, got {row[0]}"
    
    # With is_called=true, next nextval() returns val+1
    conn.execute(text("SELECT setval(pg_get_serial_sequence('_smoke_seq', 'id'), 100, true)"))
    conn.execute(text("INSERT INTO _smoke_seq (data) VALUES ('fourth')"))
    row = conn.execute(text("SELECT id FROM _smoke_seq ORDER BY id DESC LIMIT 1")).fetchone()
    assert row[0] == 101, f"After setval(100, true), next id should be 101, got {row[0]}"