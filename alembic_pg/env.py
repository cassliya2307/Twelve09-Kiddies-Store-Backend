import sys
from pathlib import Path
from urllib.parse import urlparse

from alembic import context
from sqlalchemy import engine_from_config, pool

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.core.database import Base, settings, normalize_database_url
from app.models import *  # noqa: F401,F403


config = context.config

target_metadata = Base.metadata


def _get_postgresql_url() -> str:
    """Return the normalized PostgreSQL URL, failing fast for other dialects.

    This PostgreSQL migration environment must never be pointed at MySQL or
    SQLite. The URL scheme is validated from the configured DATABASE_URL and
    the full URL (including any password) is never printed.
    """
    url = normalize_database_url(settings.DATABASE_URL)
    scheme = (urlparse(url).scheme or "").lower() if url else ""
    if scheme not in ("postgresql", "postgresql+psycopg2"):
        raise RuntimeError(
            "alembic_pg requires a PostgreSQL DATABASE_URL "
            "(postgres:// or postgresql://). Received a non-PostgreSQL scheme: "
            f"{scheme or '<empty>'}. Run the MySQL environment "
            "(alembic -c alembic.ini ...) for MySQL."
        )
    return url


def run_migrations_offline() -> None:
    url = _get_postgresql_url().replace("%", "%%")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_postgresql_url().replace("%", "%%")
    config.set_main_option("sqlalchemy.url", url)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
