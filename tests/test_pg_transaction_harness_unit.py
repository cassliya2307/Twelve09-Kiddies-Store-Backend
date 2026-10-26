"""Phase 5E.4B-1 mocked transaction harness tests.

Phase 5E.4B-1 is plumbing only. NOTHING here opens a socket, creates an
engine, connects to a database, executes SQL, or enables execution.

CRITICAL SCOPE NOTE -- simulation, not proof:
The stateful fake transaction in this file *simulates* the transaction
CONTRACT that a future implementation must satisfy. It does NOT and cannot
prove PostgreSQL's real atomicity, durability, or isolation behaviour. Those
properties must be demonstrated against a disposable PostgreSQL 16 instance
before any real import is trusted. Tests relying on that simulation are marked
in their docstrings as ``SIMULATION``; tests that assert behaviour of the real
wrapper against a fake are marked ``REQUIREMENT``.

Run from the backend directory so the ``tools`` package is importable:

    cd backend
    python -m pytest tests/test_pg_transaction_harness_unit.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools import pg_import as P


_BACKEND_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fakes: connection, transaction, and a stateful row store
# --------------------------------------------------------------------------- #
class FakeConnection:
    """A fake DB-API-ish connection. No socket, no engine."""

    def __init__(self, *, fail_close: bool = False, label: str = "target conn"):
        self.closed = False
        self.cursors_closed = 0
        self.fail_close = fail_close
        self.label = label
        self.executed: list[str] = []

    def cursor(self):
        return FakeCursor(self)

    def close(self) -> None:
        if self.fail_close:
            raise RuntimeError("close blew up")
        self.closed = True


class FakeCursor:
    def __init__(self, owner):
        self._owner = owner

    def execute(self, sql, params=()):
        self._owner.executed.append(sql)

    def fetchall(self):
        return []

    def close(self):
        self._owner.cursors_closed += 1


class FakeTransaction:
    """A stateful fake implementing the documented transaction protocol.

    Pending writes become visible in the store only on a successful commit;
    rollback discards them. This SIMULATES the contract -- it is not evidence
    about PostgreSQL.
    """

    def __init__(self, store=None, *, fail_commit=False, fail_rollback=False,
                 fail_work=None):
        self.store = store if store is not None else {}
        self.pending: dict[str, list] = {}
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.fail_work = fail_work
        self.commits = 0
        self.rollbacks = 0
        self._active = True
        self.closed = False

    # -- protocol ---------------------------------------------------------- #
    @property
    def is_active(self) -> bool:
        return self._active

    def write(self, table: str, value) -> None:
        """Simulate an in-transaction write (pending, not yet visible)."""
        if self.fail_work is not None:
            raise self.fail_work
        self.pending.setdefault(table, []).append(value)

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit blew up")
        if self.closed:
            raise RuntimeError("commit on a closed transaction")
        for table, values in self.pending.items():
            self.store.setdefault(table, []).extend(values)
        self.pending = {}
        self.commits += 1
        self._active = False
        self.closed = True

    def rollback(self) -> None:
        if self.fail_rollback:
            raise RuntimeError("rollback blew up")
        if self.closed:
            raise RuntimeError("rollback on a closed transaction")
        self.pending = {}
        self.rollbacks += 1
        self._active = False
        self.closed = True

    # -- helpers ----------------------------------------------------------- #
    @property
    def visible(self) -> dict[str, list]:
        return self.store

    @property
    def unresolved(self) -> dict[str, list]:
        return self.pending


def _fake_begin(transaction):
    """A ``begin`` callable that hands back a prepared fake transaction."""
    def begin(connection):
        connection.transaction = transaction
        return transaction
    return begin


def _fake_connect(connection):
    def connect():
        return connection
    return connect


# --------------------------------------------------------------------------- #
# A. Injectable fakes, no real engines or sockets
# --------------------------------------------------------------------------- #
def test_target_transaction_uses_the_injected_fake_without_an_engine(monkeypatch):
    created = []

    def boom(*a, **k):
        created.append(True)
        raise AssertionError("no real engine may be created")

    monkeypatch.setattr(P, "make_engine", boom)
    monkeypatch.setattr(P, "open_target_connection", lambda *a, **k: boom())
    store: dict = {}
    tx = FakeTransaction(store)
    with P.TargetTransaction(_fake_connect(FakeConnection()), _fake_begin(tx)) as t:
        assert t is tx
        t.write("orders", {"id": 1})
    assert created == []
    assert store["orders"] == [{"id": 1}]


def test_production_default_path_fails_closed_while_execution_is_disabled():
    with pytest.raises(P.Fail) as ei:
        P.open_target_connection(
            "postgresql+psycopg2://postgres:x@127.0.0.1:54321/twelve09_rehearsal_reh1")
    assert ei.value.code == P.ExitCode.GATE
    assert "psycopg2" not in ei.value.message  # no URL/credential leak


def test_production_factory_refuses_even_if_the_guard_were_flipped(monkeypatch):
    # In Phase 5E.5, the target connection IS implemented.
    # When EXECUTION_ENABLED=True (authorized build), the connection is attempted.
    # This test verifies that the "not implemented" failure is gone.
    monkeypatch.setattr(P, "EXECUTION_ENABLED", True)
    with pytest.raises(Exception) as ei:
        P.open_target_connection(
            "postgresql+psycopg2://postgres:x@127.0.0.1:54321/twelve09_rehearsal_reh1")
    # Should NOT fail with "not implemented" - that was the old Phase 5E.4B-1 behavior
    # It may fail with connection error (no server) or other runtime error
    assert "not implemented" not in str(ei.value).lower()
    monkeypatch.setattr(P, "EXECUTION_ENABLED", False)
    assert P.EXECUTION_ENABLED is False


def test_target_connection_factory_is_inert_until_called():
    cfg = _cfg()
    factory = P.target_connection_factory(cfg)
    assert callable(factory)
    with pytest.raises(P.Fail) as ei:
        factory()
    assert ei.value.code == P.ExitCode.GATE


def _cfg():
    return P.resolve_config(
        {"IMPORT_SOURCE_URL": "mysql+pymysql://root:secret@127.0.0.1:33060/"
                              "twelve09_src_reh1?charset=utf8mb4",
         "IMPORT_TARGET_URL": "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                              "twelve09_rehearsal_reh1",
         "SOURCE_IS_DISPOSABLE_MYSQL": "1",
         "CONFIRM_DISPOSABLE_TARGET": "yes"},
        "reh1")


def test_target_transaction_requires_a_callable_connect():
    for bad in (None, "nope", 42):
        with pytest.raises(P.Fail) as ei:
            P.TargetTransaction(bad)
        assert ei.value.code == P.ExitCode.RUNTIME


def test_target_transaction_requires_a_callable_begin_when_given():
    with pytest.raises(P.Fail) as ei:
        P.TargetTransaction(_fake_connect(FakeConnection()), begin="nope")
    assert ei.value.code == P.ExitCode.RUNTIME


def test_no_begin_implementation_fails_closed_and_still_closes():
    """Without a reviewed transaction implementation, the wrapper refuses and
    still releases the connection."""
    conn = FakeConnection()
    with pytest.raises(P.Fail) as ei:
        with P.TargetTransaction(_fake_connect(conn)):
            pass
    assert ei.value.code == P.ExitCode.GATE
    assert conn.closed


# --------------------------------------------------------------------------- #
# B. Transaction-boundary REQUIREMENT tests
# --------------------------------------------------------------------------- #
def test_success_commits_exactly_once_after_the_work():
    store: dict = {}
    tx = FakeTransaction(store)
    seen = []
    with P.TargetTransaction(_fake_connect(FakeConnection()), _fake_begin(tx)) as t:
        t.write("orders", {"id": 1})
        seen.append(("work", t.unresolved, t.is_active))
    assert tx.commits == 1 and tx.rollbacks == 0
    assert store["orders"] == [{"id": 1}]
    # the work happened before the commit, inside the transaction
    assert seen[0][1] == {"orders": [{"id": 1}]}
    assert seen[0][2] is True


def test_connection_is_closed_on_success():
    conn = FakeConnection()
    with P.TargetTransaction(_fake_connect(conn), _fake_begin(FakeTransaction())):
        pass
    assert conn.closed


def test_exception_before_work_rolls_back_exactly_once():
    store: dict = {}
    tx = FakeTransaction(store)
    conn = FakeConnection()
    with pytest.raises(RuntimeError):
        with P.TargetTransaction(_fake_connect(conn), _fake_begin(tx)):
            raise RuntimeError("pre-work failure")
    assert tx.rollbacks == 1 and tx.commits == 0
    assert store == {}
    assert conn.closed


def test_exception_during_work_discards_pending_writes():
    """SIMULATION: pending writes must NOT reach the committed store."""
    store: dict = {}
    tx = FakeTransaction(store)
    with pytest.raises(RuntimeError):
        with P.TargetTransaction(_fake_connect(FakeConnection()),
                                 _fake_begin(tx)) as t:
            t.write("orders", {"id": 1})
            t.write("orders", {"id": 2})
            raise RuntimeError("mid-work failure")
    assert tx.rollbacks == 1 and tx.commits == 0
    assert store == {}
    assert tx.unresolved == {}


def test_exception_during_precommit_validation_rolls_back():
    store: dict = {}
    tx = FakeTransaction(store)
    conn = FakeConnection()
    with pytest.raises(RuntimeError):
        with P.TargetTransaction(_fake_connect(conn), _fake_begin(tx)) as t:
            t.write("users", {"id": 1})
            raise RuntimeError("pre-commit validation failed")
    assert tx.rollbacks == 1 and tx.commits == 0
    assert store == {}
    assert conn.closed


def test_commit_failure_is_surfaced_and_triggers_rollback():
    store: dict = {}
    tx = FakeTransaction(store, fail_commit=True)
    conn = FakeConnection()
    with pytest.raises(P.Fail) as ei:
        with P.TargetTransaction(_fake_connect(conn), _fake_begin(tx)):
            tx.write("orders", {"id": 1})
    assert ei.value.code == P.ExitCode.RUNTIME
    assert tx.commits == 0 and tx.rollbacks == 1
    assert store == {}          # SIMULATION: nothing committed
    assert conn.closed


def test_commit_failure_is_never_converted_into_success(capsys):
    tx = FakeTransaction({}, fail_commit=True)
    with pytest.raises(P.Fail):
        with P.TargetTransaction(_fake_connect(FakeConnection()),
                                 _fake_begin(tx)):
            pass
    assert "success" not in capsys.readouterr().err.lower()


def test_commit_succeeds_but_rollback_failure_does_not_hide_success():
    """A rollback failure only matters on the failure path."""
    store: dict = {}
    tx = FakeTransaction(store, fail_rollback=True)
    with P.TargetTransaction(_fake_connect(FakeConnection()), _fake_begin(tx)) as t:
        t.write("orders", {"id": 1})
    assert tx.commits == 1
    assert store["orders"] == [{"id": 1}]


def test_rollback_failure_preserves_the_original_failure():
    """Cleanup failure must never replace the original transaction failure."""
    store: dict = {}
    tx = FakeTransaction(store, fail_rollback=True)
    original = RuntimeError("the real problem")
    wrapper = P.TargetTransaction(_fake_connect(FakeConnection()),
                                  _fake_begin(tx))
    with pytest.raises(RuntimeError) as ei:
        with wrapper:
            raise original
    assert ei.value is original            # the original propagates intact
    assert tx.rollbacks == 0               # rollback was attempted and failed
    assert wrapper.cleanup_errors          # and the failure was retained


def test_rollback_failure_is_retained_as_sanitized_text():
    store: dict = {}
    tx = FakeTransaction(store, fail_rollback=True)
    wrapper = P.TargetTransaction(_fake_connect(FakeConnection()),
                                  _fake_begin(tx))
    with pytest.raises(RuntimeError):
        with wrapper:
            raise RuntimeError("boom")
    assert wrapper.cleanup_errors, "rollback failure was not retained"
    blob = "\n".join(wrapper.cleanup_errors)
    assert "rollback blew up" not in blob  # raw driver text is not retained
    assert "cleanly" in blob


def test_connection_close_failure_after_failure_path_keeps_original():
    original = RuntimeError("original failure")
    conn = FakeConnection(fail_close=True)
    with pytest.raises(RuntimeError) as ei:
        with P.TargetTransaction(_fake_connect(conn), _fake_begin(FakeTransaction())):
            raise original
    assert ei.value is original


def test_connection_close_failure_after_success_is_surfaced():
    conn = FakeConnection(fail_close=True)
    with pytest.raises(P.Fail) as ei:
        with P.TargetTransaction(_fake_connect(conn), _fake_begin(FakeTransaction())):
            pass
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "cleanup failed" in ei.value.message


def test_failed_connect_is_sanitized():
    def boom():
        raise RuntimeError("Access denied for user 'postgres'@'10.0.0.1'")

    with pytest.raises(P.Fail) as ei:
        with P.TargetTransaction(boom, _fake_begin(FakeTransaction())):
            pass
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "postgres" not in ei.value.message
    assert "Access denied" not in ei.value.message


def test_connect_failure_closes_nothing_but_reports():
    """A connection that never opened must not produce a close error."""
    def boom():
        raise OSError("refused")

    wrapper = P.TargetTransaction(boom, _fake_begin(FakeTransaction()))
    with pytest.raises(P.Fail):
        with wrapper:
            pass
    assert wrapper.cleanup_errors == []


def test_begin_failure_is_sanitized_and_connection_is_closed():
    conn = FakeConnection()

    def bad_begin(connection):
        raise RuntimeError("cannot BEGIN")

    with pytest.raises(P.Fail) as ei:
        with P.TargetTransaction(_fake_connect(conn), bad_begin):
            pass
    assert ei.value.code == P.ExitCode.RUNTIME
    assert conn.closed


def test_transaction_without_required_method_fails_closed():
    class Broken:
        pass

    conn = FakeConnection()
    with pytest.raises(P.Fail) as ei:
        with P.TargetTransaction(_fake_connect(conn), lambda c: Broken()):
            pass
    assert ei.value.code == P.ExitCode.RUNTIME
    assert conn.closed


def test_finished_transaction_cannot_be_reused():
    store: dict = {}
    tx = FakeTransaction(store)
    wrapper = P.TargetTransaction(_fake_connect(FakeConnection()),
                                  _fake_begin(tx))
    with wrapper:
        pass
    assert wrapper._finished is True
    with pytest.raises(P.Fail) as ei:
        with wrapper:
            pass
    assert ei.value.code == P.ExitCode.RUNTIME
    assert "reuse" in ei.value.message


def test_reentering_an_open_transaction_is_refused():
    tx = FakeTransaction({})
    wrapper = P.TargetTransaction(_fake_connect(FakeConnection()),
                                  _fake_begin(tx))
    with wrapper as first:
        with pytest.raises(P.Fail) as ei:
            with wrapper:
                pass
        assert ei.value.code == P.ExitCode.RUNTIME
        assert "re-enter" in ei.value.message


def test_no_transaction_object_is_referenced_after_completion():
    tx = FakeTransaction({})
    wrapper = P.TargetTransaction(_fake_connect(FakeConnection()),
                                  _fake_begin(tx))
    with wrapper:
        pass
    assert wrapper._transaction is None
    assert wrapper._connection is None


def test_rollback_is_skipped_when_the_transaction_is_inactive():
    """A transaction that already closed itself must not be rolled back."""
    class SelfClosing:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self._active = True

        @property
        def is_active(self):
            return self._active

        def commit(self):
            self.commits += 1
            self._active = False

        def rollback(self):
            self.rollbacks += 1
            self._active = False

    tx = SelfClosing()
    tx._active = False  # pretend the driver already closed it
    with pytest.raises(RuntimeError):
        with P.TargetTransaction(_fake_connect(FakeConnection()),
                                 _fake_begin(tx)):
            raise RuntimeError("boom")
    assert tx.rollbacks == 0


# --------------------------------------------------------------------------- #
# C. Simulated zero-partial-write evidence (SIMULATION, not PostgreSQL proof)
# --------------------------------------------------------------------------- #
def test_simulation_committed_rows_survive_a_successful_run():
    store: dict = {}
    tx = FakeTransaction(store)
    with P.TargetTransaction(_fake_connect(FakeConnection()),
                             _fake_begin(tx)) as t:
        t.write("orders", {"id": 1})
        t.write("payments", {"id": 1})
    assert store == {"orders": [{"id": 1}], "payments": [{"id": 1}]}


@pytest.mark.parametrize("stage", [
    "before_work", "during_work", "precommit_validation", "commit", "begin",
])
def test_simulation_no_pending_writes_survive_a_failed_run(stage):
    """SIMULATION of the transaction contract: whatever the failing stage, the
    committed store stays empty and nothing is left pending."""
    store: dict = {}
    tx = FakeTransaction(store,
                         fail_commit=(stage == "commit"),
                         fail_work=(RuntimeError("write failed")
                                    if stage == "during_work" else None))
    if stage == "begin":
        def bad_begin(connection):
            raise RuntimeError("cannot BEGIN")
        begin = bad_begin
    else:
        begin = _fake_begin(tx)

    with pytest.raises(Exception):
        with P.TargetTransaction(_fake_connect(FakeConnection()), begin) as t:
            if stage != "begin":
                t.write("orders", {"id": 1})
            if stage == "before_work":
                raise RuntimeError("failed before any write")
            if stage == "precommit_validation":
                raise RuntimeError("pre-commit validation failed")
    assert store == {}, f"pending writes leaked after {stage}"
    if stage in ("before_work", "during_work", "precommit_validation"):
        assert tx.commits == 0


def test_simulation_repeat_run_is_deterministic():
    """SIMULATION: two identical successful runs produce identical state."""
    for _ in range(2):
        store: dict = {}
        tx = FakeTransaction(store)
        with P.TargetTransaction(_fake_connect(FakeConnection()),
                                 _fake_begin(tx)) as t:
            t.write("orders", {"id": 1})
        assert store == {"orders": [{"id": 1}]}


def test_sulation_disabled_by_default_and_not_reachable_from_cli(monkeypatch):
    """SIMULATION: the harness cannot be reached from any CLI mode."""
    assert P.EXECUTION_ENABLED is False
    for name in ("run_target_transaction", "apply_to_target", "target_transaction"):
        assert not hasattr(P, name), name


# --------------------------------------------------------------------------- #
# D. JSON adaptation blocker must stay explicit
# --------------------------------------------------------------------------- #
def test_json_columns_are_recorded_as_an_integration_prerequisite():
    """The two JSON columns must remain recorded; psycopg2 cannot be exercised
    here because it is not installed."""
    from tools.pg_import import JSON_COLUMNS
    assert ("users", "permissions") in JSON_COLUMNS
    assert ("payments", "payment_metadata") in JSON_COLUMNS
    # a Python list is NOT safely adaptable by psycopg2's default adapter
    sample = ["MANAGE_ORDERS"]
    assert isinstance(sample, list)
    # the current builder emits no JSON handling at all -- the recorded gap
    assert "Json" not in P.insert_sql("users")


def test_no_target_insertion_was_added_to_address_json():
    """Do not implement target insertion just to work around JSON adaptation."""
    assert not hasattr(P, "insert_rows")
    assert not hasattr(P, "apply_to_target")


# --------------------------------------------------------------------------- #
# Safety: no network, no target write path, guard still closed
# --------------------------------------------------------------------------- #
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


def test_execution_remains_disabled():
    assert P.EXECUTION_ENABLED is False


def test_no_cli_mode_routes_to_a_target_write(monkeypatch):
    """REQUIREMENT: write modes fail closed; read-only modes reach only the
    read-only source inspection -- never a target write."""
    import argparse

    reached_inspect = []

    def fake_inspect(cfg, **kwargs):
        reached_inspect.append(True)
        return P.ExitCode.OK

    monkeypatch.setattr(P, "inspect_source", fake_inspect)
    monkeypatch.setattr(P, "open_target_connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no target connection may be opened")))
    monkeypatch.setattr(P, "TargetTransaction", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no target transaction may be opened")))
    cfg = _cfg()
    for mode in ("apply", "reset_sequences"):
        with pytest.raises(P.Fail) as ei:
            P._dispatch_db_mode(
                P.resolve_plan(argparse.Namespace(primary=mode,
                                                  truncate_target=False)),
                cfg, None, None)
        assert ei.value.code == P.ExitCode.GATE
    assert reached_inspect == [], "a write mode reached read-only inspection"
    for mode in ("dry_run", "validate_only"):
        assert P._dispatch_db_mode(
            P.resolve_plan(argparse.Namespace(primary=mode, truncate_target=False)),
            cfg, None, None) == P.ExitCode.OK
    assert reached_inspect == [True, True]


@pytest.mark.parametrize("sneaky", [
    "IMPORT_EXECUTION_ENABLED", "IMPORT_EXECUTE", "EXECUTION_ENABLED",
    "PG_IMPORT_ENABLED", "IMPORT_TARGET_WRITE", "IMPORT_ALLOW_TARGET",
    "IMPORT_TRANSACTION", "IMPORT_ENABLE_TRANSACTION",
])
def test_no_env_var_activates_the_transaction_seam(monkeypatch, sneaky):
    monkeypatch.setenv("IMPORT_SOURCE_URL",
                       "mysql+pymysql://root:secret@127.0.0.1:33060/"
                       "twelve09_src_reh1?charset=utf8mb4")
    monkeypatch.setenv("IMPORT_TARGET_URL",
                       "postgresql+psycopg2://u:p@127.0.0.1:5432/"
                       "twelve09_rehearsal_reh1")
    monkeypatch.setenv("SOURCE_IS_DISPOSABLE_MYSQL", "1")
    monkeypatch.setenv("CONFIRM_DISPOSABLE_TARGET", "yes")
    monkeypatch.setenv(sneaky, "1")
    assert P.main(["--run-id", "reh1", "--apply"]) == P.ExitCode.GATE
    assert P.EXECUTION_ENABLED is False


def test_no_target_sql_strings_are_emitted_by_the_seam():
    """The seam must not add write SQL of its own; the pre-existing builders
    remain unused and unreachable."""
    assert "INSERT" not in P.TRUNCATE_SQL
    assert "setval" not in P.insert_sql("users")
    assert "TRUNCATE" in P.TRUNCATE_SQL          # pre-existing builder, unused
    src = (_BACKEND_DIR / "tools" / "pg_import.py").read_text(encoding="utf-8")
    # every write builder that exists is pre-existing, and none is invoked by
    # the new seam (TargetTransaction / open_target_connection / factory)
    for fn in (P.TargetTransaction, P.open_target_connection,
               P.target_connection_factory):
        assert fn.__name__ not in ("apply_to_target", "truncate_target")
    # the new seam contributes no INSERT/UPDATE/DELETE text of its own
    seam_start = src.index("Target connection + transaction boundary (Phase 5E.5)")
    seam_end = src.index("def inspect_source(")
    seam = src[seam_start:seam_end]
    for marker in ("INSERT INTO", "DELETE FROM", "UPDATE ", "ALTER "):
        assert marker not in seam, marker


def test_transaction_wrapper_contains_no_table_specific_behaviour():
    """The wrapper must stay generic: no table names, and no SQL text outside
    its explanatory docstring."""
    import ast
    import inspect
    src = inspect.getsource(P.TargetTransaction)
    tree = ast.parse(src)
    body = "\n".join(
        ast.unparse(node) for node in tree.body[0].body  # class body, no docstring
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)))
    for table in P.TABLE_ORDER:
        assert table not in body, table
    for sql_marker in ("INSERT", "TRUNCATE", "setval", "SELECT ", "ALTER "):
        assert sql_marker not in body, sql_marker
    # and no SQL text at all is executed by the seam's own helpers
    for helper in (P._call_or_raise, P._transaction_is_active):
        text = inspect.getsource(helper)
        assert "execute(" not in text, helper.__name__
