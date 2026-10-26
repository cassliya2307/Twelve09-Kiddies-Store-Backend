import os

# Override inherited environment configuration before importing application
# modules so the global engine and TestClient lifespan cannot select production.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "pytest-isolation-only-secret"

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from postgres_test_safety import (
    APPROVAL_OPT_IN,
    RUN_OPT_IN,
    create_approved_postgres_engine,
    pin_approved_postgres_target,
    pinned_postgres_target,
)


def pytest_configure(config):
    """Fail closed if application import did not produce a SQLite engine."""
    from app.core.database import engine

    app_url = make_url(engine.url)
    if app_url.get_backend_name() != "sqlite" or app_url.database is not None:
        raise pytest.UsageError("Test startup requires an in-memory SQLite application engine")
    if os.environ.get(RUN_OPT_IN) == "1" and os.environ.get(APPROVAL_OPT_IN) == "1":
        try:
            pin_approved_postgres_target()
        except RuntimeError as exc:
            raise pytest.UsageError(str(exc)) from None


@pytest.fixture(scope="function")
def pgconn():
    """Provide a live PostgreSQL connection for native tests.

    Returns a tuple: (connection, text_func, url)
    Each test gets its own connection with autocommit=True to avoid
    transaction state leaking between tests.
    """
    engine = create_approved_postgres_engine(isolation_level="AUTOCOMMIT")
    conn = engine.connect()
    try:
        yield (conn, text, "approved disposable local PostgreSQL target")
    finally:
        conn.close()
        engine.dispose()


def pytest_collection_modifyitems(items):
    """Keep PostgreSQL integration suites opt-in and validate before execution."""
    enabled = (
        os.environ.get(RUN_OPT_IN) == "1"
        and os.environ.get(APPROVAL_OPT_IN) == "1"
    )
    pg_files = {"test_pg_target_smoke.py", "test_receipt_workflow.py"}
    if enabled:
        try:
            pinned_postgres_target()
        except RuntimeError as exc:
            raise pytest.UsageError(str(exc)) from None
        return

    skip_pg = pytest.mark.skip(reason="PostgreSQL integration tests are opt-in")
    for item in items:
        if item.path.name in pg_files:
            item.add_marker(skip_pg)