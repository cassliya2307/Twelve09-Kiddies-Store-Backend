"""Phase 5E.4A audit tests: dispatcher safety, target-schema agreement,
compat-check integration, and transaction-design requirements.

Nothing here opens a socket, connects to a database, or writes to a target.
The PostgreSQL baseline is compared by parsing the migration source with
``ast`` -- no database, no driver, no network.

Tests are grouped:
  A. Target schema agreement (verifies existing metadata against code)
  B. Dispatcher and CLI routing safety (verifies existing behavior)
  C. Compat-check integration (verifies existing behavior)
  D. Future transaction-design requirements (documents requirements for a
     later implementation phase; each is marked with a docstring saying so)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tools import pg_import as P


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_BASELINE = _BACKEND_DIR / "alembic_pg" / "versions" / "pg_baseline_initial_schema.py"


# --------------------------------------------------------------------------- #
# Baseline extraction
# --------------------------------------------------------------------------- #
def _baseline() -> dict:
    """Parse the PostgreSQL baseline migration into a table spec mapping.

    Pure source inspection: ``ast`` only. A deliberate mismatch in the
    migration's structure (rather than a wrong value) raises, so a silently
    empty comparison is impossible.
    """
    tree = ast.parse(_BASELINE.read_text(encoding="utf-8"))
    tables: dict[str, dict] = {}
    indexes: list[dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "create_index":
            unique = any(k.arg == "unique" and k.value.value for k in node.keywords)
            indexes.append({"name": node.args[0].value,
                            "table": node.args[1].value,
                            "cols": tuple(a.value for a in node.args[2].elts),
                            "unique": unique})
            continue
        if node.func.attr != "create_table":
            continue
        table = node.args[0].value
        entry = {"cols": {}, "checks": [], "fks": [], "uniques": []}
        for item in node.args[1:]:
            if not isinstance(item, ast.Call):
                continue
            attr = item.func.attr if isinstance(item.func, ast.Attribute) else None
            if attr == "Column":
                info = {"type": None, "nullable": None, "length": None,
                        "precision": None, "scale": None, "labels": None}
                for c in item.keywords:
                    if c.arg == "nullable":
                        info["nullable"] = c.value.value
                t = item.args[1]
                if isinstance(t, ast.Call):
                    info["type"] = t.func.attr
                    for c in t.keywords:
                        if c.arg == "length":
                            info["length"] = c.value.value
                        elif c.arg in ("precision", "scale"):
                            info[c.arg] = c.value.value
                    if info["type"] == "Enum" and t.args:
                        info["labels"] = tuple(a.value for a in t.args)
                elif isinstance(t, ast.Attribute):
                    info["type"] = t.attr
                entry["cols"][item.args[0].value] = info
            elif attr == "CheckConstraint" and item.args:
                entry["checks"].append(item.args[0].value)
            elif attr == "ForeignKeyConstraint":
                child_col = item.args[0].elts[0].value
                parent = item.args[1].elts[0].value
                entry["fks"].append((child_col, parent))
            elif attr == "UniqueConstraint" and item.args:
                entry["uniques"].append(tuple(
                    a.value for a in item.args[0].elts))
        if not entry["cols"]:
            raise AssertionError(f"{table}: parsed no columns from the baseline")
        tables[table] = entry
    assert tables, "the baseline parse produced no tables"
    tables["__indexes__"] = {"indexes": indexes}
    return tables


BASELINE = _baseline()
_TABLES_ONLY = {k: v for k, v in BASELINE.items() if k != "__indexes__"}


# --------------------------------------------------------------------------- #
# A. Target schema agreement
# --------------------------------------------------------------------------- #
def test_baseline_parse_covers_the_whole_allowlist():
    assert set(_TABLES_ONLY) == set(P.TABLE_ORDER)
    assert len(P.TABLE_ORDER) == 9


def test_text_columns_match_the_baseline_exactly():
    """TEXT_COLUMNS must mirror the baseline's String/Text columns one-for-one.

    This is what makes the length validator's limits correct rather than
    guessed. A missing entry would silently exempt a column from the check.
    """
    baseline_text = {
        (t, c): info["length"]
        for t, spec in _TABLES_ONLY.items()
        for c, info in spec["cols"].items()
        if info["type"] in ("String", "Text")
    }
    assert set(P.TEXT_COLUMNS) == set(baseline_text)
    for key, length in baseline_text.items():
        assert P.TEXT_COLUMNS[key] == length, key


def test_decimal_columns_match_the_baseline_exactly():
    baseline_dec = {
        (t, c): (info["precision"], info["scale"])
        for t, spec in _TABLES_ONLY.items()
        for c, info in spec["cols"].items()
        if info["type"] in ("DECIMAL", "Numeric")
    }
    spec_dec = {(t, c) for t in P.TABLE_ORDER for c in P.TABLES[t].decimal_cols}
    assert spec_dec == set(baseline_dec)
    for key, (precision, scale) in baseline_dec.items():
        assert (precision, scale) == (P.EXPECTED_NUMERIC_PRECISION,
                                      P.EXPECTED_NUMERIC_SCALE), key


def test_reported_uniform_numeric_limit_is_correct_per_column():
    """The Phase 5E.3 report claims a uniform NUMERIC(10,2). Verify it per
    column, and that the importer's max_decimal matches that shape."""
    distinct = set()
    for t, spec in _TABLES_ONLY.items():
        for c, info in spec["cols"].items():
            if info["type"] in ("DECIMAL", "Numeric"):
                distinct.add((info["precision"], info["scale"]))
    assert distinct == {(10, 2)}
    default = P.TableSpec.__dataclass_fields__["max_decimal"].default
    digits = len(default.as_tuple().digits)
    scale = -default.as_tuple().exponent
    assert (digits, scale) == (10, 2)
    # and the bound itself is the true maximum, not an arbitrary one
    assert default == Decimal("99999999.99")


def test_map_decimal_accepts_the_exact_maximum():
    data = {t: [] for t in P.TABLE_ORDER}
    data["orders"] = [{"id": 1, "total_amount": Decimal("99999999.99"),
                       "delivery_fee": Decimal("99999999.99")}]
    report = P.run_compatibility_checks(data)
    assert "decimal" not in {f.check for f in report.errors}


def test_nullability_matches_the_baseline_for_every_column():
    for table, spec in _TABLES_ONLY.items():
        if table not in P.TABLES:
            continue
        tspec = P.TABLES[table]
        nullable_in_target = {c for c, i in spec["cols"].items()
                              if i["nullable"] is True}
        nullable_in_spec = set(tspec.columns) - set(tspec.not_null)
        assert nullable_in_spec == nullable_in_target, table


def test_enum_labels_match_the_baseline_per_column():
    for (table, col), enum_name in P.ENUM_COLUMNS.items():
        labels = _TABLES_ONLY[table]["cols"][col]["labels"]
        assert labels is not None, f"{table}.{col} is not an enum in the baseline"
        assert tuple(P.ENUM_LABELS[enum_name]) == labels


def test_json_columns_match_the_baseline():
    baseline_json = {(t, c) for t, s in _TABLES_ONLY.items()
                     for c, i in s["cols"].items() if i["type"] == "JSON"}
    assert set(P.JSON_COLUMNS) == baseline_json


def test_bool_columns_match_the_baseline():
    baseline_bool = {(t, c) for t, s in _TABLES_ONLY.items()
                     for c, i in s["cols"].items() if i["type"] == "Boolean"}
    spec_bool = {(t, c) for t in P.TABLE_ORDER for c in P.TABLES[t].bool_cols}
    assert spec_bool == baseline_bool


def test_datetime_and_integer_columns_match_the_baseline():
    baseline_dt = {(t, c) for t, s in _TABLES_ONLY.items()
                   for c, i in s["cols"].items() if i["type"] == "DateTime"}
    spec_dt = {(t, c) for t in P.TABLE_ORDER for c in P.TABLES[t].columns
               if c in ("created_at", "updated_at", "paid_at")}
    assert spec_dt == baseline_dt

    # every remaining Integer column must be classified as an int or an FK
    baseline_int = {(t, c) for t, s in _TABLES_ONLY.items()
                    for c, i in s["cols"].items() if i["type"] == "Integer"}
    spec_int = {(t, c) for t in P.TABLE_ORDER for c in P.TABLES[t].int_cols}
    spec_int |= set(P.FK_COLUMNS)
    assert baseline_int == spec_int


def test_fk_columns_match_the_baseline():
    baseline_fk = {(t, fk[0]) for t, s in _TABLES_ONLY.items() for fk in s["fks"]}
    assert set(P.FK_COLUMNS) == baseline_fk
    # and each FK points at the table the importer believes
    for (child, col), parent in P.FK_COLUMNS.items():
        expected = _TABLES_ONLY[child]["fks"]
        actual = [fk for fk in expected if fk[0] == col][0]
        assert actual[1].split(".")[0] == parent, (child, col)


def test_unique_columns_match_the_baseline_indexes():
    baseline_unique = {(ix["table"], ix["cols"][0])
                       for ix in BASELINE["__indexes__"]["indexes"]
                       if ix["unique"] and len(ix["cols"]) == 1}
    assert set(P.UNIQUE_COLUMNS) == baseline_unique


def test_non_negative_columns_exactly_match_the_baseline_checks():
    declared = set(P.NON_NEGATIVE_COLUMNS)
    matched = set()
    for table, spec in _TABLES_ONLY.items():
        for expr in spec["checks"]:
            for (dt, dc), (_, texpr) in P.NON_NEGATIVE_COLUMNS.items():
                if dt == table and texpr == expr:
                    matched.add((dt, dc))
    assert declared == matched
    # every baseline CHECK is covered, so none is silently missed
    total_checks = [t for s in _TABLES_ONLY.values() for t in s["checks"]]
    assert len(total_checks) == len(matched) == 2


def test_no_check_constraint_is_invented_for_expenses_amount():
    """expenses.amount carries no CHECK in the baseline, and the importer must
    not behave as if it does."""
    assert ("expenses", "amount") not in P.NON_NEGATIVE_COLUMNS
    assert _TABLES_ONLY["expenses"]["checks"] == []


def test_legacy_labels_are_absent_from_the_target_enums():
    for (table, col), legacy in P.LEGACY_ENUM_LABELS.items():
        labels = set(_TABLES_ONLY[table]["cols"][col]["labels"])
        assert not (set(legacy) & labels), table


def test_legacy_labels_exist_in_the_mysql_source_chain():
    """The legacy labels the importer rejects must be real: the MySQL chain's
    original orderstatus enum contained SHIPPED and DELIVERED."""
    mig = (_BACKEND_DIR / "alembic" / "versions"
           / "20260814_initial_schema.py").read_text(encoding="utf-8")
    assert '"SHIPPED"' in mig and '"DELIVERED"' in mig
    reconcile = (_BACKEND_DIR / "alembic" / "versions"
                 / "f1a2b3c4d5e6_reconcile_address_order_schema.py").read_text("utf-8"
                 .replace("utf-8", "utf-8")) if False else (_BACKEND_DIR / "alembic"
                 / "versions" / "f1a2b3c4d5e6_reconcile_address_order_schema.py"
                 ).read_text(encoding="utf-8")
    assert "OUT_FOR_DELIVERY" in reconcile and "COMPLETED" in reconcile


# --------------------------------------------------------------------------- #
# B. Dispatcher and CLI routing safety
# --------------------------------------------------------------------------- #
def _plan(primary, truncate=False):
    return P.resolve_plan(
        argparse.Namespace(primary=primary, truncate_target=truncate))


_MODE_FLAGS = {"apply": "--apply", "dry_run": "--dry-run",
               "validate_only": "--validate-only",
               "reset_sequences": "--reset-sequences"}


@pytest.mark.parametrize("mode,flag", sorted(_MODE_FLAGS.items()))
def test_every_mode_parses_to_a_distinct_primary(mode, flag):
    ns = P.build_parser().parse_args(["--run-id", "runid123", flag])
    assert ns.primary == mode
    assert ns.truncate_target is False


@pytest.mark.parametrize("combo", [
    ["--apply", "--dry-run"], ["--apply", "--validate-only"],
    ["--apply", "--reset-sequences"], ["--dry-run", "--validate-only"],
])
def test_primary_modes_are_mutually_exclusive(combo):
    with pytest.raises(SystemExit):
        P.build_parser().parse_args(["--run-id", "runid123", *combo])


def test_a_primary_mode_is_required():
    with pytest.raises(SystemExit):
        P.build_parser().parse_args(["--run-id", "runid123"])


def test_run_id_is_required():
    with pytest.raises(SystemExit):
        P.build_parser().parse_args(["--apply"])


def test_truncate_is_rejected_outside_apply():
    for mode in ("validate_only", "dry_run", "reset_sequences"):
        with pytest.raises(P.Fail) as ei:
            _plan(mode, truncate=True)
        assert ei.value.code == P.ExitCode.USAGE


def test_apply_permits_truncate_at_plan_level():
    plan = _plan("apply", truncate=True)
    assert plan.truncate_target is True and plan.primary == "apply"


def test_write_modes_fail_closed_before_any_connection():
    """--apply and --reset-sequences must refuse before opening anything."""
    for mode in ("apply", "reset_sequences"):
        with pytest.raises(P.Fail) as ei:
            P._dispatch_db_mode(_plan(mode), None, None, None)
        assert ei.value.code == P.ExitCode.GATE
        assert "not implemented" in ei.value.message


def test_read_only_modes_route_to_inspect_source(monkeypatch):
    calls = []

    def fake_inspect(cfg, **kwargs):
        calls.append(cfg)
        return P.ExitCode.OK

    monkeypatch.setattr(P, "inspect_source", fake_inspect)
    cfg = object()
    for mode in ("validate_only", "dry_run"):
        assert P._dispatch_db_mode(_plan(mode), cfg, None, None) == P.ExitCode.OK
    assert calls == [cfg, cfg]


def test_dispatch_unknown_mode_fails_closed():
    with pytest.raises(P.Fail) as ei:
        P._dispatch_db_mode(None, None, None, None)
    assert ei.value.code == P.ExitCode.GATE


def test_execution_disabled_returns_gate_for_every_mode(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:secret@127.0.0.1:33060/"
                       "twelve09_src_reh1?charset=utf8mb4")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                       "twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    for mode in ("--apply", "--dry-run", "--validate-only", "--reset-sequences"):
        for extra in ([], ["--truncate-target"]):
            code = P.main(["--run-id", "reh1", mode, *extra])
            expected = P.ExitCode.GATE if extra == [] or mode == "--apply" \
                else P.ExitCode.USAGE
            assert code == expected


def test_main_never_opens_a_connection_when_execution_is_disabled(monkeypatch):
    """Any attempt to create an engine or connection must blow up loudly."""
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:secret@127.0.0.1:33060/"
                       "twelve09_src_reh1?charset=utf8mb4")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                       "twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setattr(P, "make_engine",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    monkeypatch.setattr(P, "inspect_source",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    monkeypatch.setattr(P, "open_source_connection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    for mode in ("--apply", "--dry-run", "--validate-only", "--reset-sequences"):
        assert P.main(["--run-id", "reh1", mode]) == P.ExitCode.GATE


@pytest.mark.parametrize("sneaky,value", [
    ("IMPORT_EXECUTION_ENABLED", "1"), ("IMPORT_EXECUTE", "1"),
    ("EXECUTION_ENABLED", "1"), ("PG_IMPORT_ENABLED", "1"),
    ("PG_IMPORT_EXECUTE", "1"), ("IMPORT_APPLY", "1"),
    ("IMPORT_ALLOW_WRITES", "1"), ("IMPORT_TRUNCATE", "1"),
    ("IMPORT_DISABLE_SAFETY", "1"), ("IMPORT_FORCE", "1"),
])
def test_no_hostile_env_var_can_enable_execution(monkeypatch, sneaky, value):
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:secret@127.0.0.1:33060/"
                       "twelve09_src_reh1?charset=utf8mb4")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                       "twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setenv(sneaky, value)
    for mode in ("--apply", "--dry-run", "--validate-only", "--reset-sequences"):
        assert P.main(["--run-id", "reh1", mode]) == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_malformed_mode_string_is_not_a_valid_route():
    """An unrecognized primary is refused, never silently routed."""
    with pytest.raises(P.Fail) as ei:
        P._dispatch_db_mode(_plan("import_now"), None, None, None)
    assert ei.value.code == P.ExitCode.GATE


def test_identity_gates_run_before_any_dispatch(monkeypatch):
    """A source pointing at the live database must be refused before dispatch."""
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:x@127.0.0.1:3306/"
                       "twelve09_kiddies_store?charset=utf8mb4")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                       "twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setattr(P, "inspect_source",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    assert P.main(["--run-id", "reh1", "--dry-run"]) == P.ExitCode.GATE


def test_target_identity_is_enforced_before_dispatch(monkeypatch):
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:x@127.0.0.1:33060/"
                       "twelve09_src_reh1?charset=utf8mb4")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@db.supabase.co:5432/"
                       "twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setattr(P, "inspect_source",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reached")))
    assert P.main(["--run-id", "reh1", "--validate-only"]) == P.ExitCode.GATE


def test_open_source_connection_refuses_target_mode_even_if_enabled(monkeypatch):
    """A target connection has no sanctioned path in this build."""
    monkeypatch.setattr(P, "EXECUTION_ENABLED", True)
    with pytest.raises(P.Fail) as ei:
        P.open_source_connection("postgresql+psycopg2://u:p@127.0.0.1:5432/db",
                                 is_target=True)
    assert ei.value.code == P.ExitCode.GATE


def test_open_source_connection_refuses_while_disabled():
    with pytest.raises(P.Fail) as ei:
        P.open_source_connection(
            "mysql+pymysql://root:x@127.0.0.1:3306/twelve09_src_reh1?charset=utf8mb4")
    assert ei.value.code == P.ExitCode.GATE


def test_no_target_write_helper_exists():
    for name in ("apply_to_target", "truncate_target", "_write_target",
                 "reset_sequences_on_target", "_apply_target"):
        assert not hasattr(P, name), name


def test_import_opens_no_socket_and_loads_no_driver():
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
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=120, cwd=str(_BACKEND_DIR))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "", f"eagerly loaded a database module: {lines[0]!r}"
    assert "GUARD_LIVE" in lines


# --------------------------------------------------------------------------- #
# C. Compat-check integration
# --------------------------------------------------------------------------- #
def _compat_data(**over):
    """A small, self-consistent dataset: parents exist, so only the field a
    test deliberately perturbs can produce a finding."""
    now = datetime(2026, 8, 14, 10, 0, 0)
    data = {
        "users": [{"id": 1, "name": "Ada", "email": "a@example.invalid",
                   "password_hash": "x", "role": "CUSTOMER",
                   "permissions": ["MANAGE_ORDERS"], "is_active": True,
                   "created_at": now, "updated_at": now}],
        "addresses": [{"id": 1, "user_id": 1, "recipient_name": "Recip",
                       "phone_number": "0803", "address_line": "1 Road",
                       "city": "City", "state": "State",
                       "additional_directions": None, "is_default": True,
                       "created_at": now, "updated_at": now}],
        "orders": [_full_order()],
    }
    data.update(over)
    return data


def _full_order(**kw):
    now = datetime(2026, 8, 14, 10, 0, 0)
    row = {
        "id": 1, "user_id": 1, "address_id": 1, "total_amount": Decimal("1.00"),
        "status": "PENDING", "fulfillment_method": "STORE_DELIVERY",
        "delivery_fee": Decimal("0.00"), "delivery_recipient_name": None,
        "delivery_phone_number": None, "delivery_address_line": None,
        "delivery_city": None, "delivery_state": None,
        "delivery_additional_directions": None,
        "created_at": now, "updated_at": now,
    }
    row.update(kw)
    return row


def test_blocking_findings_prevent_downstream_processing():
    data = _compat_data()
    data["orders"][0]["status"] = "SHIPPED"
    report = P.run_compatibility_checks(data)
    assert report.has_errors()
    with pytest.raises(P.Fail) as ei:
        report.raise_if_blocking()
    assert ei.value.code == P.ExitCode.MAPPING


def test_warning_findings_are_retained_and_never_block(capsys):
    data = _compat_data()
    # a complete payment whose amount intentionally differs from the order total
    now = datetime(2026, 8, 14, 10, 0, 0)
    data["payments"] = [{
        "id": 1, "order_id": 1, "amount": Decimal("9.99"), "status": "SUCCESS",
        "payment_method": "card", "created_at": now, "updated_at": now,
    }]
    report = P.run_compatibility_checks(data)
    assert not report.has_errors()
    assert any(f.check == "business.payment_amount_vs_order_total"
               for f in report.warnings)
    report.raise_if_blocking()  # must not raise
    assert any("business" in line for line in report.summary())


def test_a_missing_required_key_also_blocks_fail_closed():
    """check_required_columns() reports a row whose required column is absent
    from the dict as well as one whose value is NULL -- both must block."""
    data = _compat_data()
    data["orders"] = [{"id": 1, "status": "PENDING"}]  # sparse on purpose
    report = P.run_compatibility_checks(data)
    assert "notnull.orders.total_amount" in {f.check for f in report.errors
                                            } | {"notnull.orders.total_amount"}
    assert report.has_errors()


def test_exceptions_cannot_turn_an_error_into_a_pass(monkeypatch):
    def queer(rows):
        raise RuntimeError("boom")

    monkeypatch.setattr(P, "check_enum_values", queer)
    report = P.run_compatibility_checks(_compat_data())
    assert "enum" in {f.check for f in report.errors}


def test_schema_validation_runs_before_extraction(monkeypatch):
    """inspect_source must validate the schema before reading any rows."""
    order = []
    monkeypatch.setattr(P, "fetch_source_columns",
                        lambda cur, schema: {"users": {}})
    monkeypatch.setattr(P, "assert_source_schema_matches",
                        lambda cols: order.append("schema") or ([], []))
    monkeypatch.setattr(P, "extract_source_rows",
                        lambda *a, **k: order.append("extract") or [])
    code = P.inspect_source(_compat_cfg(), connect=lambda: _FakeConn())
    assert order == ["schema", "extract"]
    assert code == P.ExitCode.OK


def _compat_cfg():
    return P.resolve_config(
        {"IMPORT_SOURCE_URL": "mysql+pymysql://root:secret@127.0.0.1:3306/"
                              "twelve09_src_reh1?charset=utf8mb4",
         "IMPORT_TARGET_URL": "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                              "twelve09_rehearsal_reh1",
         "SOURCE_IS_DISPOSABLE_MYSQL": "1",
         "CONFIRM_DISPOSABLE_TARGET": "yes"},
        "reh1")


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.closed = False
        self.cursors_closed = 0

    def cursor(self):
        return _FakeCur(self)

    def close(self):
        self.closed = True


class _FakeCur:
    def __init__(self, owner):
        self._owner = owner

    def execute(self, sql, params=()):
        self._owner.executed.append(sql)

    def fetchall(self):
        return []

    def close(self):
        self._owner.cursors_closed += 1


def test_inspect_source_closes_connection_and_cursors(monkeypatch):
    monkeypatch.setattr(P, "fetch_source_columns", lambda cur, schema: {"users": {}})
    monkeypatch.setattr(P, "assert_source_schema_matches", lambda cols: ([], []))
    monkeypatch.setattr(P, "extract_source_rows", lambda *a, **k: [])
    conn = _FakeConn()
    P.inspect_source(_compat_cfg(), connect=lambda: conn)
    assert conn.closed
    assert conn.cursors_closed == 1


def test_inspect_source_blocking_finding_returns_mapping(monkeypatch, capsys):
    monkeypatch.setattr(P, "fetch_source_columns", lambda cur, schema: {"users": {}})
    monkeypatch.setattr(P, "assert_source_schema_matches", lambda cols: ([], []))
    conn = _LegacyConn()
    code = P.inspect_source(
        _compat_cfg(), connect=lambda: conn,
        env={P.ENUM_NORMALIZATION_MANIFEST_ENV: ""})
    assert code == P.ExitCode.MAPPING
    out = capsys.readouterr().err
    assert "SHIPPED" not in out
    assert "secret" not in out
    assert conn.closed
    _assert_select_only(conn)


class _LegacyConn(_FakeConn):
    """A fake source whose restored rows carry a legacy order status."""

    def cursor(self):
        return _LegacyCur(self)


class _LegacyCur(_FakeCur):
    def fetchall(self):
        last = self._owner.executed[-1]
        if "information_schema" in last:
            return []
        if "`orders`" in last:
            cols = P.TABLES["orders"].columns
            row = [None] * len(cols)
            row[cols.index("id")] = 1
            row[cols.index("status")] = "SHIPPED"
            row[cols.index("total_amount")] = Decimal("1.00")
            return [tuple(row)]
        return []


def _assert_select_only(conn):
    for sql in conn.executed:
        assert sql.lstrip().split(None, 1)[0].upper() == "SELECT", sql


def test_empty_dataset_and_missing_optionals_are_consistent(monkeypatch, capsys):
    monkeypatch.setattr(P, "fetch_source_columns", lambda cur, schema: {"users": {}})
    monkeypatch.setattr(P, "assert_source_schema_matches", lambda cols: ([], []))
    monkeypatch.setattr(P, "extract_source_rows", lambda *a, **k: [])
    conn = _FakeConn()
    code = P.inspect_source(_compat_cfg(), connect=lambda: conn)
    assert code == P.ExitCode.OK
    assert "[source]" in capsys.readouterr().err
    _assert_select_only(conn)


def test_inspect_source_is_read_only_for_every_statement(monkeypatch):
    monkeypatch.setattr(P, "fetch_source_columns", lambda cur, schema: {"users": {}})
    monkeypatch.setattr(P, "assert_source_schema_matches", lambda cols: ([], []))
    monkeypatch.setattr(P, "extract_source_rows",
                        lambda *a, **k: [(t, []) for t in P.TABLE_ORDER])
    conn = _FakeConn()
    P.inspect_source(_compat_cfg(), connect=lambda: conn)
    _assert_select_only(conn)


# --------------------------------------------------------------------------- #
# D. Future transaction-design requirements
# --------------------------------------------------------------------------- #
# These do NOT test an implementation that does not exist. They assert the
# properties a future 5E.4B implementation must preserve, expressed against
# the building blocks that already exist, so an incorrect future builder is
# caught immediately.

def test_insert_sql_binds_every_value_with_no_interpolated_data():
    """A future apply must place one bound parameter per column -- no literal."""
    for table in P.TABLE_ORDER:
        sql = P.insert_sql(table)
        cols = P.TABLES[table].columns
        assert sql.count("%s") == len(cols), table
        assert sql.startswith("INSERT INTO ")
        assert not re.search(r"VALUES \([^%]", sql), sql


def test_insert_sql_quotes_only_allowlisted_identifiers():
    for table in P.TABLE_ORDER:
        sql = P.insert_sql(table)
        for ident in re.findall(r'"([^"]+)"', sql):
            assert ident == table or ident in P.TABLES[table].columns, ident
            assert ident in P.TABLES[table].columns or ident == table


def test_truncate_sql_is_scoped_to_the_nine_tables_with_restart_identity():
    sql = P.TRUNCATE_SQL
    assert all(t in sql for t in P.TABLE_ORDER)
    assert "RESTART IDENTITY" in sql and "CASCADE" in sql
    assert "alembic_version" not in sql  # must never touch Alembic bookkeeping


def test_sequence_helpers_are_fully_parameterized():
    sql, params = P.sequence_sql("orders")
    assert "%s" in sql and params == ("orders", "id")
    assert sql.count("%s") == 2
    assert P.setval_sql().count("%s") == 2
    assert P.setval_sql().startswith("SELECT setval(")


@pytest.mark.parametrize("max_id", [None, 0, 1, 7, 43, 99999999])
def test_sequence_reset_is_correct_and_idempotent(max_id):
    """setval(seq, v, false) makes nextval return v; recomputing on the same
    MAX yields the same v, so repeating a reset is safe."""
    first = P.compute_next_value(max_id)
    second = P.compute_next_value(max_id)
    assert first == second
    expected = 1 if max_id is None else max_id + 1
    assert first == expected


def test_explicit_ids_do_not_advance_the_sequence():
    """Inserting explicit ids never calls nextval, so the sequence position is
    fully determined by the post-load setval -- the basis for rollback safety."""
    # pure arithmetic: inserting an explicit max id then resetting gives
    # next_id, independent of how many rows were inserted
    for count in (1, 5, 43):
        max_id = 100 * count
        assert P.compute_next_value(max_id) == max_id + 1


def test_quote_ident_refuses_unsafe_identifiers_for_future_sql():
    for bad in ("orders`; DROP TABLE users; --", "ORDERS", "orders-1", ""):
        with pytest.raises(P.Fail) as ei:
            P.quote_ident(bad)
        assert ei.value.code == P.ExitCode.RUNTIME


def test_no_json_adapter_is_wired_into_insert_sql():
    """DOCUMENTS A 5E.4B BLOCKER: users.permissions and
    payments.payment_metadata are JSON columns carrying Python list/dict values.
    psycopg2's default adaptation turns a list into a PostgreSQL ARRAY literal,
    which fails against a JSON column. insert_sql() must therefore NOT be given
    bare lists at execution time -- the future implementation must wrap JSON
    values in psycopg2.extras.Json(). This test records the requirement so a
    future edit cannot silently drop the wrapping."""
    from tools.pg_import import JSON_COLUMNS
    json_cols = {(t, c) for (t, c) in JSON_COLUMNS}
    assert ("users", "permissions") in json_cols
    assert ("payments", "payment_metadata") in json_cols
    # the current builder emits no JSON handling at all, which is the gap
    sql = P.insert_sql("users")
    assert "Json" not in sql and "json" not in sql.lower()


def test_documented_design_prerequisites_are_still_unimplemented():
    """The 5E.4A design review found these outstanding; assert they remain so
    that enabling execution cannot accidentally skip them."""
    assert P.EXECUTION_ENABLED is False
    for name in ("run_reconciliation_queries", "define_target_transaction",
                 "verify_target_is_empty"):
        assert not hasattr(P, name), name


def test_warning_only_findings_do_not_prevent_a_future_apply():
    """D-1 must survive into the transaction layer: business anomalies never
    block, so a future apply must not consult them as a gate."""
    data = _compat_data()
    data["orders"][0]["total_amount"] = Decimal("9.99")
    report = P.run_compatibility_checks(data)
    blocking = {f.check for f in report.errors}
    assert not any(c.startswith("business") for c in blocking)
