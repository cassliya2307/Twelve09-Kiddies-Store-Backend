"""Fail-closed helpers for explicitly opted-in local PostgreSQL tests."""

from __future__ import annotations

import os
import re
import ipaddress
from urllib.parse import urlsplit


RUN_OPT_IN = "TWELVE09_RUN_POSTGRES_TESTS"
APPROVAL_OPT_IN = "TWELVE09_APPROVE_DISPOSABLE_POSTGRES"
TARGET_ENV = "IMPORT_TEST_TARGET_URL"
_DATABASE_NAME = re.compile(r"^twelve09_(?:rehearsal|test)_[a-z0-9]{4,32}$")
_pinned_postgres_target: str | None = None


def _validate_candidate(url: str) -> bool:
    """Check URL structure and local-target shape without logging its contents."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        is_loopback = ipaddress.ip_address(host).is_loopback
        database = parsed.path.lstrip("/")
        return (
            parsed.scheme in {"postgres", "postgresql", "postgresql+psycopg2"}
            and is_loopback
            and bool(_DATABASE_NAME.fullmatch(database))
            and parsed.port is not None
            and not parsed.query
            and not parsed.fragment
        )
    except (ValueError, UnicodeError):
        return False


def pin_approved_postgres_target() -> None:
    """Validate and pin the explicitly approved target for this pytest process.

    The approval flag is an operator assertion, not evidence that a database is
    disposable. Loopback and database naming checks cannot establish that fact.
    """
    global _pinned_postgres_target
    if os.environ.get(RUN_OPT_IN) != "1" or os.environ.get(APPROVAL_OPT_IN) != "1":
        raise RuntimeError(
            "PostgreSQL tests require explicit run and disposable-target approval flags"
        )

    url = os.environ.get(TARGET_ENV)
    if not url:
        raise RuntimeError("PostgreSQL tests require IMPORT_TEST_TARGET_URL")

    if not _validate_candidate(url):
        raise RuntimeError(
            "PostgreSQL target rejected; require explicit operator approval and a "
            "supported local target format"
        )
    if _pinned_postgres_target is not None and _pinned_postgres_target != url:
        raise RuntimeError("PostgreSQL target differs from the target already approved for this process")
    _pinned_postgres_target = url


def pinned_postgres_target() -> str:
    """Return the pinned target only while opt-ins and environment remain stable."""
    if _pinned_postgres_target is None:
        raise RuntimeError("PostgreSQL target has not been approved for this pytest process")
    if os.environ.get(RUN_OPT_IN) != "1" or os.environ.get(APPROVAL_OPT_IN) != "1":
        raise RuntimeError("PostgreSQL approval is no longer active")
    if os.environ.get(TARGET_ENV) != _pinned_postgres_target:
        raise RuntimeError("PostgreSQL target changed after approval")
    return _pinned_postgres_target


def create_approved_postgres_engine(**kwargs):
    """Create an engine using only the target pinned during pytest configuration."""
    target = pinned_postgres_target()
    from sqlalchemy import create_engine, event

    engine = create_engine(target, **kwargs)

    def reject_changed_target(*args, **event_kwargs):
        pinned_postgres_target()

    # do_connect runs before a new DBAPI connection is opened; checkout also
    # protects pooled connections if the environment changes after engine creation.
    event.listen(engine, "do_connect", reject_changed_target)
    event.listen(engine.pool, "checkout", reject_changed_target)
    return engine
