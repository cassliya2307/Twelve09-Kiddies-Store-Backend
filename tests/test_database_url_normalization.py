"""Focused unit tests for DATABASE_URL driver normalization.

These tests exercise normalize_database_url() with fake URLs only. No network
access and no live database are required.
"""

from app.core.database import normalize_database_url


def test_postgres_scheme_normalized_to_psycopg2():
    """postgres:// URLs must be normalized to postgresql+psycopg2://."""
    assert (
        normalize_database_url("postgres://user:pass@host:5432/db")
        == "postgresql+psycopg2://user:pass@host:5432/db"
    )
    assert (
        normalize_database_url("postgres://user:pass@host/db")
        == "postgresql+psycopg2://user:pass@host/db"
    )


def test_postgresql_scheme_normalized_to_psycopg2():
    """Bare postgresql:// URLs must be normalized to postgresql+psycopg2://."""
    assert (
        normalize_database_url("postgresql://user:pass@host/db")
        == "postgresql+psycopg2://user:pass@host/db"
    )


def test_postgresql_psycopg3_scheme_normalized_to_psycopg2():
    """postgresql+psycopg:// URLs must be normalized to postgresql+psycopg2://."""
    assert (
        normalize_database_url("postgresql+psycopg://user:pass@host/db")
        == "postgresql+psycopg2://user:pass@host/db"
    )
    assert (
        normalize_database_url("postgresql+psycopg://user:pass@host:5432/db")
        == "postgresql+psycopg2://user:pass@host:5432/db"
    )


def test_postgresql_psycopg2_scheme_unchanged():
    """Already-normalized postgresql+psycopg2:// URLs must not be double-rewritten."""
    assert (
        normalize_database_url("postgresql+psycopg2://user:pass@host/db")
        == "postgresql+psycopg2://user:pass@host/db"
    )


def test_mysql_scheme_normalized_to_pymysql():
    """mysql:// URLs must be normalized to mysql+pymysql:// (preserved behavior)."""
    assert (
        normalize_database_url("mysql://user:pass@host:3306/db")
        == "mysql+pymysql://user:pass@host:3306/db"
    )


def test_sslmode_require_preserved_postgres():
    """Query parameters such as sslmode=require must survive normalization."""
    result = normalize_database_url("postgres://user:pass@host:5432/db?sslmode=require")
    assert result == "postgresql+psycopg2://user:pass@host:5432/db?sslmode=require"


def test_sslmode_require_preserved_postgresql():
    """Query parameters must survive normalization on bare postgresql:// URLs."""
    result = normalize_database_url("postgresql://user:pass@host:5432/db?sslmode=require")
    assert result == "postgresql+psycopg2://user:pass@host:5432/db?sslmode=require"


def test_empty_and_none_unchanged():
    """Empty string and None must be returned unchanged."""
    assert normalize_database_url("") == ""
    assert normalize_database_url(None) is None
