"""MySQL -> PostgreSQL import utility (Phase 5D design; executed only in 5E).

SAFETY / SECURITY CONTRACT (enforced by code, not convention):

* ``EXECUTION_ENABLED`` is ``False`` in this build. With it False, ``main()``
  may run the identity gates but can never create an engine, load a driver, or
  execute SQL: every such call site lives behind ``_dispatch_db_mode()``, which
  is unreachable. There is no CLI flag or environment variable that flips it.

* Two *independent* connections are configured ONLY through environment
  variables:
      IMPORT_SOURCE_URL            -- MySQL restored from the approved backup
      IMPORT_TARGET_URL            -- disposable PostgreSQL rehearsal target
      SOURCE_IS_DISPOSABLE_MYSQL   -- must equal "1"
      CONFIRM_DISPOSABLE_TARGET    -- must equal "yes"
  URLs are NEVER taken from command-line arguments, and are never written to
  files, shell history, logs, or reports. ``DATABASE_URL`` is deliberately NOT
  consulted as any kind of fallback.

* Remote-endpoint overrides (``IMPORT_ALLOW_REMOTE_SOURCE``,
  ``IMPORT_DISPOSABLE_TARGET_HOST``) exist for an explicitly authorised,
  separately reviewed scenario only. They must be ABSENT from the rehearsal
  shell. Neither override, nor any name pattern, proves that an endpoint is
  disposable: endpoint identity must be confirmed out-of-band by the operator
  before execution is ever enabled.

* Timestamp policy is Outcome A: MySQL ``DATETIME`` values are copied
  *unchanged* into PostgreSQL ``TIMESTAMP WITHOUT TIME ZONE``. No timezone is
  inferred and no offset is ever applied. Historical timezone provenance is
  UNVERIFIED; demo timestamps are not audit-grade.

* Personal data (emails, addresses, names, payment metadata, raw rows) is
  NEVER printed. Rejected/invalid data is reported as counts + surrogate row
  IDs (+ business-code enum values) only.

* This file is IMPLEMENTATION ONLY. It has NOT been run against any database
  here. Phase 5E.2 added the READ-ONLY source layer (schema-drift guard,
  parameterized information_schema lookup, ordered extraction, text-type audit
  and the source charset contract). Phase 5E.3 added the READ-ONLY pre-import
  compatibility checks (enum values, string lengths, unique duplicates,
  permissions, decimal precision, required-column nullability, FK orphans,
  datetimes, target CHECK constraints, and report-only business invariants).
  There is still NO import transaction, no reconciliation orchestration, no
  sequence reset, and no target-side check.

All data VALUES are sent to the databases as bound parameters. SQL identifiers
(table/column names) only ever originate from the hard-coded allow-lists below
and are never derived from data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit
from typing import Any, Callable, Iterable, Iterator


# --------------------------------------------------------------------------- #
# Exit codes (fail closed everywhere)
# --------------------------------------------------------------------------- #
class ExitCode:
    OK = 0
    USAGE = 2        # argparse / contradictory flag combination
    CONFIG = 3       # G0/G1 config + scheme problems
    GATE = 4         # G2/G3/G4 identity / state gate refusal
    MAPPING = 5      # row-level type/enum/precision refusal
    RECON = 6        # reconciliation mismatch
    RUNTIME = 7      # missing driver, connection, or unexpected runtime error


class Fail(Exception):
    """A deliberate, fail-closed abort. ``code`` becomes the process exit code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Identity constants
# --------------------------------------------------------------------------- #
# The disposable source MySQL and rehearsal target PostgreSQL databases are
# expected to be freshly-created containers published on loopback. These rules
# make it structurally hard to point the importer at anything else.
#
# Run ids are deliberately restricted to lowercase alphanumerics (3-32 chars) so
# that BOTH derived database names are valid MySQL/PostgreSQL identifiers that
# need no backtick/quoting gymnastics in container bootstrap or CLI commands:
#     source : twelve09_src_<run_id>
#     target : twelve09_rehearsal_<run_id>
# See derived_source_db() / derived_target_db(). resolve_config() re-checks that
# the derived names still satisfy the identity gates below, so the documented
# convention and the enforced convention can never drift apart.
RUN_ID_RE = re.compile(r"^[a-z0-9]{3,32}$")
RUN_ID_FORMAT = "lowercase alphanumeric characters only, 3-32 characters"
SOURCE_DB_PREFIX = "twelve09_src_"
TARGET_DB_PREFIX = "twelve09_rehearsal"
# Derived-name allow-lists (kept in lock-step with RUN_ID_RE on purpose).
SOURCE_DB_RE = re.compile(r"^" + SOURCE_DB_PREFIX + r"[a-z0-9]{3,32}$")
# Target names are additionally length-bounded and charset-restricted relative
# to the earlier free-form variant; the `twelve09_rehearsal` prefix, the
# production-name check and the host allow-list below are all unchanged, so no
# identity safeguard is relaxed by matching the run-id charset.
TARGET_DB_RE = re.compile(r"^" + TARGET_DB_PREFIX + r"(_[a-z0-9]{3,32})?$")


def derived_source_db(run_id: str) -> str:
    """Disposable MySQL source database name derived from a validated run id."""
    return f"{SOURCE_DB_PREFIX}{run_id}"


def derived_target_db(run_id: str) -> str:
    """Disposable PostgreSQL target database name derived from a validated run id."""
    return f"{TARGET_DB_PREFIX}_{run_id}"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Databases that are known NOT to be disposable sources.
KNOWN_LOCAL_SOURCE_DBS = frozenset({"twelve09_kiddies_store"})

# Any host/db name that resembles a production endpoint is refused outright.
_PRODUCTION_HOST_MARKERS = (
    "supabase.co", "supabase.com", "supabase", "pooler.supabase",
    "amazonaws.com", "rds.amazonaws", ".azure.", "database.azure",
    "azurewebsites", "neon.tech", "cockroachlabs", "render.com",
)
_PRODUCTION_DB_MARKERS = (
    "prod", "production", "master", "main", "live", "primary",
    "twelve09_kiddies_store",
)

SUPPORTED_SOURCE_SCHEMES = frozenset({"mysql+pymysql"})
SUPPORTED_TARGET_SCHEMES = frozenset({"postgresql+psycopg2", "postgresql"})


# --------------------------------------------------------------------------- #
# Table schema (mirrors alembic_pg/versions/pg_baseline_initial_schema.py)
# --------------------------------------------------------------------------- #
# Enum label allow-lists (must match the PostgreSQL native enum types).
ENUM_LABELS: dict[str, tuple[str, ...]] = {
    "userrole": ("CUSTOMER", "STAFF", "ADMIN"),
    "orderstatus": (
        "PENDING", "CONFIRMED", "PROCESSING", "READY_FOR_PICKUP",
        "OUT_FOR_DELIVERY", "COMPLETED", "CANCELLED",
    ),
    "fulfillmentmethod": ("STORE_DELIVERY", "CUSTOMER_DISPATCH", "STORE_PICKUP"),
    "paymentstatus": ("PENDING", "SUCCESS", "FAILED", "REFUNDED"),
}

_ALL_ENUM_LABELS = set().union(*[set(v) for v in ENUM_LABELS.values()])

# Columns that carry an enum, keyed (table, column) -> enum type name.
ENUM_COLUMNS: dict[tuple[str, str], str] = {
    ("users", "role"): "userrole",
    ("orders", "status"): "orderstatus",
    ("orders", "fulfillment_method"): "fulfillmentmethod",
    ("payments", "status"): "paymentstatus",
}

# JSON columns keyed (table, column) -> "list" | "object" expected shape.
JSON_COLUMNS: dict[tuple[str, str], str] = {
    ("users", "permissions"): "list",
    ("payments", "payment_metadata"): "object",
}

# Parent targets for foreign keys, keyed (table, column) -> parent table.
FK_COLUMNS: dict[tuple[str, str], str] = {
    ("products", "category_id"): "categories",
    ("addresses", "user_id"): "users",
    ("orders", "user_id"): "users",
    ("orders", "address_id"): "addresses",
    ("order_items", "order_id"): "orders",
    ("order_items", "product_id"): "products",
    ("payments", "order_id"): "orders",
    ("expenses", "recorded_by"): "users",
}

# Numeric(10,2) columns carry the *_REPR sentinels below so that we can tell
# apart "not present" from an actual empty string in a row dict.
_DEC = "decimal"
_BOOL = "boolean"
_JSON = "json"
_DT = "datetime"
_NUM = "int/int-or-fk"
_STR = "text"


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[str, ...]
    not_null: frozenset[str]
    decimal_cols: frozenset[str] = frozenset()
    bool_cols: frozenset[str] = frozenset()
    varchar_cols: frozenset[str] = frozenset()
    int_cols: frozenset[str] = frozenset()  # non-FK integers
    max_decimal: Decimal = Decimal("99999999.99")


def _spec(
    columns: Iterable[str],
    not_null: Iterable[str],
    *,
    decimal_cols: Iterable[str] = (),
    bool_cols: Iterable[str] = (),
    varchar_cols: Iterable[str] = (),
    int_cols: Iterable[str] = (),
) -> TableSpec:
    return TableSpec(
        columns=tuple(columns),
        not_null=frozenset(not_null),
        decimal_cols=frozenset(decimal_cols),
        bool_cols=frozenset(bool_cols),
        varchar_cols=frozenset(varchar_cols),
        int_cols=frozenset(int_cols),
    )


# Column-by-column definitions; FK detection is via FK_COLUMNS, everything else
# falls through to (int) or (text) so a missing spec does not poison an insert.
TABLES: dict[str, TableSpec] = {
    "users": _spec(
        ["id", "name", "email", "password_hash", "role", "permissions",
         "is_active", "created_at", "updated_at"],
        ["id", "name", "email", "password_hash", "role", "permissions",
         "is_active", "created_at", "updated_at"],
        bool_cols=["is_active"], varchar_cols=["name", "email", "password_hash"],
        int_cols=["id"],
    ),
    "categories": _spec(
        ["id", "name", "description", "is_active", "created_at", "updated_at"],
        ["id", "name", "is_active", "created_at", "updated_at"],
        bool_cols=["is_active"], varchar_cols=["name", "description"],
        int_cols=["id"],
    ),
    "delivery_config": _spec(
        ["id", "fee_amount", "is_active", "updated_at"],
        ["id", "fee_amount", "is_active", "updated_at"],
        decimal_cols=["fee_amount"], bool_cols=["is_active"], int_cols=["id"],
    ),
    "products": _spec(
        ["id", "category_id", "name", "description", "price", "cost_price",
         "stock_quantity", "image_url", "is_active", "created_at", "updated_at"],
        ["id", "category_id", "name", "price", "stock_quantity", "is_active",
         "created_at", "updated_at"],
        decimal_cols=["price", "cost_price"], bool_cols=["is_active"],
        varchar_cols=["name", "description", "image_url"],
        int_cols=["id", "stock_quantity"],  # category_id is the FK (falls through)
    ),
    "addresses": _spec(
        ["id", "user_id", "recipient_name", "phone_number", "address_line",
         "city", "state", "additional_directions", "is_default", "created_at",
         "updated_at"],
        ["id", "user_id", "recipient_name", "phone_number", "address_line",
         "city", "state", "is_default", "created_at", "updated_at"],
        bool_cols=["is_default"],
        varchar_cols=["recipient_name", "phone_number", "address_line", "city",
                     "state", "additional_directions"],
        int_cols=["id"],
    ),
    "orders": _spec(
        ["id", "user_id", "address_id", "total_amount", "status",
         "fulfillment_method", "delivery_fee", "delivery_recipient_name",
         "delivery_phone_number", "delivery_address_line", "delivery_city",
         "delivery_state", "delivery_additional_directions", "created_at",
         "updated_at"],
        ["id", "user_id", "total_amount", "status", "fulfillment_method",
         "delivery_fee", "created_at", "updated_at"],
        decimal_cols=["total_amount", "delivery_fee"],
        varchar_cols=["delivery_recipient_name", "delivery_phone_number",
                     "delivery_address_line", "delivery_city", "delivery_state",
                     "delivery_additional_directions"],
        int_cols=["id"],
    ),
    "order_items": _spec(
        ["id", "order_id", "product_id", "quantity", "unit_price", "subtotal"],
        ["id", "order_id", "product_id", "quantity", "unit_price", "subtotal"],
        decimal_cols=["unit_price", "subtotal"], int_cols=["id", "quantity"],
    ),
    "payments": _spec(
        ["id", "order_id", "amount", "status", "payment_method", "provider",
         "provider_reference", "transaction_reference", "payment_metadata",
         "currency", "paid_at", "created_at", "updated_at"],
        ["id", "order_id", "amount", "status", "payment_method", "created_at",
         "updated_at"],
        decimal_cols=["amount"],
        varchar_cols=["payment_method", "provider", "provider_reference",
                     "transaction_reference", "currency"],
        int_cols=["id"],
    ),
    "expenses": _spec(
        ["id", "recorded_by", "description", "amount", "category",
         "created_at", "updated_at"],
        ["id", "recorded_by", "description", "amount", "category",
         "created_at", "updated_at"],
        decimal_cols=["amount"],
        varchar_cols=["description", "category"], int_cols=["id"],
    ),
}

# Mandatory import order (parents before children).
TABLE_ORDER: tuple[str, ...] = (
    "users", "categories", "delivery_config", "products", "addresses",
    "orders", "order_items", "payments", "expenses",
)


# --------------------------------------------------------------------------- #
# Source schema allow-list (MySQL information_schema, read-only)
# --------------------------------------------------------------------------- #
# The nine application tables. ``alembic_version`` is deliberately absent: it is
# Alembic bookkeeping for the MySQL chain, never an application table to import.
SOURCE_TABLES: frozenset[str] = frozenset(TABLE_ORDER)

# Tolerated extras in a restored source. ``alembic_version`` is the only entry:
# any OTHER unexpected table means the restore is not the approved backup and
# the run must stop rather than guess.
IGNORED_SOURCE_TABLES: frozenset[str] = frozenset({"alembic_version"})

# Single-column primary key for every application table (see app/models/* and
# alembic_pg/versions/pg_baseline_initial_schema.py).
SOURCE_PRIMARY_KEY = "id"

# Expected NUMERIC precision/scale. Mirrors TableSpec.max_decimal (8 integer
# digits + 2 fractional) and pg_baseline's DECIMAL(precision=10, scale=2).
EXPECTED_NUMERIC_PRECISION = 10
EXPECTED_NUMERIC_SCALE = 2

# The source URL MUST declare a charset. pyMySQL falls back to the server's
# default when none is given, which is how text columns come back as ``bytes``.
REQUIRED_SOURCE_CHARSET = "utf8mb4"

# Expected CHARACTER_MAXIMUM_LENGTH per (table, column) for text columns.
# ``None`` means an unbounded TEXT column (no length limit in either engine).
# Values are transcribed from the String(n) declarations in app/models/* and
# from pg_baseline_initial_schema.py -- NOT guessed.
TEXT_COLUMNS: dict[tuple[str, str], int | None] = {
    ("users", "name"): 150,
    ("users", "email"): 255,
    ("users", "password_hash"): 255,
    ("categories", "name"): 150,
    ("categories", "description"): None,
    ("products", "name"): 150,
    ("products", "description"): None,
    ("products", "image_url"): 500,
    ("addresses", "recipient_name"): 150,
    ("addresses", "phone_number"): 30,
    ("addresses", "address_line"): 255,
    ("addresses", "city"): 100,
    ("addresses", "state"): 100,
    ("addresses", "additional_directions"): None,
    ("orders", "delivery_recipient_name"): 150,
    ("orders", "delivery_phone_number"): 30,
    ("orders", "delivery_address_line"): 255,
    ("orders", "delivery_city"): 100,
    ("orders", "delivery_state"): 100,
    ("orders", "delivery_additional_directions"): None,
    ("payments", "payment_method"): 100,
    ("payments", "provider"): 50,
    ("payments", "provider_reference"): 255,
    ("payments", "transaction_reference"): 255,
    ("payments", "currency"): 3,
    ("expenses", "description"): None,
    ("expenses", "category"): 100,
}

# Accepted MySQL ``DATA_TYPE`` tokens per logical type. Kept deliberately broad
# so a semantically equivalent MySQL type is not reported as a false mismatch;
# the meaningful drift cases (enum vs varchar, wrong scale, ...) are caught by
# the per-type checks in compare_source_schema().
_MYSQL_DATA_TYPE_EXPECTATIONS: dict[str, frozenset[str]] = {
    "enum": frozenset({"enum"}),
    "decimal": frozenset({"decimal", "numeric"}),
    "boolean": frozenset({"tinyint"}),
    "json": frozenset({"json"}),
    "datetime": frozenset({"datetime", "timestamp"}),
    # TINYINT is also accepted for integers: MySQL has no real BOOLEAN type and
    # a 0/1 flag declared INTEGER is indistinguishable from one declared BOOL.
    "integer": frozenset({"int", "bigint", "smallint", "mediumint", "tinyint"}),
    "text": frozenset({"varchar", "text", "char", "tinytext", "mediumtext", "longtext"}),
}

_ENUM_LABEL_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


@dataclass(frozen=True)
class SourceColumn:
    """One row of ``information_schema.COLUMNS`` for a source table."""

    table: str
    name: str
    data_type: str
    is_nullable: bool
    column_type: str = ""
    char_max_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    extra: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "SourceColumn":
        """Build from a positional information_schema row, tolerating None."""
        values = list(row) if row is not None else []
        if len(values) < 5:
            raise Fail(
                ExitCode.RUNTIME,
                "source schema metadata row has fewer fields than expected; "
                "refusing to guess column compatibility.",
            )
        return cls(
            table=str(values[0] or ""),
            name=str(values[1] or ""),
            data_type=str(values[2] or "").lower(),
            is_nullable=str(values[3] or "").upper() != "NO",
            column_type=str(values[4] or ""),
            char_max_length=_opt_int(values[5]) if len(values) > 5 else None,
            numeric_precision=_opt_int(values[6]) if len(values) > 6 else None,
            numeric_scale=_opt_int(values[7]) if len(values) > 7 else None,
            extra=str(values[8] or "") if len(values) > 8 else "",
        )


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def expected_source_column_type(table: str, col: str) -> str:
    """Logical type token for a source column.

    Derived from the SAME spec fields map_value() dispatches on, so this can
    never disagree with the mapping path without a code change to both.
    """
    spec = TABLES[table]
    if (table, col) in ENUM_COLUMNS:
        return "enum"
    if col in spec.decimal_cols:
        return "decimal"
    if col in spec.bool_cols:
        return "boolean"
    if (table, col) in JSON_COLUMNS:
        return "json"
    if col in ("created_at", "updated_at", "paid_at"):
        return "datetime"
    if col in spec.int_cols or (table, col) in FK_COLUMNS:
        return "integer"
    return "text"


def is_ordinary_text_column(table: str, col: str) -> bool:
    """True when a source value for this column must be ``str`` or ``None``.

    Enum and JSON columns are exempt: map_enum()/map_json() deliberately accept
    raw ``bytes`` from the driver.
    """
    if (table, col) in ENUM_COLUMNS or (table, col) in JSON_COLUMNS:
        return False
    spec = TABLES[table]
    if col in spec.decimal_cols or col in spec.bool_cols:
        return False
    if col in ("created_at", "updated_at", "paid_at"):
        return False
    if col in spec.int_cols or (table, col) in FK_COLUMNS:
        return False
    return True


def _enum_labels_of(column_type: str) -> tuple[frozenset[str], bool]:
    """(labels, parseable) for a MySQL ``enum(...)`` COLUMN_TYPE."""
    match = re.match(r"^\s*enum\s*\((.*)\)\s*$", column_type, re.IGNORECASE | re.DOTALL)
    if not match:
        return frozenset(), False
    labels = frozenset(m.group(1) for m in _ENUM_LABEL_RE.finditer(match.group(1)))
    return labels, bool(labels)


def source_charset(url: str) -> str | None:
    """Extract the ``charset`` query parameter, or None if absent/ambiguous.

    Uses the same url-splitting posture as parse_url(); a URL whose query
    cannot be parsed yields None so the caller fails closed rather than
    silently accepting an unparsed charset.
    """
    try:
        query = urlsplit(url).query
    except ValueError:
        return None
    if not query:
        return None
    try:
        pairs = parse_qs(query, keep_blank_values=True)
    except ValueError:
        return None
    values = pairs.get("charset") or []
    if len(values) != 1:
        return None
    token = (values[0] or "").strip()
    return token or None


def validate_source_charset(cfg: Config) -> None:
    """G1b - enforce the source charset contract. Pure; performs no connection.

    A configured charset is still not proof that every value comes back as
    ``str``; assert_source_text_types() re-checks at extraction time.
    """
    got = source_charset(cfg.source_url)
    if got is None:
        raise Fail(
            ExitCode.CONFIG,
            f"IMPORT_SOURCE_URL must specify charset={REQUIRED_SOURCE_CHARSET} "
            "exactly once; refusing to read the source without an explicit charset.",
        )
    if got.lower() != REQUIRED_SOURCE_CHARSET:
        raise Fail(
            ExitCode.CONFIG,
            f"IMPORT_SOURCE_URL charset must be '{REQUIRED_SOURCE_CHARSET}'; "
            "the only charset this importer is certified against; refusing.",
        )


def compare_source_schema(
    source_columns: dict[str, dict[str, SourceColumn]],
) -> tuple[list[str], list[str]]:
    """Compare discovered source metadata against TABLES. Pure; no database.

    Returns ``(errors, warnings)``. ``errors`` are fail-closed mismatches;
    ``warnings`` are properties the current specification cannot verify. Never
    raises for malformed metadata: that is reported as an error or a warning.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(source_columns, dict):
        return (["source schema metadata is not a mapping"], [])

    for table in TABLE_ORDER:
        discovered = source_columns.get(table)
        if discovered is None:
            errors.append(f"{table}: table not present in the disposable source")
            continue
        if not discovered:
            errors.append(f"{table}: present but reports no columns")
            continue

        spec = TABLES[table]
        expected_names = set(spec.columns)
        actual_names = set(discovered)
        for col in sorted(expected_names - actual_names):
            errors.append(f"{table}.{col}: expected column missing from source")
        for col in sorted(actual_names - expected_names):
            errors.append(f"{table}.{col}: unexpected column in source")

        pk = SOURCE_PRIMARY_KEY
        if pk in discovered and "auto_increment" not in discovered[pk].extra.lower():
            errors.append(f"{table}.{pk}: expected auto_increment, not declared")

        for col in spec.columns:
            meta = discovered.get(col)
            if meta is None:
                continue  # already reported as missing above
            if not meta.name:
                errors.append(f"{table}.{col}: source metadata has no column name")
                continue
            if not meta.data_type:
                errors.append(f"{table}.{col}: source metadata has no DATA_TYPE")
                continue

            expected_nullable = col not in spec.not_null
            if meta.is_nullable != expected_nullable:
                errors.append(
                    f"{table}.{col}: expected NULLABLE="
                    f"{'YES' if expected_nullable else 'NO'}, source reports "
                    f"{'YES' if meta.is_nullable else 'NO'}"
                )

            logical = expected_source_column_type(table, col)
            allowed = _MYSQL_DATA_TYPE_EXPECTATIONS[logical]
            if meta.data_type not in allowed:
                errors.append(
                    f"{table}.{col}: expected a {logical} column, source reports "
                    f"{meta.data_type}"
                )
                continue

            if logical == "enum":
                labels, parseable = _enum_labels_of(meta.column_type)
                if not parseable:
                    warnings.append(f"{table}.{col}: COLUMN_TYPE is not a parsable enum")
                elif labels != set(ENUM_LABELS[ENUM_COLUMNS[(table, col)]]):
                    errors.append(f"{table}.{col}: enum label set does not match the target")
            elif logical == "decimal":
                if (meta.numeric_precision, meta.numeric_scale) != (
                    EXPECTED_NUMERIC_PRECISION, EXPECTED_NUMERIC_SCALE,
                ):
                    errors.append(
                        f"{table}.{col}: expected "
                        f"{EXPECTED_NUMERIC_PRECISION}.{EXPECTED_NUMERIC_SCALE}, "
                        f"source reports {meta.numeric_precision}."
                        f"{meta.numeric_scale}"
                    )
            elif logical == "text":
                expected_len = TEXT_COLUMNS.get((table, col))
                if expected_len is None:
                    # MySQL's TEXT type reports CHARACTER_MAXIMUM_LENGTH = 65535,
                    # which is the maximum for TEXT and effectively unbounded.
                    # Accept this as equivalent to PostgreSQL's unbounded TEXT.
                    if meta.char_max_length is not None and meta.char_max_length != 65535:
                        errors.append(
                            f"{table}.{col}: expected an unbounded TEXT column, "
                            f"source reports a length of {meta.char_max_length}"
                        )
                elif meta.char_max_length != expected_len:
                    errors.append(
                        f"{table}.{col}: expected a length of {expected_len}, "
                        f"source reports {meta.char_max_length}"
                    )

    for table in sorted(source_columns):
        if table not in SOURCE_TABLES and table not in IGNORED_SOURCE_TABLES:
            errors.append(f"{table}: unexpected table in the disposable source")

    return errors, warnings


def assert_source_schema_matches(
    source_columns: dict[str, dict[str, SourceColumn]],
) -> tuple[list[str], list[str]]:
    """Raise ``Fail(CONFIG)`` on any schema error; return ``(errors, warnings)``.

    Messages identify table and column only -- never row values, customer data,
    credentials, or connection URLs.
    """
    errors, warnings = compare_source_schema(source_columns)
    if errors:
        raise Fail(
            ExitCode.CONFIG,
            "source schema is incompatible: " + "; ".join(errors),
        )
    return errors, warnings


def schema_lookup_sql(table_count: int | None = None) -> tuple[str, tuple[str, ...]]:
    """Parameterised information_schema lookup for the approved tables.

    The placeholder count is derived from ``TABLE_ORDER``, never from data.
    """
    count = len(TABLE_ORDER) if table_count is None else table_count
    if count <= 0:
        raise Fail(ExitCode.RUNTIME, "schema lookup requires at least one table name")
    placeholders = ", ".join(["%s"] * count)
    return (
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_TYPE, "
        "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, EXTRA "
        "FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders}) "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
        tuple(TABLE_ORDER),
    )


def fetch_source_columns(
    cursor: Any, schema: str,
) -> dict[str, dict[str, SourceColumn]]:
    """Read source column metadata through a DB-API cursor. Closes nothing."""
    validate_source_schema_name(schema)
    sql, params = schema_lookup_sql()
    cursor.execute(sql, (schema, *params))
    fetched = cursor.fetchall()
    out: dict[str, dict[str, SourceColumn]] = {}
    for raw in fetched:
        meta = SourceColumn.from_row(raw)
        if not meta.table:
            raise Fail(ExitCode.RUNTIME, "source schema metadata row has no TABLE_NAME")
        if meta.name in out.setdefault(meta.table, {}):
            raise Fail(
                ExitCode.RUNTIME,
                f"{meta.table}.{meta.name}: duplicate source schema metadata row",
            )
        out[meta.table][meta.name] = meta
    for table in source_schema_tables_missing(out):
        out[table] = {}
    return out


def source_schema_tables_missing(
    source_columns: dict[str, dict[str, SourceColumn]],
) -> list[str]:
    """Approved tables absent from a discovered-schema mapping."""
    return [t for t in TABLE_ORDER if t not in source_columns]


def validate_source_schema_name(schema: str) -> str:
    """Allow-list a schema/database name before it can reach any SQL text."""
    if not SOURCE_DB_RE.match(schema or ""):
        raise Fail(
            ExitCode.CONFIG,
            "source database name does not match the disposable pattern "
            f"'{SOURCE_DB_RE.pattern}'; refusing to query information_schema.",
        )
    return schema


def quote_mysql_ident(name: str) -> str:
    """Quote a MySQL identifier. Same allow-list as quote_ident() (PostgreSQL)."""
    if not re.match(r"^[a-z_][a-z0-9_]*$", name):
        raise Fail(ExitCode.RUNTIME, f"refusing unsafe MySQL identifier {name!r}")
    return "`" + name + "`"


def select_source_sql(table: str, schema: str | None = None) -> str:
    """Read-only SELECT for one approved table, ordered by primary key.

    Column and table identifiers come only from TABLE_ORDER / TableSpec.columns;
    values are always bound parameters by the driver.
    """
    if table not in TABLES:
        raise Fail(ExitCode.RUNTIME, f"refusing to read unknown source table {table!r}")
    cols = [quote_mysql_ident(c) for c in TABLES[table].columns]
    if schema is None:
        source = quote_mysql_ident(table)
    else:
        validate_source_schema_name(schema)
        source = f"{quote_mysql_ident(schema)}.{quote_mysql_ident(table)}"
    order = quote_mysql_ident(SOURCE_PRIMARY_KEY)
    return f"SELECT {', '.join(cols)} FROM {source} ORDER BY {order}"


def _close_quietly(resource: Any, what: str) -> None:
    """Close a cursor/connection, surfacing failures as sanitized RUNTIME errors."""
    try:
        resource.close()
    except Fail:
        raise
    except Exception:
        raise Fail(ExitCode.RUNTIME, f"could not close the {what} cleanly.")


def extract_source_rows(
    connect: Callable[[], Any], *, schema: str | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Read every approved table, in TABLE_ORDER, ordered by primary key.

    ``connect`` is a zero-argument callable returning a DB-API connection. It is
    the injection seam: production passes a factory over make_engine(); the unit
    tests pass a fake. The connection and every cursor are closed on success AND
    on failure. No target, DDL, or sequence operation happens here.

    Returns ``[(table, rows), ...]`` with rows as dicts keyed by the approved
    column names, so the column set is fixed by the spec rather than by whatever
    the cursor happens to yield.
    """
    if schema is not None:
        validate_source_schema_name(schema)
    extracted: list[tuple[str, list[dict[str, Any]]]] = []
    try:
        connection = connect()
    except Fail:
        raise
    except Exception:
        raise Fail(ExitCode.RUNTIME, "could not open the disposable MySQL source connection.")
    try:
        for table in TABLE_ORDER:
            cursor = connection.cursor()
            try:
                sql = select_source_sql(table, schema=schema)
                try:
                    cursor.execute(sql)
                    fetched = cursor.fetchall()
                except Fail:
                    raise
                except Exception:
                    raise Fail(
                        ExitCode.RUNTIME,
                        f"{table}: could not read rows from the disposable source.",
                    )
                columns = TABLES[table].columns
                rows: list[dict[str, Any]] = []
                for raw in fetched:
                    if len(raw) != len(columns):
                        raise Fail(
                            ExitCode.RUNTIME,
                            f"{table}: cursor returned a different number of "
                            "values than the approved column list.",
                        )
                    rows.append(dict(zip(columns, raw)))
            finally:
                _close_quietly(cursor, f"{table} cursor")
            extracted.append((table, rows))
    finally:
        _close_quietly(connection, "source connection")
    return extracted


def assert_source_text_types(table: str, row: dict[str, Any]) -> None:
    """Detect text values returned as ``bytes`` where ordinary text is expected.

    A configured charset is NOT proof: a server collation or a VARBINARY-ish
    column can still hand back bytes. Enum/JSON columns are exempt because
    map_enum()/map_json() accept bytes by design.
    """
    for col in TABLES[table].columns:
        value = row.get(col)
        if value is None or not isinstance(value, (bytes, bytearray)):
            continue
        if not is_ordinary_text_column(table, col):
            continue
        raise Fail(
            ExitCode.MAPPING,
            f"{table}.{col}: text value returned as bytes; the source URL must "
            f"declare charset={REQUIRED_SOURCE_CHARSET}.",
        )


# --------------------------------------------------------------------------- #
# Phase 5E.3 -- pre-import compatibility checks (pure; read-only)
# --------------------------------------------------------------------------- #
# Distinguished outcomes: ERROR blocks an import, WARNING is reported only
# (decision D-1), PASS means the check ran and found nothing.
CHECK_PASS = "PASS"
CHECK_WARNING = "WARNING"
CHECK_ERROR = "ERROR"

# Target UNIQUE constraints, transcribed from
# alembic_pg/versions/pg_baseline_initial_schema.py (ix_users_email / ix_categories_name)
# and app/models/user.py:33 / app/models/category.py:15.
UNIQUE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "email"),
    ("categories", "name"),
)

# Target CHECK constraints, transcribed verbatim from
# alembic_pg/versions/pg_baseline_initial_schema.py:99-105. Nothing else in the
# target schema carries a CHECK -- notably expenses.amount has NONE, so a
# negative expense is NOT an import blocker. Do not add entries without a
# matching constraint in the baseline migration.
NON_NEGATIVE_COLUMNS: dict[tuple[str, str], tuple[str, str]] = {
    ("products", "stock_quantity"): (
        "ck_products_stock_quantity_non_negative", "stock_quantity >= 0"),
    ("products", "cost_price"): (
        "ck_products_cost_price_non_negative", "cost_price IS NULL OR cost_price >= 0"),
}

# Authoritative Permission labels, transcribed from the application enum at
# app/models/user.py:18-25. This constant exists here (rather than importing
# app.models) because importing the application model would pull SQLAlchemy into
# this module and break the import-isolation guarantee. The unit suite imports
# the application enum lazily and asserts the two sets are identical, so drift
# is caught by a test rather than trusted.
PERMISSION_LABELS: frozenset[str] = frozenset({
    "MANAGE_PRODUCTS", "MANAGE_INVENTORY", "MANAGE_ORDERS", "MANAGE_CUSTOMERS",
    "MANAGE_DELIVERIES", "MANAGE_EXPENSES", "VIEW_REPORTS",
})

# Legacy order statuses that the reconciled target enum no longer accepts. They
# are named explicitly so the report is explicit; no mapping ships with this
# tool (decision D-4). An approved manifest is the ONLY way to import them.
LEGACY_ENUM_LABELS: dict[tuple[str, str], tuple[str, ...]] = {
    ("orders", "status"): ("SHIPPED", "DELIVERED"),
}

# Environment variable naming an approved enum-normalization manifest. Absent
# means "no manifest exists" and unsupported values are rejected.
ENUM_NORMALIZATION_MANIFEST_ENV = "IMPORT_ENUM_NORMALIZATION_MANIFEST"

# Above this many surrogate ids, the report stops listing them and reports a
# count instead. Keeps logs bounded without hiding any row from the operator.
_MAX_REPORTED_IDS = 20


@dataclass(frozen=True)
class CheckFinding:
    """One sanitized result of one compatibility check.

    ``count`` is the number of affected rows; ``ids`` holds at most
    ``_MAX_REPORTED_IDS`` surrogate primary-key values. NO source values,
    personal data, payment references, or credentials are ever stored here.
    """

    check: str
    status: str
    count: int = 0
    ids: tuple[int, ...] = ()
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == CHECK_ERROR

    def render(self) -> str:
        head = f"[{self.status}] {self.check}"
        if not self.count and self.status == CHECK_PASS:
            return f"{head}: ok"
        parts = [f"{head}: {self.count} row(s) affected"]
        if self.ids:
            shown = ", ".join(str(i) for i in self.ids)
            if self.count > len(self.ids):
                shown += f", ...(+{self.count - len(self.ids)} more)"
            parts.append(f"ids={shown}")
        if self.detail:
            parts.append(self.detail)
        return " ".join(parts)


@dataclass(frozen=True)
class CompatibilityReport:
    """Aggregated, deterministic outcome of every pre-import check."""

    findings: tuple[CheckFinding, ...] = ()

    @property
    def errors(self) -> tuple[CheckFinding, ...]:
        return tuple(f for f in self.findings if f.status == CHECK_ERROR)

    @property
    def warnings(self) -> tuple[CheckFinding, ...]:
        return tuple(f for f in self.findings if f.status == CHECK_WARNING)

    def has_errors(self) -> bool:
        return bool(self.errors)

    def summary(self) -> list[str]:
        """Sanitized, ordered report lines. Never includes source values."""
        return [f.render() for f in self.findings]

    def raise_if_blocking(self) -> None:
        """Fail closed, with a sanitized, aggregate-only message."""
        if not self.has_errors():
            return
        lines = "; ".join(f.render() for f in self.errors)
        raise Fail(ExitCode.MAPPING, "source data is not import-compatible: " + lines)


def _row_ids(rows: Iterable[dict[str, Any]]) -> list[Any]:
    """Surrogate primary keys only, in row order."""
    out: list[Any] = []
    for row in rows:
        value = row.get(SOURCE_PRIMARY_KEY) if isinstance(row, dict) else None
        if value is not None:
            out.append(value)
    return out


def _sample(ids: Iterable[Any]) -> tuple[int, ...]:
    """Deterministic, bounded sample of surrogate ids."""
    shown = list(ids)[:_MAX_REPORTED_IDS]
    return tuple(int(i) for i in shown if isinstance(i, int) or str(i).isdigit())


def _finding(check: str, rows_hit: list[Any], detail: str = "",
            status: str = CHECK_ERROR) -> CheckFinding:
    if not rows_hit:
        return CheckFinding(check=check, status=CHECK_PASS, detail=detail)
    return CheckFinding(
        check=check, status=status, count=len(rows_hit),
        ids=_sample(rows_hit), detail=detail,
    )


def load_enum_normalization_manifest(path: str | None) -> dict[tuple[str, str], dict[str, str]]:
    """Load an APPROVED enum-normalization manifest, or {} when none exists.

    A manifest is the ONLY sanctioned way to import a source value the target
    enum does not accept (decision D-4). Nothing is auto-approved: no default
    mapping ships with this tool, and a manifest is honoured only when
    IMPORT_ENUM_NORMALIZATION_MANIFEST names a readable file whose contents
    validate strictly:
      * keys must be "<table>.<column>" naming a real enum column;
      * values must be an object mapping source label -> target label;
      * every target label must already exist in that column's target enum.
    Anything else is a CONFIG failure, never a silent ignore.
    """
    if not path or not str(path).strip():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError:
        raise Fail(
            ExitCode.CONFIG,
            f"{ENUM_NORMALIZATION_MANIFEST_ENV} names a file that cannot be read; "
            "refusing to guess a mapping.",
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Fail(
            ExitCode.CONFIG,
            f"{ENUM_NORMALIZATION_MANIFEST_ENV} does not name valid JSON; refusing "
            "to guess a mapping.",
        )
    if not isinstance(raw, dict):
        raise Fail(ExitCode.CONFIG, "enum normalization manifest must be a JSON object.")
    manifest: dict[tuple[str, str], dict[str, str]] = {}
    for key, mapping in raw.items():
        if not isinstance(key, str) or "." not in key:
            raise Fail(
                ExitCode.CONFIG,
                "enum normalization manifest keys must be '<table>.<column>'.",
            )
        table, _, col = key.partition(".")
        if (table, col) not in ENUM_COLUMNS:
            raise Fail(
                ExitCode.CONFIG,
                f"enum normalization manifest key '{table}.{col}' is not an enum "
                "column; refusing unknown targets.",
            )
        if not isinstance(mapping, dict):
            raise Fail(
                ExitCode.CONFIG,
                f"enum normalization manifest entry '{key}' must be an object.",
            )
        allowed = set(ENUM_LABELS[ENUM_COLUMNS[(table, col)]])
        for source_label, target_label in mapping.items():
            if not isinstance(source_label, str) or not isinstance(target_label, str):
                raise Fail(
                    ExitCode.CONFIG,
                    f"enum normalization manifest entry '{key}' must map strings "
                    "to strings.",
                )
            if target_label not in allowed:
                raise Fail(
                    ExitCode.CONFIG,
                    f"enum normalization manifest maps '{table}.{col}' to "
                    f"'{target_label}', which the target enum does not accept; "
                    "refusing.",
                )
        manifest[(table, col)] = dict(mapping)
    return manifest


# --------------------------------------------------------------------------- #
# Blocking checks
# --------------------------------------------------------------------------- #
def check_enum_values(rows_by_table, manifest=None) -> list[CheckFinding]:
    """Reject source enum values the PostgreSQL enum does not accept.

    Blocking. An approved manifest is the only way through; no automatic
    normalization (decision D-4).
    """
    manifest = manifest or {}
    findings: list[CheckFinding] = []
    for (table, col), enum_name in sorted(ENUM_COLUMNS.items()):
        allowed = ENUM_LABELS[enum_name]
        mapping = manifest.get((table, col), {})
        hits: list[Any] = []
        legacy_hits: list[Any] = []
        for row in rows_by_table.get(table, ()):
            value = row.get(col)
            if value is None:
                continue  # nullability is a separate check
            if isinstance(value, (bytes, bytearray)):
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError:
                    hits.append(row.get(SOURCE_PRIMARY_KEY))
                    continue
            token = value.strip() if isinstance(value, str) else value
            if not isinstance(token, str):
                hits.append(row.get(SOURCE_PRIMARY_KEY))
                continue
            # An approved manifest mapping is honoured BEFORE any rejection;
            # every target label in a validated manifest is guaranteed to be
            # accepted by the target enum, so this cannot widen the domain.
            if token in mapping:
                continue
            if token not in allowed:
                if token in LEGACY_ENUM_LABELS.get((table, col), ()):
                    legacy_hits.append(row.get(SOURCE_PRIMARY_KEY))
                else:
                    hits.append(row.get(SOURCE_PRIMARY_KEY))
        hits = [h for h in hits if h is not None]
        legacy_hits = [h for h in legacy_hits if h is not None]
        if hits:
            findings.append(_finding(
                f"enum.{enum_name}", hits,
                f"{table}.{col}: unsupported value (not in the target {enum_name} "
                "enum and not covered by an approved manifest)"))
        if legacy_hits:
            findings.append(_finding(
                f"enum.{enum_name}.legacy", legacy_hits,
                f"{table}.{col}: legacy value requiring an approved normalization "
                "manifest; refused by default (decision D-4)"))
        if not hits and not legacy_hits:
            findings.append(CheckFinding(
                check=f"enum.{enum_name}", status=CHECK_PASS,
                detail=f"{table}.{col}"))
    return findings


def check_string_lengths(rows_by_table) -> list[CheckFinding]:
    """Reject values that would overflow the target VARCHAR(n). Blocking."""
    findings: list[CheckFinding] = []
    for table in TABLE_ORDER:
        spec = TABLES[table]
        for col in spec.columns:
            limit = TEXT_COLUMNS.get((table, col))
            if limit is None:
                continue
            hits: list[Any] = []
            for row in rows_by_table.get(table, ()):
                value = row.get(col)
                if value is None:
                    continue
                if isinstance(value, (bytes, bytearray)):
                    try:
                        value = value.decode("utf-8")
                    except UnicodeDecodeError:
                        hits.append(row.get(SOURCE_PRIMARY_KEY))
                        continue
                if not isinstance(value, str) or len(value) > limit:
                    hits.append(row.get(SOURCE_PRIMARY_KEY))
            hits = [h for h in hits if h is not None]
            if hits:
                findings.append(_finding(
                    f"length.{table}.{col}", hits,
                    f"exceeds the target limit of {limit} character(s)"))
            else:
                findings.append(CheckFinding(
                    check=f"length.{table}.{col}", status=CHECK_PASS))
    return findings


def check_unique_duplicates(rows_by_table) -> list[CheckFinding]:
    """Reject duplicate values on target-unique columns. Blocking.

    Reports counts and surrogate ids only; the duplicate value itself is never
    stored, logged, or rendered.
    """
    findings: list[CheckFinding] = []
    for table, col in UNIQUE_COLUMNS:
        seen: dict[Any, Any] = {}
        duplicates: list[Any] = []
        malformed = 0
        for row in rows_by_table.get(table, ()):
            value = row.get(col)
            if value is None:
                continue  # nullability handled elsewhere
            key = value
            try:
                if isinstance(key, (bytes, bytearray)):
                    key = key.decode("utf-8")
                if isinstance(key, str):
                    key = key.strip()
            except UnicodeDecodeError:
                malformed += 1
                continue
            row_id = row.get(SOURCE_PRIMARY_KEY)
            if key in seen:
                duplicates.append(row_id)
            else:
                seen[key] = row_id
        duplicates = [d for d in duplicates if d is not None]
        if duplicates or malformed:
            detail = f"duplicate value on a target-unique column"
            if malformed:
                detail += f"; {malformed} row(s) had an unusable value"
            findings.append(_finding(f"unique.{table}.{col}", duplicates, detail))
        else:
            findings.append(CheckFinding(check=f"unique.{table}.{col}", status=CHECK_PASS))
    return findings


def check_permissions(rows_by_table) -> list[CheckFinding]:
    """Validate users.permissions members against the application enum. Blocking.

    Unknown or malformed members are rejected (decision D-3). Entries are never
    silently discarded, renamed, or normalized.
    """
    unknown: list[Any] = []
    malformed: list[Any] = []
    for row in rows_by_table.get("users", ()):
        value = row.get("permissions")
        if value is None:
            continue
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                malformed.append(row.get(SOURCE_PRIMARY_KEY))
                continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                malformed.append(row.get(SOURCE_PRIMARY_KEY))
                continue
        if not isinstance(value, list):
            malformed.append(row.get(SOURCE_PRIMARY_KEY))
            continue
        row_id = row.get(SOURCE_PRIMARY_KEY)
        for member in value:
            if isinstance(member, (bytes, bytearray)):
                try:
                    member = member.decode("utf-8")
                except UnicodeDecodeError:
                    unknown.append(row_id)
                    continue
            if not isinstance(member, str) or member not in PERMISSION_LABELS:
                unknown.append(row_id)
    unknown = [u for u in unknown if u is not None]
    malformed = [m for m in malformed if m is not None]
    findings = [_finding("permissions.unknown", unknown,
                         "member not in the application Permission enum")]
    findings.append(_finding("permissions.shape", malformed,
                             "permissions is not a JSON array of labels"))
    if not unknown and not malformed:
        findings = [CheckFinding(check="permissions", status=CHECK_PASS)]
    return findings


def check_decimal_precision(rows_by_table) -> list[CheckFinding]:
    """Reject numerics that overflow NUMERIC(10,2) or need rounding. Blocking.

    Reuses map_decimal() so the row-level mapper and this bulk pre-check cannot
    disagree. Values are never rounded or altered.
    """
    findings: list[CheckFinding] = []
    for table in TABLE_ORDER:
        spec = TABLES[table]
        for col in sorted(spec.decimal_cols):
            hits: list[Any] = []
            for row in rows_by_table.get(table, ()):
                if row.get(col) is None:
                    continue  # nullable; nullability is a separate check
                try:
                    map_decimal(table, col, row.get(col), spec)
                except Fail:
                    hits.append(row.get(SOURCE_PRIMARY_KEY))
            hits = [h for h in hits if h is not None]
            if hits:
                findings.append(_finding(
                    f"decimal.{table}.{col}", hits,
                    "would overflow NUMERIC(10,2) or require rounding"))
            else:
                findings.append(CheckFinding(
                    check=f"decimal.{table}.{col}", status=CHECK_PASS))
    return findings


def check_required_columns(rows_by_table) -> list[CheckFinding]:
    """Reject NULLs in target-non-nullable columns. Blocking."""
    findings: list[CheckFinding] = []
    for table in TABLE_ORDER:
        spec = TABLES[table]
        for col in spec.columns:
            if col not in spec.not_null:
                continue
            hits = [row.get(SOURCE_PRIMARY_KEY) for row in rows_by_table.get(table, ())
                    if row.get(col) is None]
            hits = [h for h in hits if h is not None]
            if hits:
                findings.append(_finding(
                    f"notnull.{table}.{col}", hits,
                    "NULL in a column the target declares NOT NULL"))
            else:
                findings.append(CheckFinding(
                    check=f"notnull.{table}.{col}", status=CHECK_PASS))
    return findings


def check_foreign_keys(rows_by_table) -> list[CheckFinding]:
    """Detect child rows referencing a missing parent. Blocking.

    Covers every FK in FK_COLUMNS, which mirrors the PostgreSQL baseline's
    ForeignKeyConstraint declarations. Orphans are reported, never repaired.
    """
    findings: list[CheckFinding] = []
    parent_ids: dict[str, set[Any]] = {
        table: {row.get(SOURCE_PRIMARY_KEY) for row in rows_by_table.get(table, ())
                if row.get(SOURCE_PRIMARY_KEY) is not None}
        for table in TABLE_ORDER
    }
    for (child, col), parent in sorted(FK_COLUMNS.items()):
        keys = parent_ids.get(parent, set())
        hits: list[Any] = []
        for row in rows_by_table.get(child, ()):
            value = row.get(col)
            if value is None:
                continue  # nullable FKs are legitimate
            if value not in keys:
                hits.append(row.get(SOURCE_PRIMARY_KEY))
        hits = [h for h in hits if h is not None]
        if hits:
            findings.append(_finding(
                f"fk.{child}.{col}", hits,
                f"references a {parent} row that is absent from the source"))
        else:
            findings.append(CheckFinding(
                check=f"fk.{child}.{col}", status=CHECK_PASS))
    return findings


def check_datetimes(rows_by_table) -> list[CheckFinding]:
    """Reject values that cannot be represented in the target column. Blocking.

    Outcome A is preserved: a naive datetime is accepted unchanged; an aware
    datetime is refused rather than silently stripped; a zero/invalid date or a
    non-datetime value is refused. No timezone coercion happens anywhere.
    """
    findings: list[CheckFinding] = []
    for table in TABLE_ORDER:
        cols = [c for c in TABLES[table].columns
                if c in ("created_at", "updated_at", "paid_at")]
        for col in cols:
            hits: list[Any] = []
            for row in rows_by_table.get(table, ()):
                if row.get(col) is None:
                    continue
                try:
                    map_datetime(table, col, row.get(col))
                except Fail:
                    hits.append(row.get(SOURCE_PRIMARY_KEY))
            hits = [h for h in hits if h is not None]
            if hits:
                findings.append(_finding(
                    f"datetime.{table}.{col}", hits,
                    "not a naive datetime representable in TIMESTAMP WITHOUT "
                    "TIME ZONE"))
            else:
                findings.append(CheckFinding(
                    check=f"datetime.{table}.{col}", status=CHECK_PASS))
    return findings


def check_non_negative(rows_by_table) -> list[CheckFinding]:
    """Apply the target CHECK constraints' blocking behaviour. Blocking.

    Only columns carrying a real CHECK in the baseline are checked. There is no
    CHECK on expenses.amount, so a negative expense is not an error -- see
    NON_NEGATIVE_COLUMNS.
    """
    findings: list[CheckFinding] = []
    for (table, col), (constraint, expression) in sorted(NON_NEGATIVE_COLUMNS.items()):
        hits: list[Any] = []
        for row in rows_by_table.get(table, ()):
            value = row.get(col)
            if value is None:
                continue  # the constraint permits NULL
            try:
                ok = Decimal(str(value)) >= 0
            except (InvalidOperation, ValueError, ArithmeticError):
                ok = False
            if not ok:
                hits.append(row.get(SOURCE_PRIMARY_KEY))
        hits = [h for h in hits if h is not None]
        if hits:
            findings.append(_finding(
                f"check.{constraint}", hits,
                f"violates the target constraint {expression}"))
        else:
            findings.append(CheckFinding(
                check=f"check.{constraint}", status=CHECK_PASS,
                detail=f"{table}.{col}"))
    return findings


# --------------------------------------------------------------------------- #
# Report-only checks (decision D-1: warnings, never import blockers)
# --------------------------------------------------------------------------- #
def check_business_invariants(rows_by_table) -> list[CheckFinding]:
    """Report anomalies against application business rules. WARNING only.

    None of these blocks an import (decision D-1). Monetary comparisons use
    Decimal exclusively; nullable values are skipped rather than coerced. No
    value is ever stored in the finding.
    """
    findings: list[CheckFinding] = []
    orders = list(rows_by_table.get("orders", ()))
    payments = list(rows_by_table.get("payments", ()))
    items = list(rows_by_table.get("order_items", ()))

    # (a) multiple payments for one order
    per_order: dict[Any, list[Any]] = {}
    for payment in payments:
        order_id = payment.get("order_id")
        if order_id is None:
            continue
        per_order.setdefault(order_id, []).append(payment.get(SOURCE_PRIMARY_KEY))
    multi = sorted(oid for oid, ids in per_order.items()
                   if len([i for i in ids if i is not None]) > 1)
    findings.append(_finding("business.payments_per_order", multi,
                             "more than one payment record for one order",
                             status=CHECK_WARNING))

    # (b) payment amount vs order total
    order_totals = {o.get("id"): o.get("total_amount") for o in orders}
    amount_mismatch: list[Any] = []
    for payment in payments:
        order_id = payment.get("order_id")
        if order_id not in order_totals:
            continue
        try:
            if Decimal(str(payment.get("amount"))) != Decimal(str(order_totals[order_id])):
                amount_mismatch.append(payment.get(SOURCE_PRIMARY_KEY))
        except (InvalidOperation, ValueError, ArithmeticError):
            amount_mismatch.append(payment.get(SOURCE_PRIMARY_KEY))
    findings.append(_finding("business.payment_amount_vs_order_total", amount_mismatch,
                             "payment amount differs from the order total",
                             status=CHECK_WARNING))

    # (c) order item subtotal vs unit_price * quantity
    subtotal_mismatch: list[Any] = []
    for item in items:
        unit_price = item.get("unit_price")
        quantity = item.get("quantity")
        subtotal = item.get("subtotal")
        if unit_price is None or quantity is None or subtotal is None:
            continue
        try:
            if Decimal(str(subtotal)) != Decimal(str(unit_price)) * Decimal(str(quantity)):
                subtotal_mismatch.append(item.get(SOURCE_PRIMARY_KEY))
        except (InvalidOperation, ValueError, ArithmeticError):
            subtotal_mismatch.append(item.get(SOURCE_PRIMARY_KEY))
    findings.append(_finding("business.item_subtotal", subtotal_mismatch,
                             "subtotal differs from unit_price x quantity",
                             status=CHECK_WARNING))

    # (d) order total vs sum(item subtotals) + delivery_fee
    items_by_order: dict[Any, list[Any]] = {}
    for item in items:
        order_id = item.get("order_id")
        if order_id is None:
            continue
        items_by_order.setdefault(order_id, []).append(item.get("subtotal"))
    total_mismatch: list[Any] = []
    for order in orders:
        order_id = order.get("id")
        subtotals = items_by_order.get(order_id)
        if not subtotals:
            continue  # no line items to reconcile against
        try:
            summed = sum((Decimal(str(s)) for s in subtotals if s is not None),
                         Decimal("0"))
            fee = order.get("delivery_fee")
            fee = Decimal(str(fee)) if fee is not None else Decimal("0.00")
            if Decimal(str(order.get("total_amount"))) != summed + fee:
                total_mismatch.append(order_id)
        except (InvalidOperation, ValueError, ArithmeticError):
            total_mismatch.append(order_id)
    findings.append(_finding("business.order_total", total_mismatch,
                             "total_amount differs from sum(item subtotals) + "
                             "delivery_fee", status=CHECK_WARNING))

    findings = [f for f in findings if f.count or f.status == CHECK_PASS]
    if not findings:
        findings = [CheckFinding(check="business", status=CHECK_PASS)]
    return findings


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
# (stable finding prefix, function name). Function names are resolved through
# globals() at call time so a check can be substituted in tests.
_CHECK_ORDER: tuple[tuple[str, str], ...] = (
    ("enum", "check_enum_values"),
    ("length", "check_string_lengths"),
    ("unique", "check_unique_duplicates"),
    ("permissions", "check_permissions"),
    ("decimal", "check_decimal_precision"),
    ("notnull", "check_required_columns"),
    ("fk", "check_foreign_keys"),
    ("datetime", "check_datetimes"),
    ("check", "check_non_negative"),
    ("business", "check_business_invariants"),
)


def run_compatibility_checks(
    rows_by_table: dict[str, Iterable[dict[str, Any]]],
    *,
    manifest: dict[tuple[str, str], dict[str, str]] | None = None,
) -> CompatibilityReport:
    """Run every pre-import check over extracted rows. Pure; no database.

    Deterministic: checks run in a fixed order and each reports deterministically.
    Every check is wrapped so an unexpected exception becomes a sanitized ERROR
    finding -- malformed data can never bypass a check by crashing it.
    """
    if not isinstance(rows_by_table, dict):
        return CompatibilityReport((CheckFinding(
            check="input", status=CHECK_ERROR,
            detail="extracted rows must be a mapping of table to rows"),))

    findings: list[CheckFinding] = []
    for prefix, name in _CHECK_ORDER:
        check_fn = globals().get(name)
        if check_fn is None:
            findings.append(CheckFinding(
                check=prefix, status=CHECK_ERROR,
                detail="check is referenced but not defined; refusing"))
            continue
        try:
            if prefix == "enum":
                produced = check_fn(rows_by_table, manifest=manifest)
            else:
                produced = check_fn(rows_by_table)
        except Fail as exc:
            produced = [CheckFinding(check=prefix, status=CHECK_ERROR,
                                     detail=exc.message)]
        except Exception as exc:  # fail closed, never bypass a check
            produced = [CheckFinding(
                check=prefix, status=CHECK_ERROR,
                detail=f"check could not complete ({type(exc).__name__})")]
        findings.extend(produced)
    return CompatibilityReport(tuple(findings))


# --------------------------------------------------------------------------- #
# URL handling / sanitisation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedURL:
    scheme: str
    host: str | None
    port: int | None
    database: str | None
    has_credentials: bool


def parse_url(url: str) -> ParsedURL:
    # ``urlsplit`` itself, and the ``.hostname`` / ``.port`` properties, can
    # raise ValueError (malformed IPv6 brackets, non-numeric or out-of-range
    # port, illegal characters in the netloc). All of them must surface as the
    # project's sanitized CONFIG failure rather than an uncaught traceback.
    # The URL itself is never echoed back, so no credential can leak.
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or None
        port = parts.port
        database = (parts.path[1:] if parts.path else "") or None
        has_credentials = bool(parts.username or parts.password)
    except ValueError:
        raise Fail(
            ExitCode.CONFIG,
            "malformed connection URL (invalid scheme, host, port, or netloc).",
        )
    return ParsedURL(
        scheme=scheme,
        host=host,
        port=port,
        database=database,
        has_credentials=has_credentials,
    )


def sanitize_url(url: str) -> str:
    """Return a log-safe rendering: scheme + host + database. NO user/password."""
    p = parse_url(url)
    host = p.host or "<no-host>"
    db = p.database or "<no-db>"
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme or '<no-scheme>'}://***@{host}{port}/{db}"


def _is_loopback(host: str | None) -> bool:
    return host is not None and host.lower() in _LOOPBACK_HOSTS


def _looks_production_host(host: str | None) -> str | None:
    if not host:
        return "<no-host>"
    h = host.lower()
    for marker in _PRODUCTION_HOST_MARKERS:
        if marker in h:
            return marker
    return None


def _looks_production_db(db: str | None) -> str | None:
    if not db:
        return "<no-db>"
    d = db.lower()
    for marker in _PRODUCTION_DB_MARKERS:
        if marker in d:
            return marker
    return None


# --------------------------------------------------------------------------- #
# Configuration + modes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    source_url: str
    target_url: str
    source_is_disposable: str
    confirm_disposable_target: str
    run_id: str

    @property
    def expected_source_db(self) -> str:
        return derived_source_db(self.run_id)

    @property
    def source_schema(self) -> str:
        """Schema used to qualify source reads and information_schema lookups.

        source_identity() already requires the URL's database to equal this
        name, so the derived value is the authoritative one.
        """
        return self.expected_source_db

    @property
    def expected_target_db(self) -> str:
        return derived_target_db(self.run_id)


@dataclass(frozen=True)
class RunPlan:
    primary: str
    truncate_target: bool
    confirm_truncate: bool
    writes_target: bool

    @property
    def destructive(self) -> bool:
        return self.truncate_target


FAIL_MSG_URL_LEAK = (
    "Connection URLs must be supplied via the IMPORT_SOURCE_URL / "
    "IMPORT_TARGET_URL environment variables; they are not accepted as "
    "command-line arguments and are never logged."
)


def resolve_config(env: dict[str, str], run_id: str) -> Config:
    """G0 - configuration presence. Pure; performs no connection."""
    if not run_id or not RUN_ID_RE.match(run_id):
        raise Fail(
            ExitCode.USAGE,
            f"--run-id is required and must match {RUN_ID_RE.pattern} "
            f"({RUN_ID_FORMAT}).",
        )
    # The run id is the ONLY input that determines the disposable database
    # names. Re-check the derived names against the identity gates here so a
    # later pattern change can never make the documented convention silently
    # unrunnable (and so a bad id fails fast at USAGE, not at a gate).
    for label, name, pattern in (
        ("source", derived_source_db(run_id), SOURCE_DB_RE),
        ("target", derived_target_db(run_id), TARGET_DB_RE),
    ):
        if not pattern.match(name):
            raise Fail(
                ExitCode.USAGE,
                f"run-id '{run_id}' derives a {label} database name that does "
                f"not satisfy {pattern.pattern}; refusing.",
            )
    for name in ("IMPORT_SOURCE_URL", "IMPORT_TARGET_URL"):
        value = env.get(name, "")
        if not value or not value.strip():
            raise Fail(
                ExitCode.CONFIG,
                f"{name} is not set. {FAIL_MSG_URL_LEAK}",
            )
    source_url = env["IMPORT_SOURCE_URL"].strip()
    target_url = env["IMPORT_TARGET_URL"].strip()
    # Guard against accidentally shipping the app's URL here.
    for name, url in (("IMPORT_SOURCE_URL", source_url), ("IMPORT_TARGET_URL", target_url)):
        if url.startswith("sqlite"):
            raise Fail(ExitCode.CONFIG, f"{name} must not be a SQLite URL.")
    return Config(
        source_url=source_url,
        target_url=target_url,
        source_is_disposable=env.get("SOURCE_IS_DISPOSABLE_MYSQL", "").strip(),
        confirm_disposable_target=env.get("CONFIRM_DISPOSABLE_TARGET", "").strip(),
        run_id=run_id,
    )


def validate_schemes(cfg: Config) -> tuple[ParsedURL, ParsedURL]:
    """G1 - driver/scheme validation. Pure."""
    src = parse_url(cfg.source_url)
    tgt = parse_url(cfg.target_url)
    if src.scheme not in SUPPORTED_SOURCE_SCHEMES:
        raise Fail(
            ExitCode.CONFIG,
            "IMPORT_SOURCE_URL scheme must be 'mysql+pymysql'; got "
            f"'{src.scheme or '<none>'}'.",
        )
    if tgt.scheme not in SUPPORTED_TARGET_SCHEMES:
        raise Fail(
            ExitCode.CONFIG,
            "IMPORT_TARGET_URL scheme must be 'postgresql+psycopg2' or "
            f"'postgresql'; got '{tgt.scheme or '<none>'}'.",
        )
    if src.host is None or tgt.host is None:
        raise Fail(ExitCode.CONFIG, "Connection URLs must specify a host.")
    return src, tgt


def source_identity(cfg: Config, src: ParsedURL, env: dict[str, str]) -> ParsedURL:
    """G2 - disposable-source identity. Pure (config only)."""
    if cfg.source_is_disposable != "1":
        raise Fail(
            ExitCode.GATE,
            "SOURCE_IS_DISPOSABLE_MYSQL is not set to '1'; refusing to touch a "
            "source that is not explicitly attested disposable.",
        )
    if not _is_loopback(src.host) and env.get("IMPORT_ALLOW_REMOTE_SOURCE") != "1":
        raise Fail(
            ExitCode.GATE,
            "Source host is not loopback. A disposable MySQL restored from the "
            "backup is expected on 127.0.0.1; refusing unsafe remote source.",
        )
    if _looks_production_host(src.host):
        raise Fail(ExitCode.GATE, "Source host resembles a production endpoint.")
    if src.database in KNOWN_LOCAL_SOURCE_DBS:
        raise Fail(
            ExitCode.GATE,
            "Source database matches a known live/local database; will never "
            "silently fall back to the live database.",
        )
    if src.database != cfg.expected_source_db:
        raise Fail(
            ExitCode.GATE,
            "Source database must equal the run-id-keyed disposable name "
            f"'{cfg.expected_source_db}'; got '{src.database}'.",
        )
    return src


def target_identity(cfg: Config, tgt: ParsedURL, env: dict[str, str]) -> ParsedURL:
    """G3 - disposable-target identity. Pure (config only)."""
    if cfg.confirm_disposable_target != "yes":
        raise Fail(
            ExitCode.GATE,
            "CONFIRM_DISPOSABLE_TARGET is not set to 'yes'; refusing to operate "
            "on an unconfirmed target.",
        )
    if _looks_production_host(tgt.host):
        raise Fail(
            ExitCode.GATE,
            f"Target host resembles a production endpoint "
            f"('{_looks_production_host(tgt.host)}'); refusing.",
        )
    if _looks_production_db(tgt.database):
        raise Fail(
            ExitCode.GATE,
            f"Target database name resembles production "
            f"('{_looks_production_db(tgt.database)}'); refusing.",
        )
    if not TARGET_DB_RE.match(tgt.database or ""):
        raise Fail(
            ExitCode.GATE,
            "Target database name must match the rehearsal pattern '"
            + TARGET_DB_RE.pattern + "'; refusing ambiguous target.",
        )
    if not _is_loopback(tgt.host):
        allowed = env.get("IMPORT_DISPOSABLE_TARGET_HOST", "")
        if not allowed or allowed.lower() != (tgt.host or "").lower():
            raise Fail(
                ExitCode.GATE,
                "Target host is not loopback and not explicitly allow-listed "
                "via IMPORT_DISPOSABLE_TARGET_HOST; refusing unsafe remote target.",
            )
    return tgt


def resolve_plan(args: argparse.Namespace) -> RunPlan:
    """Fail closed on contradictory flag combinations."""
    truncate_target = getattr(args, "truncate_target", False)
    confirm_truncate = getattr(args, "confirm_truncate", False)
    primary = getattr(args, "primary", None)
    if truncate_target and primary != "apply":
        raise Fail(
            ExitCode.USAGE,
            "--truncate-target is only valid with --apply.",
        )
    if confirm_truncate and not (truncate_target and primary == "apply"):
        raise Fail(
            ExitCode.USAGE,
            "--confirm-truncate is only valid with --apply --truncate-target.",
        )
    writes = primary in ("apply", "reset_sequences")
    return RunPlan(
        primary=primary,
        truncate_target=bool(truncate_target),
        confirm_truncate=bool(confirm_truncate),
        writes_target=writes,
    )


# --------------------------------------------------------------------------- #
# Value mapping (pure; Outcome A for datetimes)
# --------------------------------------------------------------------------- #
def map_decimal(table: str, col: str, value: Any, spec: TableSpec) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str) and value.strip() != "":
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            raise Fail(ExitCode.MAPPING, f"{table}.{col}: not a decimal.")
    else:
        raise Fail(ExitCode.MAPPING, f"{table}.{col}: null/invalid decimal.")
    if not d.is_finite():
        raise Fail(ExitCode.MAPPING, f"{table}.{col}: non-finite decimal.")
    # NUMERIC(10,2): at most 2 fractional digits, at most 8 integer digits.
    exponent = -d.as_tuple().exponent
    if exponent > 2:
        raise Fail(
            ExitCode.MAPPING,
            f"{table}.{col}: {len(str(value))} char(s) with >2 decimals would "
            "require rounding; refusing (no silent rounding).",
        )
    if abs(d) > spec.max_decimal:
        raise Fail(ExitCode.MAPPING, f"{table}.{col}: exceeds NUMERIC(10,2).")
    return d


def map_bool(table: str, col: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in ("0", "1"):
        return value == "1"
    raise Fail(
        ExitCode.MAPPING,
        f"{table}.{col}: boolean source domain must be 0/1; refusing value.",
    )


def map_enum(table: str, col: str, value: Any) -> str:
    enum_name = ENUM_COLUMNS[(table, col)]
    allowed = ENUM_LABELS[enum_name]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "strict")
    if isinstance(value, str):
        token = value.strip()
        if token in allowed:
            return token
    raise Fail(
        ExitCode.MAPPING,
        f"{table}.{col}: value is not an allowed {enum_name} label; refusing "
        "(possible case mismatch or legacy status).",
    )


def map_json(table: str, col: str, value: Any) -> Any:
    kind = JSON_COLUMNS[(table, col)]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "strict")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise Fail(ExitCode.MAPPING, f"{table}.{col}: invalid JSON text.")
    if kind == "list":
        if not isinstance(value, list):
            raise Fail(ExitCode.MAPPING, f"{table}.{col}: expected a JSON list.")
        return value
    if kind == "object":
        if not isinstance(value, dict):
            raise Fail(ExitCode.MAPPING, f"{table}.{col}: expected a JSON object.")
        return value
    return value


def map_datetime(table: str, col: str, value: Any) -> datetime:
    # Outcome A: copy unchanged. Refuse (fail closed) rather than silently drop
    # timezone info, because dropping it would be an implicit tz policy choice.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value
    if isinstance(value, datetime) and value.tzinfo is not None:
        raise Fail(
            ExitCode.MAPPING,
            f"{table}.{col}: aware datetime encountered; refusing to strip tz "
            "(Outcome A forbids implicit conversion).",
        )
    raise Fail(ExitCode.MAPPING, f"{table}.{col}: not a datetime value.")


def map_value(n_table: str, n_col: str, n_value: Any) -> Any:
    spec = TABLES[n_table]
    # A NULL source value maps to Python ``None`` BEFORE any typed conversion is
    # attempted. Nullable target columns (e.g. ``products.cost_price``,
    # ``payments.payment_metadata``, ``orders.address_id``, ``payments.paid_at``)
    # must therefore import cleanly. NOT NULL enforcement is performed by
    # ``map_row()`` below, which still raises the sanitized ``MappingError`` for
    # any required column that ends up ``None`` -- so fail-closed behaviour for
    # required columns is unchanged.
    if n_value is None:
        return None
    if (n_table, n_col) in ENUM_COLUMNS:
        return map_enum(n_table, n_col, n_value)
    if n_col in spec.decimal_cols:
        return map_decimal(n_table, n_col, n_value, spec)
    if n_col in spec.bool_cols:
        return map_bool(n_table, n_col, n_value)
    if (n_table, n_col) in JSON_COLUMNS:
        return map_json(n_table, n_col, n_value)
    # datetime columns (created_at/updated_at/paid_at) are named by convention.
    if n_col == "paid_at" or n_col in ("created_at", "updated_at"):
        return map_datetime(n_table, n_col, n_value)
    # plain text / int passthrough (FK ids, names, descriptions, etc.)
    if n_col in spec.int_cols or (n_table, n_col) in FK_COLUMNS:
        if not isinstance(n_value, int):
            raise Fail(ExitCode.MAPPING, f"{n_table}.{n_col}: expected an integer.")
        return n_value
    return n_value


class MappingError(Exception):
    """One row failed mapping. Carries sanitized diagnostic info."""

    def __init__(self, table: str, row_id: Any, col: str, reason: str) -> None:
        super().__init__(reason)
        self.table = table
        self.row_id = row_id
        self.col = col
        self.reason = reason


def map_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Map a source row (dict keyed by column name) to target-typed values.

    Raises ``MappingError`` (sanitized: table, surrogate id, column, reason) on
    the first problem so a partial import can never be mistaken for success.
    """
    spec = TABLES[table]
    surrogate_id = row.get("id")
    mapped: dict[str, Any] = {}
    for col in spec.columns:
        if col not in row:
            raise MappingError(table, surrogate_id, col, "missing column")
        value = row[col]
        try:
            mapped[col] = map_value(table, col, value)
        except Fail as exc:
            raise MappingError(table, surrogate_id, col, exc.message)
        if mapped[col] is None and col in spec.not_null:
            raise MappingError(table, surrogate_id, col, "NOT NULL value missing")
    return mapped


# --------------------------------------------------------------------------- #
# Sequence handling (correct empty-table strategy)
# --------------------------------------------------------------------------- #
def compute_next_value(max_id: Any) -> int:
    """nextval target after import: (max id)+1, and 1 for an empty table."""
    if max_id is None:
        return 1
    if not isinstance(max_id, int) or max_id < 0:
        raise Fail(ExitCode.RUNTIME, f"invalid MAX(id) value: {max_id!r}")
    return max_id + 1


def sequence_sql(table: str) -> tuple[str, tuple[str, str]]:
    return ("SELECT pg_get_serial_sequence(%s, %s)", (table, "id"))


def setval_sql() -> str:
    # Both the sequence name and the value are bound parameters.
    return "SELECT setval(%s, %s, false)"


# --------------------------------------------------------------------------- #
# Reconciliation (pure helpers; DB queries live in the execution path)
# --------------------------------------------------------------------------- #
def diff_counts(source_counts: dict[str, int], target_counts: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for table in TABLE_ORDER:
        s = source_counts.get(table)
        t = target_counts.get(table)
        if s != t:
            problems.append(f"{table}: source={s} target={t}")
    return problems


def enum_domain_violations(table: str, col: str, rows: Iterable[tuple[Any, Any]]) -> list[str]:
    enum_name = ENUM_COLUMNS[(table, col)]
    allowed = set(ENUM_LABELS[enum_name])
    return [
        f"{table}.{col}: id={row_id} not in {enum_name}"
        for row_id, value in rows if value not in allowed
    ]


def check_order_amounts(order_id: int, total: Decimal, delivery_fee: Decimal,
                        line_subtotals: Iterable[Decimal]) -> bool:
    """Business rule (confirmed in app/main.py checkout):
    total_amount == sum(order_items.subtotal) + delivery_fee."""
    summed = sum((Decimal(s) for s in line_subtotals), Decimal("0"))
    return Decimal(total) == summed + Decimal(delivery_fee)


def check_stock_cost(stock_quantity: Any, cost_price: Any) -> tuple[bool, bool]:
    # Returns (stock_ok, cost_ok). A cost that cannot be parsed as a number is a
    # *reconciliation failure* (cost_ok=False), never an unhandled
    # ``decimal.InvalidOperation`` escaping into the caller.
    stock_ok = isinstance(stock_quantity, int) and stock_quantity >= 0
    if cost_price is None:
        cost_ok = True
    else:
        try:
            cost_ok = Decimal(str(cost_price)) >= 0
        except (InvalidOperation, ValueError, ArithmeticError):
            cost_ok = False
    return stock_ok, cost_ok


# --------------------------------------------------------------------------- #
# Lazy database engines (executed only in Phase 5E)
# --------------------------------------------------------------------------- #
def make_engine(url: str, *, is_target: bool):
    try:
        from sqlalchemy import create_engine
    except Exception as exc:  # pragma: no cover - depends on runtime env
        raise Fail(ExitCode.RUNTIME, f"SQLAlchemy unavailable: {exc}")
    if is_target:
        try:
            import psycopg2  # noqa: F401
        except Exception:
            raise Fail(ExitCode.RUNTIME, "psycopg2 driver is not installed; install requirements first.")
        return create_engine(url, pool_pre_ping=True)
    try:
        import pymysql  # noqa: F401
    except Exception:
        raise Fail(ExitCode.RUNTIME, "pymysql driver is not installed; install requirements first.")
    return create_engine(url, pool_pre_ping=True)


def open_source_connection(url: str, *, is_target: bool = False, schema: str | None = None):
    """The ONLY sanctioned way to obtain a live source connection.

    Refuses outright while ``EXECUTION_ENABLED`` is False, so no socket can be
    created for a database by this utility until an authorized Phase 5E build
    flips the constant in source. This is checked here rather than at the call
    site so a future caller cannot forget it.

    Returns a raw DB-API connection backed by a lazily created engine that the
    caller MUST close (see extract_source_rows(), which does exactly that).
    """
    if not EXECUTION_ENABLED:
        raise Fail(
            ExitCode.GATE,
            "database execution is disabled in this build (EXECUTION_ENABLED="
            "False); no source connection can be opened.",
        )
    if is_target:
        raise Fail(
            ExitCode.GATE,
            "target connections are not implemented in this build; refusing.",
        )
    if schema is not None:
        validate_source_schema_name(schema)
    engine = make_engine(url, is_target=False)
    return engine.raw_connection()


def source_connection_factory(cfg: "Config", schema: str | None = None):
    """A ``connect`` callable for extract_source_rows(), bound to one config."""
    return lambda: open_source_connection(cfg.source_url, schema=schema)


# --------------------------------------------------------------------------- #
# Target connection + transaction boundary (Phase 5E.5)
# --------------------------------------------------------------------------- #
def open_target_connection(url: str, *, schema: str | None = None):
    """The ONLY sanctioned way to obtain a live target connection.

    Opens a SQLAlchemy engine and returns a raw DB-API connection.
    The caller MUST close the connection.

    Refuses outright while ``EXECUTION_ENABLED`` is False, so no socket can be
    created for a database by this utility until an authorized Phase 5E build
    flips the constant in source. This is checked here rather than at the call
    site so a future caller cannot forget it.
    """
    if not EXECUTION_ENABLED:
        raise Fail(
            ExitCode.GATE,
            "database execution is disabled in this build (EXECUTION_ENABLED="
            "False); no target connection can be opened.",
        )
    if schema is not None:
        # Target schema validation is not needed for the rehearsal pattern;
        # the database name is validated by target_identity() upstream.
        pass
    engine = make_engine(url, is_target=True)
    return engine.raw_connection()


def target_connection_factory(cfg: "Config", schema: str | None = None):
    """A ``connect`` callable bound to the configured target."""
    return lambda: open_target_connection(cfg.target_url, schema=schema)


def _transaction_is_active(transaction: Any) -> bool:
    """Negotiate an activity flag from a transaction object.

    Tries, in order: an ``is_active`` attribute, an ``is_active()`` callable, an
    ``in_transaction()`` callable. When none is present the answer is
    conservative (True), so a rollback is attempted rather than skipped; the
    caller's rollback path already tolerates a rollback that fails.
    """
    for attr in ("is_active", "in_transaction"):
        candidate = getattr(transaction, attr, None)
        if isinstance(candidate, bool):
            return candidate
        if callable(candidate):
            try:
                return bool(candidate())
            except Exception:
                return True
    return True


def _call_or_raise(target: Any, method: str, what: str) -> None:
    """Invoke ``target.method()``, converting a driver error into a sanitized
    RUNTIME failure. Fail exceptions are not swallowed."""
    fn = getattr(target, method, None)
    if not callable(fn):
        raise Fail(
            ExitCode.RUNTIME,
            f"the {what} does not provide {method}(); refusing to proceed.",
        )
    try:
        fn()
    except Fail:
        raise
    except Exception:
        raise Fail(
            ExitCode.RUNTIME,
            f"could not {what}.{method}() cleanly.",
        )


class TargetTransaction:
    """A narrow transaction-boundary wrapper. Phase 5E.4B-1.

    Delegates entirely to an injected connection and an injected ``begin``
    callable that turns a connection into a transaction object exposing
    ``commit()``, ``rollback()`` and (optionally) an activity flag.

    What it guarantees (all proven with fakes, never with a live database):
      * the unit of work runs *inside* the transaction, so a commit happens
        only after the caller's work and any pre-commit checks succeed;
      * ``commit()`` is called exactly once, and only on the success path;
      * ``rollback()`` is called exactly once on any failure, and only when the
        transaction reports itself active;
      * a commit failure is surfaced (never swallowed) and is followed by a
        rollback attempt when the transaction is still active;
      * no exception is ever converted into a success result -- ``__exit__``
        always returns False;
      * the connection is closed on every path;
      * a cleanup failure never replaces the original failure: the original
        exception propagates and cleanup problems are retained only as
        sanitized text on ``cleanup_errors``;
      * a finished transaction cannot be reused.

    What it deliberately does NOT do:
      * it does not check ``EXECUTION_ENABLED``. Gating is the caller's job and
        is enforced in ``main()`` *before* any dispatch; checking here would
        make the injected-fake tests impossible without flipping the guard;
      * it does not know about tables, rows, INSERT/TRUNCATE/setval SQL,
        reconciliation, or any import behaviour -- those belong to a later
        independently reviewed phase;
      * it does not prove PostgreSQL atomicity. See the runbook's
        "Phase 5E.4B-1" section: a real disposable-PostgreSQL integration test
        is a mandatory prerequisite before this is trusted.
    """

    def __init__(self, connect: Callable[[], Any],
                 begin: Callable[[Any], Any] | None = None,
                 *, label: str = "target") -> None:
        if not callable(connect):
            raise Fail(ExitCode.RUNTIME,
                       "TargetTransaction requires a callable connect.")
        if begin is not None and not callable(begin):
            raise Fail(ExitCode.RUNTIME,
                       "TargetTransaction begin must be callable when given.")
        self._connect = connect
        self._begin = begin
        self._label = label
        self._connection: Any = None
        self._transaction: Any = None
        self._finished = False
        self.cleanup_errors: list[str] = []

    # -- internal helpers -------------------------------------------------- #
    def _open(self) -> None:
        try:
            self._connection = self._connect()
        except Fail:
            raise
        except Exception:
            raise Fail(
                ExitCode.RUNTIME,
                f"could not open the {self._label} connection.")
        if self._begin is None:
            # No transaction implementation has been reviewed yet. Fail closed
            # rather than inventing one; the connection is still cleaned up.
            self._close_connection()
            raise Fail(
                ExitCode.GATE,
                "no target transaction implementation is available in this "
                "build; refusing to open a transaction without one.")
        try:
            self._transaction = self._begin(self._connection)
        except Fail:
            self._close_connection()
            raise
        except Exception:
            self._close_connection()
            raise Fail(
                ExitCode.RUNTIME,
                f"could not begin a {self._label} transaction.")

    def _note_cleanup(self, what: str) -> None:
        """Record a sanitized cleanup problem without hiding the original."""
        self.cleanup_errors.append(what)

    def _safe_rollback(self) -> None:
        if self._transaction is None:
            return
        if not _transaction_is_active(self._transaction):
            return
        try:
            _call_or_raise(self._transaction, "rollback", self._label)
        except Fail as exc:
            self._note_cleanup(exc.message)

    def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            _close_quietly(connection, f"{self._label} connection")
        except Fail as exc:
            self._note_cleanup(exc.message)

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> Any:
        if self._finished:
            raise Fail(
                ExitCode.RUNTIME,
                "refusing to reuse a finished target transaction.")
        if self._transaction is not None or self._connection is not None:
            raise Fail(
                ExitCode.RUNTIME,
                "refusing to re-enter a target transaction that is already open.")
        self._open()
        return self._transaction

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Never swallow: returning False propagates whatever ``exc`` carries.
        primary = exc
        try:
            if primary is not None:
                self._safe_rollback()
            else:
                try:
                    _call_or_raise(self._transaction, "commit", self._label)
                except Fail as commit_error:
                    # A commit failure must surface, and a rollback is attempted
                    # only when the transaction still reports itself active.
                    primary = commit_error
                    self._safe_rollback()
        finally:
            self._finished = True
            transaction, self._transaction = self._transaction, None
            if transaction is not None:
                # The wrapper does not own transaction disposal; it only stops
                # referencing it so a finished transaction can never be reused.
                del transaction
            self._close_connection()
        if primary is None and self.cleanup_errors:
            # Nothing failed in the transaction itself, so a cleanup problem is
            # the only problem there is -- surface it rather than hide it.
            raise Fail(
                ExitCode.RUNTIME,
                f"{self._label} cleanup failed after a successful commit: "
                + "; ".join(self.cleanup_errors))
        # A commit (or begin/close) failure discovered here has no exception
        # context to ride on -- the with-body completed normally -- so it MUST
        # be raised explicitly. Assigning it to a local is not enough, and
        # silently returning would convert a failed commit into a success.
        if primary is not None and exc is None:
            raise primary
        return False


def inspect_source(cfg, connect=None, *, schema: str | None = None,
                  env: dict[str, str] | None = None) -> int:
    """Read-only source inspection: schema guard + extraction + text-type audit
    + Phase 5E.3 pre-import compatibility checks.

    Opens NO target connection and performs NO write, DDL, truncation, or
    sequence action. ``connect`` is injectable so tests supply a fake instead
    of a socket.

    Reports aggregate counts and surrogate ids only; never emails, addresses,
    payment references, payment metadata, credentials, or complete URLs.

    Returns ExitCode.OK when nothing blocks, ExitCode.MAPPING when a blocking
    compatibility issue exists, or the Fail code of a config/gate problem.
    """
    validate_source_charset(cfg)
    schema = schema or cfg.source_schema
    validate_source_schema_name(schema)
    connect = connect or source_connection_factory(cfg, schema=schema)
    environ = os.environ if env is None else env

    # 1. Schema guard, read via a cursor on the same connection as extraction.
    connection = None
    try:
        try:
            connection = connect()
        except Fail:
            raise
        except Exception:
            raise Fail(ExitCode.RUNTIME,
                       "could not open the disposable MySQL source connection.")
        cursor = connection.cursor()
        try:
            source_columns = fetch_source_columns(cursor, schema)
        finally:
            _close_quietly(cursor, "source schema cursor")
    finally:
        if connection is not None:
            _close_quietly(connection, "source connection")

    _, warnings = assert_source_schema_matches(source_columns)
    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)

    # 2. Extraction (same read-only rules; schema-qualified, ordered by PK).
    extracted = extract_source_rows(connect, schema=schema)
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table, rows in extracted:
        for row in rows:
            assert_source_text_types(table, row)
        rows_by_table[table] = rows
        print(f"[source] {table}: {len(rows)} row(s)", file=sys.stderr)

    # 3. Phase 5E.3 compatibility checks over the extracted rows.
    manifest = load_enum_normalization_manifest(
        environ.get(ENUM_NORMALIZATION_MANIFEST_ENV, ""))
    report = run_compatibility_checks(rows_by_table, manifest=manifest)
    for line in report.summary():
        print(f"[check] {line}", file=sys.stderr)

    print(
        f"[source] schema compatible for {len(extracted)} approved table(s); "
        "read-only inspection complete (no target touched).",
        file=sys.stderr,
    )
    if report.has_errors():
        print(
            f"[abort:{ExitCode.MAPPING}] {len(report.errors)} blocking "
            f"compatibility check(s) failed; {len(report.warnings)} warning(s) "
            "reported. No data was written.",
            file=sys.stderr,
        )
        return ExitCode.MAPPING
    return ExitCode.OK


def truncate_sql() -> str:
    """Build TRUNCATE SQL with quoted identifiers (defence-in-depth)."""
    return "TRUNCATE TABLE " + ", ".join(quote_ident(t) for t in TABLE_ORDER) + " RESTART IDENTITY CASCADE"


def quote_ident(name: str) -> str:
    # Only ever called with constants from TABLE_ORDER / TableSpec.columns.
    if not re.match(r"^[a-z_][a-z0-9_]*$", name):
        raise Fail(ExitCode.RUNTIME, f"refusing unsafe SQL identifier {name!r}")
    return '"' + name + '"'


# Backward-compatible constant for existing tests
TRUNCATE_SQL = truncate_sql()


def insert_sql(table: str) -> str:
    cols = [quote_ident(c) for c in TABLES[table].columns]
    placeholders = ", ".join(["%s"] * len(cols))
    return f"INSERT INTO {quote_ident(table)} ({', '.join(cols)}) VALUES ({placeholders})"


# This constant is the SINGLE switch that gates every database execution branch.
# It is deliberately NOT read from argv, os.environ, or any config file, so
# there is no way to enable database execution from the CLI or the shell: it is
# flipped to True ONLY in the source of an explicitly authorized Phase 5E build.
EXECUTION_ENABLED = False

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pg_import",
        description="MySQL(backup) -> disposable PostgreSQL import utility (Phase 5D design).",
    )
    p.add_argument("--run-id", required=True,
                   help="unique rehearsal id, lowercase alphanumeric 3-32 chars "
                        "(drives both disposable database names)")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", dest="primary", action="store_const", const="apply",
                     help="import data into the disposable target (after migration)")
    grp.add_argument("--validate-only", dest="primary", action="store_const",
                     const="validate_only", help="read-only source/target reconciliation")
    grp.add_argument("--dry-run", dest="primary", action="store_const", const="dry_run",
                     help="run gates + read-only target checks; no writes")
    grp.add_argument("--reset-sequences", dest="primary", action="store_const",
                     const="reset_sequences", help="reset target id sequences only")
    p.add_argument("--truncate-target", action="store_true",
                   help="(only with --apply) empty the disposable target first")
    p.add_argument("--confirm-truncate", action="store_true",
                   help="(only with --apply --truncate-target) explicit confirmation for destructive truncation")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        plan = resolve_plan(args)
        cfg = resolve_config(os.environ, args.run_id)
        src, tgt = validate_schemes(cfg)
        validate_source_charset(cfg)
        source_identity(cfg, src, os.environ)
        target_identity(cfg, tgt, os.environ)
    except Fail as exc:
        print(f"[abort:{exc.code}] {exc.message}", file=sys.stderr)
        return exc.code

    print(
        f"[plan] mode={plan.primary} truncate={plan.truncate_target} "
        f"run_id={cfg.run_id}\n"
        f"[plan] source={sanitize_url(cfg.source_url)}\n"
        f"[plan] target={sanitize_url(cfg.target_url)}\n"
        "[plan] timestamp policy = Outcome A (copy unchanged; no offset applied)",
        file=sys.stderr,
    )

    # All modes requiring a database are gated behind explicit authorization.
    if not EXECUTION_ENABLED:
        print(
            "[abort] database execution is disabled in this build "
            "(EXECUTION_ENABLED=False); it is enabled only in the explicitly "
            "authorized Phase 5E execution build. Gates passed.",
            file=sys.stderr,
        )
        return ExitCode.GATE
    _dispatch_db_mode(plan, cfg, src, tgt)  # pragma: no cover - Phase 5E
    return ExitCode.OK


def _dispatch_db_mode(plan, cfg, src, tgt):  # pragma: no cover - implemented/run in Phase 5E
    """Phase 5E.2 implements ONLY the read-only source-inspection modes.

    ``--validate-only`` and ``--dry-run`` currently perform source-side schema
    validation, extraction, and the text-type audit. They open NO target
    connection and perform NO write, DDL, truncation, or sequence reset.
    ``--apply`` and ``--reset-sequences`` are still unimplemented and fail
    closed.

    Still to be built before this can report success:
      * target connection + target-side reconciliation for validate-only/dry-run
      * the 5E.3 pre-import compatibility pass (legacy enums, VARCHAR lengths,
        duplicate unique values, permissions members, decimal scale, NULL in
        required columns, FK references)
      * source read + ordered parents-to-children insert inside ONE transaction
      * TRUNCATE (transactional under PostgreSQL RESTART IDENTITY CASCADE)
      * the reconciliation QUERIES themselves (nine-table counts, PK uniqueness /
        preserved ids, FK orphan count, NUMERIC precision, order-total rule,
        payment/order consistency, JSON shape, NULL/NOT NULL, timestamp
        equality, sequence state) -- only the pure helpers below exist today
      * setval() after a successful load, then a single commit / single rollback
    Building blocks that already exist:
      * make_engine()            -- lazy driver import + engine creation
      * insert_sql()/sequence_sql()/setval_sql()/truncate_sql() -- safe SQL builders
      * map_row()                -- per-row type mapping (Outcome A datetimes)
      * assert_source_schema_matches()/extract_source_rows() -- Phase 5E.2 read-only source layer
      * diff_counts()/enum_domain_violations()/check_order_amounts()/
        check_stock_cost()       -- pure reconciliation helpers
    """
    import psycopg2.extras

    # Fail closed for invalid/missing arguments (preserves test expectations)
    if plan is None or cfg is None:
        raise Fail(ExitCode.GATE, "database execution path not implemented")

    mode = getattr(plan, "primary", None)

    # EXECUTION_ENABLED guard for write modes (read-only modes allowed per Phase 5E.2)
    write_modes = ("apply", "reset_sequences")
    if mode in write_modes and not EXECUTION_ENABLED:
        raise Fail(
            ExitCode.GATE,
            f"database execution path '{mode}' is not implemented in this build; "
            "only the read-only source-inspection modes are.",
        )

    if mode in ("validate_only", "dry_run"):
        return inspect_source(cfg)

    # --- Phase 5E.5: --apply and --reset-sequences implementation ---

    # Source inspection (schema guard + extraction + compatibility checks)
    source_result = inspect_source(cfg)
    if source_result != ExitCode.OK:
        return source_result

    # Re-extract rows for the actual import (inspect_source only validates)
    schema = cfg.source_schema
    validate_source_schema_name(schema)
    connect = source_connection_factory(cfg, schema=schema)
    extracted = extract_source_rows(connect, schema=schema)
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table, rows in extracted:
        for row in rows:
            assert_source_text_types(table, row)
        rows_by_table[table] = rows

    # Target connection
    target_connect = target_connection_factory(cfg)
    target_conn = target_connect()
    try:
        # Use autocommit=False for transaction control
        target_conn.autocommit = False

        # Helper: check if target database has any data
        def _target_has_data(conn) -> bool:
            cursor = conn.cursor()
            try:
                for table in TABLE_ORDER:
                    cursor.execute(f"SELECT 1 FROM {quote_ident(table)} LIMIT 1")
                    if cursor.fetchone() is not None:
                        return True
                return False
            finally:
                cursor.close()

        # --- Non-empty target protection for --apply (without --truncate-target) ---
        if mode == "apply" and not plan.truncate_target:
            if _target_has_data(target_conn):
                raise Fail(
                    ExitCode.GATE,
                    "target database is not empty; use --truncate-target with --confirm-truncate to overwrite",
                )

        # --- --reset-sequences: require empty target and re-verify identity ---
        if mode == "reset_sequences":
            # Re-verify target identity before any sequence operation
            src_parsed, tgt_parsed = validate_schemes(cfg)
            target_identity(cfg, tgt_parsed, os.environ)
            # Require empty target (no truncate allowed for reset-sequences)
            if _target_has_data(target_conn):
                raise Fail(
                    ExitCode.GATE,
                    "target database is not empty; --reset-sequences requires an empty target",
                )

        # --- TRUNCATE if requested (only for --apply with --truncate-target) ---
        if plan.truncate_target:
            if not plan.confirm_truncate:
                raise Fail(
                    ExitCode.GATE,
                    "--confirm-truncate is required when using --truncate-target",
                )
            # Verify target is empty or explicitly confirmed
            cursor = target_conn.cursor()
            try:
                # Execute truncate (inside transaction, will roll back on failure)
                cursor.execute(truncate_sql())
            finally:
                cursor.close()

        # --- Import or sequence reset ---
        if mode == "apply":
            # Single transaction for all inserts + setval
            try:
                _import_all_tables(target_conn, rows_by_table)
                _reset_sequences(target_conn, rows_by_table)
                target_conn.commit()
            except Exception:
                target_conn.rollback()
                raise
        elif mode == "reset_sequences":
            # Only reset sequences, no inserts, no truncation
            try:
                _reset_sequences(target_conn, rows_by_table)
                target_conn.commit()
            except Exception:
                target_conn.rollback()
                raise
        else:
            raise Fail(
                ExitCode.GATE,
                f"database execution path '{mode or '<unknown>'}' is not implemented.",
            )
    finally:
        target_conn.close()

    return ExitCode.OK


def _import_all_tables(conn, rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    """Insert all rows for all tables in TABLE_ORDER inside a transaction."""
    cursor = conn.cursor()
    try:
        for table in TABLE_ORDER:
            rows = rows_by_table.get(table, [])
            if not rows:
                continue
            sql = insert_sql(table)
            # Prepare parameters with JSON adaptation for JSON columns
            for row in rows:
                mapped = map_row(table, row)
                params = _adapt_params_for_json(table, mapped)
                cursor.execute(sql, params)
    finally:
        cursor.close()


def _adapt_params_for_json(table: str, mapped: dict[str, Any]) -> list[Any]:
    """Adapt parameter values for PostgreSQL, wrapping JSON columns in psycopg2.extras.Json."""
    import psycopg2.extras
    spec = TABLES[table]
    params = []
    for col in spec.columns:
        value = mapped[col]
        if (table, col) in JSON_COLUMNS and value is not None:
            # Wrap list/dict in psycopg2.extras.Json for proper JSON column insertion
            params.append(psycopg2.extras.Json(value))
        else:
            params.append(value)
    return params


def _reset_sequences(conn, rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    """Reset sequences for all tables to MAX(id)+1 (or 1 if empty)."""
    cursor = conn.cursor()
    try:
        for table in TABLE_ORDER:
            # Get the sequence name
            seq_sql, seq_params = sequence_sql(table)
            cursor.execute(seq_sql, seq_params)
            seq_row = cursor.fetchone()
            if seq_row is None or seq_row[0] is None:
                # No sequence (e.g., table without SERIAL)
                continue
            seq_name = seq_row[0]

            # Compute next value from source data
            rows = rows_by_table.get(table, [])
            max_id = max((row.get("id") for row in rows if row.get("id") is not None), default=None)
            next_val = compute_next_value(max_id)

            # Set sequence
            cursor.execute(setval_sql(), (seq_name, next_val))
    finally:
        cursor.close()


if __name__ == "__main__":
    raise SystemExit(main())
