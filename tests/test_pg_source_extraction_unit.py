"""Phase 5E.2 unit tests: read-only MySQL source layer.

Everything here is database-free. Fake cursors/connections stand in for
pymysql, and synthetic ``information_schema`` metadata stands in for the real
catalog, so no MySQL, PostgreSQL, container, or network is required.

Run from the backend directory so the ``tools`` package is importable:

    cd backend
    python -m pytest tests/test_pg_source_extraction_unit.py -q
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tools import pg_import as P


# --------------------------------------------------------------------------- #
# Helpers: synthetic metadata / fake drivers
# --------------------------------------------------------------------------- #
_BACKEND_DIR = Path(__file__).resolve().parents[1]

_DATA_TYPE_FOR = {
    "enum": "enum",
    "decimal": "decimal",
    "boolean": "tinyint",
    "json": "json",
    "datetime": "datetime",
    "integer": "int",
    "text": "varchar",
}

_FORBIDDEN_STATEMENTS = (
    "INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "CREATE", "ALTER",
    "GRANT", "REVOKE", "SETVAL", "NEXTVAL", "REPLACE", "CALL", "MERGE",
)


def _assert_read_only(executed) -> None:
    """Every statement issued must be a SELECT. Checked by statement kind, not by
    substring, because column names like ``updated_at`` contain 'update'."""
    for sql, _ in executed:
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        assert head == "SELECT", f"non-SELECT statement issued: {sql!r}"


def _expected_columns(table: str) -> dict[str, P.SourceColumn]:
    """Build the metadata a fully compatible source would report."""
    spec = P.TABLES[table]
    out: dict[str, P.SourceColumn] = {}
    for col in spec.columns:
        logical = P.expected_source_column_type(table, col)
        if logical == "enum":
            data_type = "enum"
            column_type = "enum(" + ", ".join(
                repr(label) for label in P.ENUM_LABELS[P.ENUM_COLUMNS[(table, col)]]
            ) + ")"
            char_len = None
        elif logical == "decimal":
            data_type, column_type, char_len = "decimal", "decimal(10,2)", None
        elif logical == "json":
            data_type, column_type, char_len = "json", "json", None
        elif logical == "boolean":
            data_type, column_type, char_len = "tinyint", "tinyint(1)", None
        elif logical == "datetime":
            data_type, column_type, char_len = "datetime", "datetime", None
        elif logical == "integer":
            data_type, column_type, char_len = "int", "int", None
        else:
            char_len = P.TEXT_COLUMNS.get((table, col))
            if char_len is None:
                data_type, column_type = "text", "text"
            else:
                data_type = "varchar"
                column_type = f"varchar({char_len})"
        out[col] = P.SourceColumn(
            table=table,
            name=col,
            data_type=data_type,
            is_nullable=col not in spec.not_null,
            column_type=column_type,
            char_max_length=char_len,
            numeric_precision=10 if logical == "decimal" else None,
            numeric_scale=2 if logical == "decimal" else None,
            extra="auto_increment" if col == P.SOURCE_PRIMARY_KEY else "",
        )
    return out


def expected_source_columns() -> dict[str, dict[str, P.SourceColumn]]:
    """A fully compatible source catalog for all nine approved tables."""
    return {table: _expected_columns(table) for table in P.TABLE_ORDER}


class RoutingCursor:
    """Answers a SELECT for the right table, parsed out of the SQL text."""

    def __init__(self, owner: "RoutingConnection", by_table, metadata=None,
                 fail_on=None):
        self._owner = owner
        self._by_table = by_table
        self._metadata = metadata
        self._fail_on = fail_on
        self._rows: list = []
        self.closed = False

    def execute(self, sql: str, params=()) -> None:
        self._owner.executed.append((sql, tuple(params or ())))
        if self._fail_on and self._fail_on in sql.lower():
            raise RuntimeError("driver blew up: driver detail")
        if "information_schema" in sql:
            self._rows = self._metadata if self._metadata is not None else _metadata_rows()
            return
        match = re.search(r"FROM `(?:[a-z0-9_]+`\.`)?([a-z_]+)`", sql)
        table = match.group(1) if match else None
        self._rows = self._by_table.get(table, [])

    def fetchall(self):
        return self._rows

    def close(self):
        self._owner.cursors_closed += 1
        self.closed = True


class RoutingConnection:
    """A fake DB-API connection that never touches a socket.

    ``by_table`` maps a table name to its canned rows (tuples in approved column
    order); any table not present returns no rows.
    """

    def __init__(self, by_table=None, metadata=None, fail_on=None):
        self.executed: list[tuple[str, tuple]] = []
        self.cursors_closed = 0
        self.closed = False
        self._by_table = by_table or {}
        self._metadata = metadata
        self._fail_on = fail_on

    def cursor(self) -> RoutingCursor:
        return RoutingCursor(self, self._by_table, self._metadata, self._fail_on)

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# 2. Schema comparison
# --------------------------------------------------------------------------- #
def test_expected_source_schema_has_no_errors_or_warnings():
    errors, warnings = P.compare_source_schema(expected_source_columns())
    assert errors == []
    assert warnings == []


def test_missing_table_is_an_error():
    meta = expected_source_columns()
    del meta["payments"]
    errors, _ = P.compare_source_schema(meta)
    assert any("payments" in e and "not present" in e for e in errors)


def test_missing_column_is_an_error():
    meta = expected_source_columns()
    del meta["users"]["email"]
    errors, _ = P.compare_source_schema(meta)
    assert "users.email: expected column missing from source" in errors


def test_unexpected_column_is_an_error():
    meta = expected_source_columns()
    meta["users"]["legacy_flag"] = P.SourceColumn(
        table="users", name="legacy_flag", data_type="int", is_nullable=True,
    )
    errors, _ = P.compare_source_schema(meta)
    assert "users.legacy_flag: unexpected column in source" in errors


def test_unexpected_table_is_an_error():
    meta = expected_source_columns()
    meta["staff_permissions"] = {"id": P.SourceColumn(
        table="staff_permissions", name="id", data_type="int", is_nullable=False)}
    errors, _ = P.compare_source_schema(meta)
    assert any("staff_permissions" in e and "unexpected table" in e for e in errors)


def test_alembic_version_is_tolerated():
    meta = expected_source_columns()
    meta["alembic_version"] = {"version_num": P.SourceColumn(
        table="alembic_version", name="version_num", data_type="varchar",
        is_nullable=False, char_max_length=32)}
    errors, warnings = P.compare_source_schema(meta)
    assert errors == []


def test_nullability_mismatch_is_an_error():
    meta = expected_source_columns()
    original = meta["users"]["email"]
    meta["users"]["email"] = P.SourceColumn(
        table="users", name="email", data_type=original.data_type,
        is_nullable=True, column_type=original.column_type,
        char_max_length=original.char_max_length, extra=original.extra)
    errors, _ = P.compare_source_schema(meta)
    assert any("users.email" in e and "NULLABLE" in e for e in errors)


def test_data_type_mismatch_is_an_error():
    meta = expected_source_columns()
    original = meta["orders"]["status"]
    meta["orders"]["status"] = P.SourceColumn(
        table="orders", name="status", data_type="varchar", is_nullable=False,
        column_type="varchar(50)", char_max_length=50)
    errors, _ = P.compare_source_schema(meta)
    assert any("orders.status" in e and "expected a enum" in e for e in errors)


def test_enum_label_set_mismatch_is_an_error():
    meta = expected_source_columns()
    original = meta["orders"]["status"]
    meta["orders"]["status"] = P.SourceColumn(
        table="orders", name="status", data_type="enum", is_nullable=False,
        column_type="enum('PENDING','CANCELLED')")
    errors, _ = P.compare_source_schema(meta)
    assert any("orders.status" in e and "enum label set" in e for e in errors)


def test_decimal_precision_and_scale_mismatch_is_an_error():
    meta = expected_source_columns()
    original = meta["payments"]["amount"]
    meta["payments"]["amount"] = P.SourceColumn(
        table="payments", name="amount", data_type="decimal", is_nullable=False,
        column_type="decimal(10,4)", numeric_precision=10, numeric_scale=4)
    errors, _ = P.compare_source_schema(meta)
    assert any("payments.amount" in e and "expected 10.2" in e for e in errors)


def test_varchar_length_mismatch_is_an_error():
    meta = expected_source_columns()
    original = meta["payments"]["currency"]
    meta["payments"]["currency"] = P.SourceColumn(
        table="payments", name="currency", data_type="varchar", is_nullable=True,
        column_type="varchar(10)", char_max_length=10)
    errors, _ = P.compare_source_schema(meta)
    assert any("payments.currency" in e and "length of 3" in e for e in errors)


def test_bounded_declaration_where_text_expected_is_an_error():
    meta = expected_source_columns()
    original = meta["expenses"]["description"]
    meta["expenses"]["description"] = P.SourceColumn(
        table="expenses", name="description", data_type="text", is_nullable=False,
        column_type="text", char_max_length=255)
    errors, _ = P.compare_source_schema(meta)
    assert any("expenses.description" in e and "unbounded" in e for e in errors)


def test_missing_auto_increment_is_an_error():
    meta = expected_source_columns()
    original = meta["users"]["id"]
    meta["users"]["id"] = P.SourceColumn(
        table="users", name="id", data_type="int", is_nullable=False,
        column_type="int", extra="")
    errors, _ = P.compare_source_schema(meta)
    assert any("users.id" in e and "auto_increment" in e for e in errors)


def test_duplicate_column_metadata_is_a_controlled_error():
    meta = expected_source_columns()
    meta["users"]["name"] = P.SourceColumn(
        table="users", name="name", data_type="varchar", is_nullable=False,
        column_type="varchar(150)", char_max_length=150)
    with pytest.raises(P.Fail) as ei:
        P.fetch_source_columns(_DupCursor(), "twelve09_src_reh1")
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "duplicate" in ei.value.message


class _DupCursor:
    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        base = expected_source_columns()["users"]
        rows = [["users", c.name, c.data_type, "NO" if not c.is_nullable else "YES",
                 c.column_type, c.char_max_length, c.numeric_precision,
                 c.numeric_scale, c.extra] for c in base.values()]
        # add a duplicate of the first row
        rows.append(list(rows[0]))
        return rows

    def close(self):
        pass


def test_unparseable_enum_column_type_is_a_warning_not_an_error():
    meta = expected_source_columns()
    original = meta["orders"]["status"]
    meta["orders"]["status"] = P.SourceColumn(
        table="orders", name="status", data_type="enum", is_nullable=False,
        column_type="enum()")
    errors, warnings = P.compare_source_schema(meta)
    assert errors == []
    assert any("orders.status" in w and "parsable" in w for w in warnings)


def test_short_metadata_row_is_a_controlled_error_not_a_crash():
    with pytest.raises(P.Fail) as ei:
        P.SourceColumn.from_row(("users", "id", "int"))
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "fewer fields" in ei.value.message


def test_none_metadata_row_is_a_controlled_error():
    with pytest.raises(P.Fail):
        P.SourceColumn.from_row(None)


def test_non_mapping_metadata_is_a_controlled_error_not_a_crash():
    errors, warnings = P.compare_source_schema(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert errors and warnings == []


def test_table_present_with_no_columns_is_an_error():
    meta = expected_source_columns()
    meta["expenses"] = {}
    errors, _ = P.compare_source_schema(meta)
    assert any("expenses" in e and "no columns" in e for e in errors)


def test_assert_source_schema_matches_raises_config_without_row_values():
    meta = expected_source_columns()
    del meta["users"]["email"]
    with pytest.raises(P.Fail) as ei:
        P.assert_source_schema_matches(meta)
    assert ei.value.code == P.ExitCode.CONFIG
    assert "twelve09_src_run1" not in ei.value.message  # no database name leak
    assert "root:secret" not in ei.value.message


# --------------------------------------------------------------------------- #
# Derived logical type agrees with map_value()'s dispatch
# --------------------------------------------------------------------------- #
def test_expected_source_column_type_matches_map_value_dispatch_for_every_column():
    """The schema guard and the value mapper must never disagree."""
    for table in P.TABLE_ORDER:
        for col in P.TABLES[table].columns:
            logical = P.expected_source_column_type(table, col)
            if (table, col) in P.ENUM_COLUMNS:
                assert logical == "enum"
            elif col in P.TABLES[table].decimal_cols:
                assert logical == "decimal"
            elif col in P.TABLES[table].bool_cols:
                assert logical == "boolean"
            elif (table, col) in P.JSON_COLUMNS:
                assert logical == "json"
            elif col in ("created_at", "updated_at", "paid_at"):
                assert logical == "datetime"
            elif col in P.TABLES[table].int_cols or (table, col) in P.FK_COLUMNS:
                assert logical == "integer"
            else:
                assert logical == "text"


def test_is_ordinary_text_column_excludes_enum_and_json():
    assert P.is_ordinary_text_column("users", "name") is True
    assert P.is_ordinary_text_column("orders", "delivery_city") is True
    assert P.is_ordinary_text_column("users", "role") is False          # enum
    assert P.is_ordinary_text_column("payments", "payment_metadata") is False
    assert P.is_ordinary_text_column("users", "created_at") is False
    assert P.is_ordinary_text_column("products", "category_id") is False  # FK int


# --------------------------------------------------------------------------- #
# Parameterized schema lookup
# --------------------------------------------------------------------------- #
def test_schema_lookup_sql_is_parameterized_and_scoped():
    sql, params = P.schema_lookup_sql()
    assert "information_schema.COLUMNS" in sql
    assert "TABLE_SCHEMA = %s" in sql
    assert "TABLE_NAME IN" in sql
    assert params == tuple(P.TABLE_ORDER)
    assert len(params) == 9
    # one placeholder for the schema + one per table name, all bound parameters
    assert sql.count("%s") == 10
    # no table name is ever interpolated into the SQL text
    for table in P.TABLE_ORDER:
        assert table not in sql


def test_schema_lookup_sql_rejects_zero_tables():
    with pytest.raises(P.Fail) as ei:
        P.schema_lookup_sql(0)
    assert ei.value.code == P.ExitCode.RUNTIME


def test_source_schema_name_must_match_the_disposable_pattern():
    P.validate_source_schema_name("twelve09_src_reh1")
    for bad in ("twelve09_src_REH1", "twelve09_kiddies_store", "sys", "",
                "a; DROP TABLE users", "twelve09_src_ab"):
        with pytest.raises(P.Fail) as ei:
            P.validate_source_schema_name(bad)
        assert ei.value.code == P.ExitCode.CONFIG


def test_fetch_source_columns_builds_a_mapping():
    conn = RoutingConnection()
    cur = conn.cursor()
    meta = P.fetch_source_columns(cur, "twelve09_src_reh1")
    assert set(meta) == set(P.TABLE_ORDER)
    assert meta["users"]["email"].data_type == "varchar"
    assert meta["users"]["permissions"].data_type == "json"
    assert meta["orders"]["status"].is_nullable is False
    assert meta["payments"]["paid_at"].is_nullable is True
    assert conn.executed[0][1][0] == "twelve09_src_reh1"
    assert conn.executed[0][1][1:] == tuple(P.TABLE_ORDER)
    # fetch_source_columns deliberately closes nothing; the caller owns the
    # cursor (extract_source_rows and inspect_source both close it).


def _metadata_rows():
    """information_schema-shaped positional rows for a compatible source."""
    rows = []
    for table in P.TABLE_ORDER:
        for col in _expected_columns(table).values():
            rows.append([
                table, col.name, col.data_type,
                "YES" if col.is_nullable else "NO", col.column_type,
                col.char_max_length, col.numeric_precision, col.numeric_scale,
                col.extra,
            ])
    return rows


# --------------------------------------------------------------------------- #
# Identifier quoting / safe SQL
# --------------------------------------------------------------------------- #
def test_quote_mysql_ident_backticks_safe_names():
    assert P.quote_mysql_ident("users") == "`users`"
    assert P.quote_mysql_ident("order_items") == "`order_items`"
    assert P.quote_mysql_ident("a1_b2") == "`a1_b2`"


@pytest.mark.parametrize("bad", [
    "users`; DROP TABLE users; --", "users--", "users`", "`users", "USERS",
    "users OR 1=1", "users.", "1users", "users users", "",
])
def test_quote_mysql_ident_refuses_unsafe_identifiers(bad):
    with pytest.raises(P.Fail) as ei:
        P.quote_mysql_ident(bad)
    assert ei.value.code == P.ExitCode.RUNTIME


def test_select_source_sql_is_read_only_ordered_and_qualified():
    sql = P.select_source_sql("orders")
    assert sql.startswith("SELECT ")
    assert "ORDER BY `id`" in sql
    assert "FROM `orders`" in sql
    assert "INSERT" not in sql.upper()
    assert "WHERE" not in sql  # no untrusted predicates
    qualified = P.select_source_sql("orders", schema="twelve09_src_reh1")
    assert qualified.startswith("SELECT ")
    assert "FROM `twelve09_src_reh1`.`orders`" in qualified
    assert "ORDER BY `id`" in qualified


def test_select_source_sql_lists_only_approved_columns():
    col_count = len(P.TABLES["payments"].columns)
    sql = P.select_source_sql("payments")
    assert sql.count(",") == col_count - 1  # N-1 separators in the column list
    for col in P.TABLES["payments"].columns:
        assert f"`{col}`" in sql


def test_select_source_sql_refuses_unknown_table():
    with pytest.raises(P.Fail) as ei:
        P.select_source_sql("mysql")
    assert ei.value.code == P.ExitCode.RUNTIME


def test_select_source_sql_validates_the_schema_name():
    with pytest.raises(P.Fail) as ei:
        P.select_source_sql("orders", schema="twelve09_kiddies_store")
    assert ei.value.code == P.ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# 3. Extraction
# --------------------------------------------------------------------------- #
def _rows_for(table: str, n: int):
    """Synthetic, import-clean rows for one table.

    Text values embed the row index so target-unique columns (users.email,
    categories.name) are naturally distinct; a deliberate duplicate is produced
    by tests that need one.
    """
    cols = P.TABLES[table].columns
    out = []
    for i in range(1, n + 1):
        row = []
        for col in cols:
            if col == "id":
                row.append(i)
            elif col in ("created_at", "updated_at", "paid_at"):
                row.append(None if col == "paid_at" else datetime(2026, 8, 14, 10, 0, 0))
            elif col in P.TABLES[table].decimal_cols:
                row.append(Decimal("10.00"))
            elif col in P.TABLES[table].bool_cols:
                row.append(1)
            elif (table, col) in P.JSON_COLUMNS:
                row.append('["MANAGE_ORDERS"]')
            elif (table, col) in P.ENUM_COLUMNS:
                row.append("PENDING" if table == "orders" else "CUSTOMER")
            elif col in P.TABLES[table].int_cols or (table, col) in P.FK_COLUMNS:
                row.append(1)
            elif (table, col) in P.UNIQUE_COLUMNS:
                row.append(f"{table}-{col}-{i}")
            else:
                row.append(f"x{i}")
        out.append(tuple(row))
    return out


def _rows_as_dicts(table: str, n: int):
    cols = P.TABLES[table].columns
    return [dict(zip(cols, r)) for r in _rows_for(table, n)]


def test_extract_source_rows_returns_all_nine_tables_in_order():
    conn = RoutingConnection()
    extracted = P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert [t for t, _ in extracted] == list(P.TABLE_ORDER)
    assert all(rows == [] for _, rows in extracted)
    assert conn.closed
    assert conn.cursors_closed == len(P.TABLE_ORDER)


def test_extract_source_rows_orders_by_primary_key_and_keeps_order():
    """The SQL demands ordering by PK; the driver's order is preserved verbatim."""
    rows = _rows_for("users", 3)
    reordered = [rows[2], rows[0], rows[1]]
    conn = RoutingConnection({"users": reordered})
    extracted = P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert [r["id"] for r in extracted[0][1]] == [r[0] for r in reordered]
    for sql, _ in conn.executed:
        assert "ORDER BY `id`" in sql
    _assert_read_only(conn.executed)


def test_extract_source_rows_drops_explicit_schema_into_every_query():
    conn = RoutingConnection()
    P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert len(conn.executed) == len(P.TABLE_ORDER)
    for sql, _ in conn.executed:
        assert "`twelve09_src_reh1`." in sql


def test_extract_source_rows_without_schema_is_unqualified():
    conn = RoutingConnection()
    P.extract_source_rows(lambda: conn)
    for sql, _ in conn.executed:
        assert "`twelve09_src_reh1`." not in sql


def test_extract_source_rows_handles_empty_tables():
    conn = RoutingConnection()
    extracted = P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert all(len(rows) == 0 for _, rows in extracted)


def test_extract_source_rows_cleans_up_when_a_query_fails():
    conn = RoutingConnection({"users": _rows_for("users", 3)}, fail_on="`payments`")
    with pytest.raises(P.Fail) as ei:
        P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert ei.value.code == P.ExitCode.RUNTIME
    assert conn.closed
    assert conn.cursors_closed >= 1


def test_extract_source_rows_cleans_up_when_connect_fails():
    def _boom():
        raise OSError("connection refused")

    with pytest.raises(P.Fail) as ei:
        P.extract_source_rows(_boom, schema="twelve09_src_reh1")
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "twelve09_src_reh1" not in ei.value.message


def test_extract_source_rows_sanitizes_driver_errors():
    def _boom():
        raise RuntimeError("Access denied for user 'root'@'127.0.0.1'")

    with pytest.raises(P.Fail) as ei:
        P.extract_source_rows(_boom)
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "root" not in ei.value.message
    assert "Access denied" not in ei.value.message


def test_extract_source_rows_width_mismatch_is_controlled():
    conn = RoutingConnection({"users": [(1, 2)]})
    with pytest.raises(P.Fail) as ei:
        P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert ei.value.code == P.ExitCode.RUNTIME


def test_extract_source_rows_validates_the_schema_name():
    with pytest.raises(P.Fail) as ei:
        P.extract_source_rows(lambda: RoutingConnection(), schema="live_db")
    assert ei.value.code == P.ExitCode.CONFIG


def test_extract_source_rows_issues_no_write_sql():
    conn = RoutingConnection()
    P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    _assert_read_only(conn.executed)
    for sql, _ in conn.executed:
        assert "information_schema" not in sql  # extraction never touches the catalog


def test_extract_source_rows_sanitizes_driver_errors():
    conn = RoutingConnection(fail_on="`users`")
    with pytest.raises(P.Fail) as ei:
        P.extract_source_rows(lambda: conn, schema="twelve09_src_reh1")
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "driver detail" not in ei.value.message
    assert "root:" not in ei.value.message
    assert conn.closed


# --------------------------------------------------------------------------- #
# 4. Charset contract
# --------------------------------------------------------------------------- #
def _cfg(url: str):
    return P.resolve_config(
        {
            "IMPORT_SOURCE_URL": url,
            "IMPORT_TARGET_URL": "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_reh1",
            "SOURCE_IS_DISPOSABLE_MYSQL": "1",
            "CONFIRM_DISPOSABLE_TARGET": "yes",
        },
        "reh1",
    )


_GOOD = "mysql+pymysql://root:secret@127.0.0.1:3306/twelve09_src_reh1?charset=utf8mb4"


def test_correct_charset_is_accepted():
    P.validate_source_charset(_cfg(_GOOD))
    assert P.source_charset(_GOOD) == "utf8mb4"


def test_charset_is_case_insensitive():
    P.validate_source_charset(_cfg(_GOOD.replace("utf8mb4", "UTF8MB4")))


def test_charset_with_other_query_params_is_found():
    url = _GOOD + "&autocommit=true"
    assert P.source_charset(url) == "utf8mb4"
    P.validate_source_charset(_cfg(url))


def test_missing_charset_is_rejected():
    url = "mysql+pymysql://root:secret@127.0.0.1:3306/twelve09_src_reh1"
    assert P.source_charset(url) is None
    with pytest.raises(P.Fail) as ei:
        P.validate_source_charset(_cfg(url))
    assert ei.value.code == P.ExitCode.CONFIG
    assert "root:secret" not in ei.value.message


def test_wrong_charset_is_rejected():
    url = _GOOD.replace("utf8mb4", "latin1")
    with pytest.raises(P.Fail) as ei:
        P.validate_source_charset(_cfg(url))
    assert ei.value.code == P.ExitCode.CONFIG
    assert "latin1" not in ei.value.message  # do not echo the rejected value


def test_empty_charset_is_rejected():
    url = "mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_reh1?charset="
    assert P.source_charset(url) is None
    with pytest.raises(P.Fail):
        P.validate_source_charset(_cfg(url))


@pytest.mark.parametrize("url", [
    "mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_reh1?charset=utf8mb4&charset=utf8",
    "mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_reh1?charset=utf8mb4&charset=latin1",
])
def test_ambiguous_charset_is_rejected(url):
    assert P.source_charset(url) is None
    with pytest.raises(P.Fail) as ei:
        P.validate_source_charset(_cfg(url))
    assert ei.value.code == P.ExitCode.CONFIG


def test_malformed_url_yields_no_charset_and_fails_closed():
    url = "mysql+pymysql://root:x@127.0.0.1:99999/twelve09_src_reh1"
    assert P.source_charset(url) is None


def test_main_refuses_a_charsetless_source(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:secret@127.0.0.1:3306/twelve09_src_reh1")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    assert P.main(["--run-id", "reh1", "--dry-run"]) == P.ExitCode.CONFIG


def test_main_rejects_a_wrong_charset(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL", _GOOD.replace("utf8mb4", "latin1"))
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    assert P.main(["--run-id", "reh1", "--validate-only"]) == P.ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# Text-type audit
# --------------------------------------------------------------------------- #
def test_bytes_in_an_ordinary_text_column_are_rejected():
    row = {"id": 1, "name": b"Ada", "email": "a@example.invalid",
           "password_hash": "x", "role": "ADMIN", "permissions": ["MANAGE_ORDERS"],
           "is_active": True, "created_at": datetime(2026, 8, 14),
           "updated_at": datetime(2026, 8, 14)}
    with pytest.raises(P.Fail) as ei:
        P.assert_source_text_types("users", row)
    assert ei.value.code == P.ExitCode.MAPPING
    assert "users.name" in ei.value.message
    assert "Ada" not in ei.value.message  # no row value in the error


def test_bytes_in_enum_columns_still_map():
    """Existing enum byte-handling must keep working."""
    assert P.map_enum("orders", "status", b"PENDING") == "PENDING"
    P.assert_source_text_types("orders", _order_row(status=b"PENDING"))  # must NOT raise


def test_bytes_in_json_columns_still_map():
    """Existing JSON byte-handling must keep working."""
    assert P.map_json("users", "permissions", b'["MANAGE_ORDERS"]') == ["MANAGE_ORDERS"]
    P.assert_source_text_types("users", _user_row(permissions=b'["MANAGE_ORDERS"]'))


def test_text_types_pass_for_str_and_none():
    P.assert_source_text_types("orders", _order_row(delivery_city=None))
    P.assert_source_text_types("orders", _order_row(delivery_additional_directions=None))


def _user_row(**over):
    row = {
        "id": 3, "name": "Ada", "email": "a@example.invalid", "password_hash": "x",
        "role": "ADMIN", "permissions": ["MANAGE_ORDERS"], "is_active": True,
        "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14),
    }
    row.update(over)
    return row


def _order_row(**over):
    row = {
        "id": 9, "user_id": 1, "address_id": 2, "total_amount": Decimal("120.00"),
        "status": "PENDING", "fulfillment_method": "STORE_DELIVERY",
        "delivery_fee": Decimal("20.00"), "delivery_recipient_name": "Recip",
        "delivery_phone_number": "08030000000", "delivery_address_line": "1 Road",
        "delivery_city": "City", "delivery_state": "State",
        "delivery_additional_directions": None,
        "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14),
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# 5. Read-only source inspection
# --------------------------------------------------------------------------- #
def _inspect_connection(*, by_table=None, metadata=None, fail_on=None):
    """A routing fake answering the schema lookup and the nine SELECTs."""
    table_rows = {"users": _rows_for("users", 2)}
    if by_table:
        table_rows.update(by_table)
    return RoutingConnection(table_rows, metadata=metadata, fail_on=fail_on)


def test_inspect_source_is_read_only_and_reports_counts_only(capsys):
    conn = _inspect_connection()
    code = P.inspect_source(_cfg(_GOOD), lambda: conn)
    assert code == P.ExitCode.OK
    assert conn.closed
    _assert_read_only(conn.executed)
    assert any("information_schema" in sql for sql, _ in conn.executed)  # the schema lookup
    out = capsys.readouterr().err
    assert "[source] users: 2 row(s)" in out
    assert "[source] schema compatible for 9 approved table(s)" in out
    assert "secret" not in out
    assert "a@example.invalid" not in out
    assert "root:" not in out


def test_inspect_source_refuses_a_bad_schema_before_reading():
    metadata = _metadata_rows()
    dropped = [r for r in metadata if not (r[0] == "users" and r[1] == "email")]
    conn = _inspect_connection(metadata=dropped)
    with pytest.raises(P.Fail) as ei:
        P.inspect_source(_cfg(_GOOD), lambda: conn)
    assert ei.value.code == P.ExitCode.CONFIG
    assert conn.closed


def test_inspect_source_rejects_bytes_in_text_without_touching_target():
    rows = [list(r) for r in _rows_for("users", 1)]
    rows[0][P.TABLES["users"].columns.index("name")] = b"Ada"
    conn = _inspect_connection(by_table={"users": [tuple(r) for r in rows]})
    with pytest.raises(P.Fail) as ei:
        P.inspect_source(_cfg(_GOOD), lambda: conn)
    assert ei.value.code == P.ExitCode.MAPPING
    assert conn.closed
    _assert_read_only(conn.executed)


def test_inspect_source_opens_no_target_connection(capsys):
    """inspect_source must never create a PostgreSQL engine or connection."""
    conn = _inspect_connection()
    code = P.inspect_source(_cfg(_GOOD), lambda: conn)
    assert code == P.ExitCode.OK
    for sql, _ in conn.executed:
        assert "psycopg2" not in sql
        assert "postgres" not in sql.lower()
        assert "twelve09_rehearsal" not in sql


def test_inspect_source_runs_compat_checks_and_reports_sanitized(capsys):
    conn = _inspect_connection()
    code = P.inspect_source(_cfg(_GOOD), lambda: conn)
    assert code == P.ExitCode.OK
    out = capsys.readouterr().err
    assert "[check] " in out
    assert "[PASS]" in out
    assert "root:secret" not in out


def test_inspect_source_blocks_on_a_compat_error(capsys):
    rows = [list(r) for r in _rows_for("users", 2)]
    cols = P.TABLES["users"].columns
    rows[1][cols.index("role")] = "SHIPPED"
    conn = _inspect_connection(by_table={"users": [tuple(r) for r in rows]})
    code = P.inspect_source(_cfg(_GOOD), lambda: conn)
    assert code == P.ExitCode.MAPPING
    out = capsys.readouterr().err
    assert "[abort:5]" in out
    assert "No data was written" in out
    assert "SHIPPED" not in out   # the offending value is never printed
    _assert_read_only(conn.executed)


def test_inspect_source_honours_an_approved_manifest(capsys, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"users.role": {"SHIPPED": "STAFF"}}),
                        encoding="utf-8")
    rows = [list(r) for r in _rows_for("users", 2)]
    cols = P.TABLES["users"].columns
    rows[1][cols.index("role")] = "SHIPPED"
    conn = _inspect_connection(by_table={"users": [tuple(r) for r in rows]})
    code = P.inspect_source(_cfg(_GOOD), lambda: conn, env={
        P.ENUM_NORMALIZATION_MANIFEST_ENV: str(manifest)})
    assert code == P.ExitCode.OK


# --------------------------------------------------------------------------- #
# Connection gating
# --------------------------------------------------------------------------- #
def test_open_source_connection_refuses_while_execution_is_disabled():
    with pytest.raises(P.Fail) as ei:
        P.open_source_connection(_GOOD)
    assert ei.value.code == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_open_source_connection_refuses_target_mode_even_if_enabled(monkeypatch):
    monkeypatch.setattr(P, "EXECUTION_ENABLED", True)
    with pytest.raises(P.Fail) as ei:
        P.open_source_connection(_GOOD, is_target=True)
    assert ei.value.code == P.ExitCode.GATE


def test_source_connection_factory_does_not_open_anything():
    factory = P.source_connection_factory(_cfg(_GOOD), schema="twelve09_src_reh1")
    assert callable(factory)


# --------------------------------------------------------------------------- #
# Execution guard (retained from Phase 5D.2, extended)
# --------------------------------------------------------------------------- #
def test_execution_is_disabled():
    assert P.EXECUTION_ENABLED is False


def test_dispatch_path_with_unknown_mode_still_fails_closed():
    with pytest.raises(P.Fail) as ei:
        P._dispatch_db_mode(None, None, None, None)
    assert ei.value.code == P.ExitCode.GATE


def test_dispatch_apply_and_reset_are_not_implemented():
    for mode in ("apply", "reset_sequences"):
        with pytest.raises(P.Fail) as ei:
            P._dispatch_db_mode(
                P.resolve_plan(_ns(mode)), _cfg(_GOOD), None, None)
        assert ei.value.code == P.ExitCode.GATE


def _ns(primary, truncate=False):
    import argparse
    return argparse.Namespace(primary=primary, truncate_target=truncate)


def test_no_environment_variable_can_enable_execution(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL", _GOOD)
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    for sneaky in ("IMPORT_EXECUTION_ENABLED", "IMPORT_EXECUTE", "EXECUTION_ENABLED",
                   "PG_IMPORT_ENABLED", "PG_IMPORT_EXECUTE", "IMPORT_ALLOW_WRITES",
                   "IMPORT_APPLY", "IMPORT_DRY_RUN"):
        monkeypatch.setenv(sneaky, "1")
    for mode in ("--apply", "--dry-run", "--validate-only", "--reset-sequences"):
        assert P.main(["--run-id", "reh1", mode]) == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_main_stops_at_the_gate_for_every_mode(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL", _GOOD)
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setattr(P, "inspect_source",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    monkeypatch.setattr(P, "open_source_connection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    for mode in ("--apply", "--dry-run", "--validate-only", "--reset-sequences"):
        assert P.main(["--run-id", "reh1", mode]) == P.ExitCode.GATE


def test_import_does_not_eagerly_load_drivers_or_open_a_socket():
    """Fresh subprocess: importing the module must load no driver and connect
    to nothing. The socket guard is proved live inside the same script."""
    code = (
        "import socket, sys\n"
        "def _guard(*a, **k):\n"
        "    raise AssertionError('connect attempted')\n"
        "socket.socket.connect = _guard\n"
        "import tools.pg_import as p\n"
        "assert p.EXECUTION_ENABLED is False, p.EXECUTION_ENABLED\n"
        "loaded = sorted(m for m in ('sqlalchemy','pymysql','psycopg2') "
        "if m in sys.modules)\n"
        "print('|'.join(loaded))\n"
        "try:\n"
        "    socket.socket().connect(('127.0.0.1', 1))\n"
        "    print('GUARD_INERT')\n"
        "except AssertionError:\n"
        "    print('GUARD_LIVE')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=str(_BACKEND_DIR),
    )
    assert result.returncode == 0, result.stderr
    # NB: do not strip() -- line 1 is the empty string printed when no driver
    # is loaded, and stripping would hide it.
    lines = result.stdout.splitlines()
    assert lines[0] == "", f"eagerly loaded a database module: {lines[0]!r}"
    assert "GUARD_LIVE" in lines
