"""Isolated unit tests for tools.pg_import (Phase 5D).

These tests are deliberately database-free: they exercise only the pure
gate / mapping / mode / reconciliation logic. Importing tools.pg_import does
NOT import SQLAlchemy, pymysql, or psycopg2 (those are imported lazily inside
the execution paths that are gated behind Phase 5E), so this module is safe to
run with no database, driver, container, or network available.

Run from the backend directory so the ``tools`` package is importable:

    cd backend
    python -m pytest tests/test_pg_import_unit.py -q
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tools import pg_import as P


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_BACKEND_DIR = Path(__file__).resolve().parents[1]

VALID_SRC = "mysql+pymysql://root:secret@127.0.0.1:3306/twelve09_src_run1?charset=utf8mb4"
VALID_TGT = "postgresql+psycopg2://user:secret@127.0.0.1:5432/twelve09_rehearsal"
ATT = {"SOURCE_IS_DISPOSABLE_MYSQL": "1", "CONFIRM_DISPOSABLE_TARGET": "yes"}


def _config_env(*, source_url=VALID_SRC, target_url=VALID_TGT, extra=None):
    env = {"IMPORT_SOURCE_URL": source_url, "IMPORT_TARGET_URL": target_url}
    env.update(ATT)
    if extra is not None:
        env.update(extra)
    return env


def _plan(primary, truncate=False):
    return P.resolve_plan(argparse.Namespace(primary=primary, truncate_target=truncate))


def _order_row(**overrides):
    """A complete, valid ``orders`` row (15 columns)."""
    row = {
        "id": 9, "user_id": 1, "address_id": 2, "total_amount": Decimal("120.00"),
        "status": "PENDING", "fulfillment_method": "STORE_DELIVERY",
        "delivery_fee": Decimal("20.00"), "delivery_recipient_name": "Recip",
        "delivery_phone_number": "08030000000", "delivery_address_line": "1 Road",
        "delivery_city": "City", "delivery_state": "State",
        "delivery_additional_directions": None,
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    row.update(overrides)
    return row


def _product_row(**overrides):
    """A complete, valid ``products`` row (11 columns)."""
    row = {
        "id": 5, "category_id": 1, "name": "Toy", "description": None,
        "price": Decimal("10.00"), "cost_price": Decimal("4.00"),
        "stock_quantity": 3, "image_url": None, "is_active": True,
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    row.update(overrides)
    return row


def _payment_row(**overrides):
    """A complete, valid ``payments`` row (13 columns)."""
    row = {
        "id": 7, "order_id": 1, "amount": Decimal("150.00"), "status": "SUCCESS",
        "payment_method": "card", "provider": "paystack",
        "provider_reference": "ref-1", "transaction_reference": "tx-1",
        "payment_metadata": {"channel": "card"}, "currency": "NGN",
        "paid_at": datetime(2026, 8, 14, 10, 0, 0),
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Mode resolution (fail closed on contradictions)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("primary", ["validate_only", "dry_run", "reset_sequences"])
def test_truncate_rejected_outside_apply(primary):
    with pytest.raises(P.Fail) as ei:
        _plan(primary, truncate=True)
    assert ei.value.code == P.ExitCode.USAGE


def test_plan_apply_flags():
    assert _plan("apply").writes_target and not _plan("apply").destructive
    assert _plan("apply", truncate=True).destructive
    assert not _plan("validate_only").writes_target
    assert _plan("reset_sequences").writes_target


# --------------------------------------------------------------------------- #
# G0 configuration
# --------------------------------------------------------------------------- #
def test_g0_missing_url():
    with pytest.raises(P.Fail) as ei:
        P.resolve_config({}, "run1")
    assert ei.value.code in (P.ExitCode.CONFIG, P.ExitCode.USAGE)


def test_g0_rejects_sqlite_url():
    env = _config_env(target_url="sqlite:////tmp/x.sqlite")
    with pytest.raises(P.Fail) as ei:
        P.resolve_config(env, "run1")
    assert ei.value.code == P.ExitCode.CONFIG


def test_run_id_required():
    with pytest.raises(P.Fail):
        P.resolve_config(_config_env(), "")


# --------------------------------------------------------------------------- #
# G1 schemes
# --------------------------------------------------------------------------- #
def test_g1_wrong_schemes_swapped():
    cfg = P.resolve_config(
        _config_env(source_url="postgresql+psycopg2://u@127.0.0.1/db",
                    target_url="mysql+pymysql://u@127.0.0.1/db"),
        "run1",
    )
    with pytest.raises(P.Fail) as ei:
        P.validate_schemes(cfg)
    assert ei.value.code == P.ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# URL sanitisation
# --------------------------------------------------------------------------- #
def test_sanitize_redacts_credentials():
    out = P.sanitize_url(VALID_TGT)
    assert "secret" not in out
    assert "@127.0.0.1:5432/twelve09_rehearsal" in out
    assert out.startswith("postgresql+psycopg2://***")


# --------------------------------------------------------------------------- #
# G2 source identity
# --------------------------------------------------------------------------- #
def _run_source(url, extra=None, run_id="run1"):
    cfg = P.resolve_config(_config_env(source_url=url, extra=extra), run_id)
    src, _ = P.validate_schemes(cfg)
    return P.source_identity(cfg, src, {**(ATT), **(extra or {})})


def test_g2_refuses_live_db():
    with pytest.raises(P.Fail) as ei:
        _run_source("mysql+pymysql://root:x@127.0.0.1:3306/twelve09_kiddies_store")
    assert ei.value.code == P.ExitCode.GATE


def test_g2_requires_runid_keyed_name():
    with pytest.raises(P.Fail) as ei:
        _run_source("mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_OTHER")
    assert ei.value.code == P.ExitCode.GATE


def test_g2_requires_attestation():
    with pytest.raises(P.Fail) as ei:
        env = {"IMPORT_SOURCE_URL": VALID_SRC, "IMPORT_TARGET_URL": VALID_TGT,
               "CONFIRM_DISPOSABLE_TARGET": "yes"}
        cfg = P.resolve_config(env, "run1")
        src, _ = P.validate_schemes(cfg)
        P.source_identity(cfg, src, env)
    assert ei.value.code == P.ExitCode.GATE


def test_g2_accepts_disposable_loopback():
    ident = _run_source("mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_run1")
    assert ident.database == "twelve09_src_run1"


# --------------------------------------------------------------------------- #
# G3 target identity
# --------------------------------------------------------------------------- #
def _run_target(url, extra=None, run_id="run1", source_url=None):
    cfg = P.resolve_config(
        _config_env(source_url=source_url or VALID_SRC, target_url=url, extra=extra),
        run_id,
    )
    src, tgt = P.validate_schemes(cfg)
    P.source_identity(cfg, src, {**(ATT), **(extra or {})})
    return P.target_identity(cfg, tgt, {**(ATT), **(extra or {})})


@pytest.mark.parametrize("url", [
    "postgresql+psycopg2://u:p@db.supabase.co:5432/twelve09_rehearsal",   # prod host
    "postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_production",       # prod db
    "postgresql+psycopg2://u:p@127.0.0.1:5432/some_other_db",            # bad pattern
    "postgresql+psycopg2://u:p@10.0.0.5:5432/twelve09_rehearsal",        # non-loopback
])
def test_g3_refuses_unsafe_targets(url):
    with pytest.raises(P.Fail) as ei:
        _run_target(url)
    assert ei.value.code == P.ExitCode.GATE


def test_g3_requires_confirmation():
    with pytest.raises(P.Fail) as ei:
        env = {"IMPORT_SOURCE_URL": VALID_SRC, "IMPORT_TARGET_URL": VALID_TGT,
               "SOURCE_IS_DISPOSABLE_MYSQL": "1"}
        cfg = P.resolve_config(env, "run1")
        src, tgt = P.validate_schemes(cfg)
        P.source_identity(cfg, src, env)
        P.target_identity(cfg, tgt, env)
    assert ei.value.code == P.ExitCode.GATE


def test_g3_accepts_valid_rehearsal():
    # Uses the *derived* names so the documented convention and both gates are
    # exercised together (no gate is weakened to make this pass).
    ident = _run_target(
        "postgresql+psycopg2://u:p@127.0.0.1:5432/" + P.derived_target_db("reh1"),
        source_url="mysql+pymysql://root:x@127.0.0.1:3306/" + P.derived_source_db("reh1"),
        run_id="reh1",
    )
    assert ident.database == P.derived_target_db("reh1")


# --------------------------------------------------------------------------- #
# Run-id format + derived disposable database names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("run_id", ["run1", "reh1", "abc", "5e20261015a1b2", "a" * 32])
def test_valid_documented_run_ids(run_id):
    assert P.RUN_ID_RE.match(run_id)
    assert P.SOURCE_DB_RE.match(P.derived_source_db(run_id))
    assert P.TARGET_DB_RE.match(P.derived_target_db(run_id))


@pytest.mark.parametrize("run_id", [
    "",                 # empty / missing
    "ab",              # too short (<3)
    "a" * 33,          # too long (>32)
    "RUN1",            # uppercase
    "5e-2026-10",      # hyphenated
    "5e_2026_10",      # underscored
    "5e.2026",         # dotted
    "reh 1",           # whitespace
    "-reh1",           # leading punctuation
    "../etc/passwd",   # path traversal attempt
    "ünf gültig",      # non-ascii
])
def test_invalid_run_ids_rejected(run_id):
    assert not P.RUN_ID_RE.match(run_id)


def test_resolve_config_rejects_invalid_run_id():
    with pytest.raises(P.Fail) as ei:
        P.resolve_config(_config_env(), "RUN-1")
    assert ei.value.code == P.ExitCode.USAGE


def test_derived_database_names_for_a_valid_id():
    cfg = P.resolve_config(
        _config_env(source_url="mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_5e20261015a1b2",
                    target_url="postgresql+psycopg2://u:p@127.0.0.1:5432/twelve09_rehearsal_5e20261015a1b2"),
        "5e20261015a1b2",
    )
    assert cfg.expected_source_db == "twelve09_src_5e20261015a1b2"
    assert cfg.expected_target_db == "twelve09_rehearsal_5e20261015a1b2"
    # ...and every gate accepts the derived names (no gate is weakened to do so).
    src, tgt = P.validate_schemes(cfg)
    assert P.source_identity(cfg, src, ATT).database == cfg.expected_source_db
    assert P.target_identity(cfg, tgt, ATT).database == cfg.expected_target_db


# --------------------------------------------------------------------------- #
# Value mapping
# --------------------------------------------------------------------------- #
def test_map_decimal_ok():
    spec = P.TABLES["payments"]
    assert P.map_decimal("payments", "amount", Decimal("1234.56"), spec) == Decimal("1234.56")
    assert P.map_decimal("payments", "amount", 12, spec) == Decimal("12")


def test_map_decimal_refuses_rounding():
    spec = P.TABLES["payments"]
    with pytest.raises(P.Fail):
        P.map_decimal("payments", "amount", Decimal("1.234"), spec)


def test_map_decimal_overflow():
    spec = P.TABLES["payments"]
    with pytest.raises(P.Fail):
        P.map_decimal("payments", "amount", Decimal("100000000.00"), spec)


def test_map_bool_domain():
    assert P.map_bool("users", "is_active", 1) is True
    assert P.map_bool("users", "is_active", 0) is False
    assert P.map_bool("users", "is_active", True) is True
    with pytest.raises(P.Fail):
        P.map_bool("users", "is_active", 2)


def test_map_enum_case_and_legacy():
    assert P.map_enum("orders", "status", "PENDING") == "PENDING"
    assert P.map_enum("orders", "status", "READY_FOR_PICKUP") == "READY_FOR_PICKUP"
    with pytest.raises(P.Fail):
        P.map_enum("orders", "status", "pending")          # case mismatch
    with pytest.raises(P.Fail):
        P.map_enum("orders", "status", "SHIPPED")          # legacy label


def test_map_json_shapes():
    assert P.map_json("users", "permissions", '["MANAGE_ORDERS"]') == ["MANAGE_ORDERS"]
    assert P.map_json("payments", "payment_metadata", '{"k": 1}') == {"k": 1}
    with pytest.raises(P.Fail):
        P.map_json("users", "permissions", '{"k": 1}')      # wrong shape
    with pytest.raises(P.Fail):
        P.map_json("payments", "payment_metadata", "not-json")


def test_map_datetime_outcome_a():
    naive = datetime(2026, 8, 14, 10, 0, 0)
    assert P.map_datetime("orders", "created_at", naive) == naive
    with pytest.raises(P.Fail):
        P.map_datetime("orders", "created_at", naive.replace(tzinfo=timezone.utc))


def test_map_row_success_and_not_null():
    row = {
        "id": 1, "fee_amount": Decimal("5.00"), "is_active": 1,
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    mapped = P.map_row("delivery_config", row)
    assert mapped["is_active"] is True and mapped["fee_amount"] == Decimal("5.00")

    bad = dict(row); bad["updated_at"] = None  # NOT NULL column
    with pytest.raises(P.MappingError) as ei:
        P.map_row("delivery_config", bad)
    err = ei.value
    assert err.table == "delivery_config" and err.row_id == 1 and err.col == "updated_at"


def test_map_row_nullable_decimal_cost_price():
    """products.cost_price is nullable: NULL must map to None (Phase 5D.1 C1)."""
    mapped = P.map_row("products", _product_row(cost_price=None))
    assert mapped["cost_price"] is None
    assert mapped["price"] == Decimal("10.00")


def test_map_row_nullable_json_payment_metadata():
    """payments.payment_metadata is nullable: NULL must map to None (5D.1 C2)."""
    mapped = P.map_row("payments", _payment_row(payment_metadata=None))
    assert mapped["payment_metadata"] is None
    assert mapped["amount"] == Decimal("150.00")


def test_map_row_nullable_metadata_and_cost_price_roundtrip():
    """Both nullable columns coexist in a full pass over both tables."""
    prod = P.map_row("products", _product_row(cost_price=None))
    pay = P.map_row("payments", _payment_row(payment_metadata=None))
    assert prod == P.map_row("products", _product_row(cost_price=None))
    assert pay["status"] == "SUCCESS"


@pytest.mark.parametrize("col", [
    "name", "email", "password_hash", "role", "permissions", "is_active",
    "created_at", "updated_at",
])
def test_map_row_still_refuses_null_in_required_column(col):
    """NOT NULL enforcement is unchanged: a NULL in a required column fails."""
    row = {
        "id": 3, "name": "Ada", "email": "a@example.invalid",
        "password_hash": "x", "role": "ADMIN",
        "permissions": ["MANAGE_ORDERS"], "is_active": True,
        "created_at": datetime(2026, 8, 14, 10, 0, 0),
        "updated_at": datetime(2026, 8, 14, 10, 0, 0),
    }
    row[col] = None
    with pytest.raises(P.MappingError) as ei:
        P.map_row("users", row)
    assert ei.value.col == col and ei.value.row_id == 3
    assert ei.value.reason == "NOT NULL value missing"


def test_map_row_nullable_fk_and_datetime_still_map_to_none():
    """orders.address_id and payments.paid_at are already-nullable columns."""
    assert P.map_row("orders", _order_row(address_id=None))["address_id"] is None
    assert P.map_row("payments", _payment_row(paid_at=None))["paid_at"] is None


def test_map_value_short_circuits_before_typed_conversion():
    """map_value returns None directly, never delegating None to a converter."""
    assert P.map_value("products", "cost_price", None) is None
    assert P.map_value("payments", "payment_metadata", None) is None
    with pytest.raises(P.Fail):
        P.map_decimal("products", "cost_price", None, P.TABLES["products"])
    with pytest.raises(P.Fail):
        P.map_json("payments", "payment_metadata", None)


def test_map_row_enum_error_is_sanitized():
    row = _product_row(id=5, cost_price=None)
    # The nullable product row is now actually run through the mapper:
    assert P.map_row("products", row)["cost_price"] is None
    # products has no enum col; use orders to test enum sanitation separately:
    order = {"id": 9, "user_id": 1, "address_id": 1, "total_amount": Decimal("1.00"),
             "status": "SHIPPED", "fulfillment_method": "STORE_DELIVERY",
             "delivery_fee": Decimal("0.00"), "created_at": datetime(2026, 8, 14), "updated_at": datetime(2026, 8, 14)}
    with pytest.raises(P.MappingError) as ei:
        P.map_row("orders", order)
    assert ei.value.col == "status" and ei.value.row_id == 9
    # MappingError carries only table/row_id/col/reason (no emails/addresses/metadata)
    assert not any(hasattr(ei.value, a) for a in ("email", "address", "payment_metadata"))


# --------------------------------------------------------------------------- #
# Sequences
# --------------------------------------------------------------------------- #
def test_sequence_next_value_empty_and_nonempty():
    assert P.compute_next_value(None) == 1     # empty table -> first id = 1
    assert P.compute_next_value(43) == 44
    assert P.compute_next_value(0) == 1


def test_setval_and_sequence_sql_are_parameterised():
    sql, params = P.sequence_sql("orders")
    assert "%s" in sql and params == ("orders", "id")
    assert P.setval_sql().count("%s") == 2


# --------------------------------------------------------------------------- #
# Reconciliation helpers
# --------------------------------------------------------------------------- #
def test_diff_counts():
    assert P.diff_counts({"users": 43, "categories": 4}, {"users": 43, "categories": 5}) == [
        "categories: source=4 target=5"
    ]


def test_enum_domain_violations_reports_ids_only():
    rows = [(1, "PENDING"), (2, "SHIPPED")]
    out = P.enum_domain_violations("orders", "status", rows)
    assert out == ["orders.status: id=2 not in orderstatus"]


def test_order_amount_rule():
    assert P.check_order_amounts(1, Decimal("150.00"), Decimal("20.00"), [Decimal("80.00"), Decimal("50.00")])
    assert not P.check_order_amounts(2, Decimal("150.00"), Decimal("20.00"), [Decimal("80.00")])


def test_stock_cost_checks():
    assert P.check_stock_cost(3, None) == (True, True)
    assert P.check_stock_cost(-1, Decimal("2.00")) == (False, True)
    assert P.check_stock_cost(3, Decimal("-1.00")) == (True, False)
    assert P.check_stock_cost(3, Decimal("0")) == (True, True)
    assert P.check_stock_cost(3, 0) == (True, True)


@pytest.mark.parametrize("bad_cost", ["not-a-number", "", "   ", "1,234.56", "NaN", object()])
def test_stock_cost_invalid_input_is_a_controlled_failure(bad_cost):
    """Unparseable cost must yield a reconciliation failure, never an
    unhandled ``decimal.InvalidOperation`` (Phase 5D.1 Medium)."""
    assert P.check_stock_cost(3, bad_cost) == (True, False)


# --------------------------------------------------------------------------- #
# main() failure paths (no database touched)
# --------------------------------------------------------------------------- #
def test_main_aborts_without_config(monkeypatch):
    for k in ("IMPORT_SOURCE_URL", "IMPORT_TARGET_URL", "SOURCE_IS_DISPOSABLE_MYSQL",
              "CONFIRM_DISPOSABLE_TARGET"):
        monkeypatch.delenv(k, raising=False)
    assert P.main(["--run-id", "run1", "--dry-run"]) != P.ExitCode.OK


def test_main_valid_config_still_refuses_before_db(monkeypatch, capsys):
    monkeypatch.setenv("IMPORT_SOURCE_URL", VALID_SRC)
    monkeypatch.setenv("IMPORT_TARGET_URL", VALID_TGT)
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    code = P.main(["--run-id", "run1", "--apply"])
    # Gates pass; DB execution is gated behind Phase 5E -> non-zero, no connection.
    assert code == P.ExitCode.GATE
    out = capsys.readouterr().err
    assert "secret" not in out  # credentials never reach logs


def test_main_rejects_contradictory_flags(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL", VALID_SRC)
    monkeypatch.setenv("IMPORT_TARGET_URL", VALID_TGT)
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    assert P.main(["--run-id", "run1", "--validate-only", "--truncate-target"]) == P.ExitCode.USAGE


# --------------------------------------------------------------------------- #
# Execution-disabled safeguards (Phase 5D.2 / Task 4)
# --------------------------------------------------------------------------- #
def test_execution_is_disabled_in_this_build():
    assert P.EXECUTION_ENABLED is False


def test_dispatch_path_is_not_implemented_and_raises():
    # Even if it WERE reached, it fails closed.
    with pytest.raises(P.Fail) as ei:
        P._dispatch_db_mode(None, None, None, None)
    assert ei.value.code == P.ExitCode.GATE


@pytest.mark.parametrize(
    "mode",
    [["--apply"], ["--dry-run"], ["--validate-only"], ["--reset-sequences"],
     ["--apply", "--truncate-target"]],
)
def test_main_cannot_reach_execution_while_disabled(monkeypatch, mode):
    """Every CLI mode must stop at the execution gate and never touch a DB.

    ``_dispatch_db_mode`` and ``make_engine`` are monkeypatched to explode, so
    any attempt to reach database execution fails the test loudly.
    """
    monkeypatch.setenv("IMPORT_SOURCE_URL", VALID_SRC)
    monkeypatch.setenv("IMPORT_TARGET_URL", VALID_TGT)
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setenv("IMPORT_ALLOW_REMOTE_SOURCE", "0")

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("database execution was reachable while disabled")

    monkeypatch.setattr(P, "_dispatch_db_mode", _boom)
    monkeypatch.setattr(P, "make_engine", _boom)

    code = P.main(["--run-id", "run1", *mode])
    assert code == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_no_environment_variable_can_enable_execution(monkeypatch):
    """No CLI flag and no env var flips the guard."""
    monkeypatch.setenv("IMPORT_SOURCE_URL", VALID_SRC)
    monkeypatch.setenv("IMPORT_TARGET_URL", VALID_TGT)
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    for sneaky in ("IMPORT_EXECUTION_ENABLED", "IMPORT_EXECUTE", "PG_IMPORT_ENABLED",
                   "EXECUTION_ENABLED", "PG_IMPORT_EXECUTE", "IMPORT_ALLOW_WRITES"):
        monkeypatch.setenv(sneaky, "1")
    assert P.main(["--run-id", "run1", "--apply"]) == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_import_does_not_eagerly_load_db_drivers():
    """Importing ``tools.pg_import`` must not pull in a DB driver/ORM.

    Runs in a *fresh subprocess* so the assertion is not invalidated by another
    test module having already imported SQLAlchemy in this interpreter.
    """
    code = (
        "import sys;"
        "import tools.pg_import as p;"
        "assert p.EXECUTION_ENABLED is False;"
        "loaded = sorted(m for m in ('sqlalchemy','pymysql','psycopg2') "
        "if m in sys.modules);"
        "print('|'.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=str(_BACKEND_DIR),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "importing tools.pg_import eagerly loaded a database module"
    )


# --------------------------------------------------------------------------- #
# Malformed URL handling (Phase 5D.2 / Task 5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "postgresql+psycopg2://u:p@127.0.0.1:not-a-port/twelve09_rehearsal",
    "postgresql+psycopg2://u:p@127.0.0.1:99999/twelve09_rehearsal",
    "postgresql+psycopg2://u:p@[::1/twelve09_rehearsal",
])
def test_parse_url_malformed_port_or_host_is_sanitized_config_error(url):
    """A bad port must be a Fail(CONFIG), not an uncaught ValueError."""
    with pytest.raises(P.Fail) as ei:
        P.parse_url(url)
    assert ei.value.code == P.ExitCode.CONFIG
    assert "secret" not in ei.value.message
    # sanitize_url must not blow up either (it is used for logging).
    with pytest.raises(P.Fail):
        P.sanitize_url(url)


def test_parse_url_missing_host_is_config_error():
    """A URL with no host parses cleanly and is refused later, not crash."""
    url = "postgresql+psycopg2://u:p@:5432/twelve09_rehearsal"
    parsed = P.parse_url(url)
    assert parsed.host is None
    cfg = P.resolve_config(
        {"IMPORT_SOURCE_URL": VALID_SRC, "IMPORT_TARGET_URL": url,
         "SOURCE_IS_DISPOSABLE_MYSQL": "1", "CONFIRM_DISPOSABLE_TARGET": "yes"},
        "run1",
    )
    with pytest.raises(P.Fail) as ei:
        P.validate_schemes(cfg)
    assert ei.value.code == P.ExitCode.CONFIG


def test_main_with_malformed_target_url_returns_config_code(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL", VALID_SRC)
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:not-a-port/twelve09_rehearsal")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setattr(P, "_dispatch_db_mode",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    assert P.main(["--run-id", "run1", "--dry-run"]) == P.ExitCode.CONFIG
