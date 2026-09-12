from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


BASE_DIR = Path(__file__).resolve().parents[2]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_database_url(url: str) -> str:
    """Normalize DATABASE_URL to use PyMySQL driver for MySQL connections.

    Converts generic mysql:// URLs to mysql+pymysql:// to ensure SQLAlchemy
    selects the PyMySQL driver instead of MySQLdb (mysqlclient).
    """
    if not url:
        return url

    parsed = urlparse(url)
    if parsed.scheme == "mysql":
        return urlunparse(parsed._replace(scheme="mysql+pymysql"))
    return url


def _sqlite_connect_args(url: str) -> dict:
    """Return SQLite-specific connect args; empty dict for non-SQLite URLs."""
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", "sqlite_async"):
        return {"check_same_thread": False}
    return {}


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    PRODUCT_IMAGE_UPLOAD_DIR: str = "static/uploads"
    PRODUCT_IMAGE_MAX_SIZE: int = 5 * 1024 * 1024
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""
    PAYSTACK_WEBHOOK_SECRET: str = ""
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_TIMEOUT: int = 30
    CORS_ALLOWED_ORIGINS: str = "https://twelve09-kiddies-store-frontend-sepia.vercel.app,http://localhost:3000,http://localhost:5173"
    CORS_ALLOW_CREDENTIALS: bool = True
    ENVIRONMENT: str = "development"
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")


settings = Settings()


def _engine_kwargs(database_url: str) -> dict:
    """Build engine keyword arguments conditional on the database dialect.

    MySQL-specific pool parameters (pool_size, max_overflow, pool_recycle,
    pool_timeout) are omitted for SQLite to avoid compatibility issues and
    test-harness timeouts.
    """
    parsed = urlparse(database_url)
    scheme = (parsed.scheme or "").lower()
    kwargs = {
        "pool_pre_ping": True,
    }
    if scheme == "sqlite":
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = _sqlite_connect_args(database_url)
    else:
        kwargs.update(
            {
                "pool_size": settings.DB_POOL_SIZE,
                "max_overflow": settings.DB_MAX_OVERFLOW,
                "pool_recycle": settings.DB_POOL_RECYCLE,
                "pool_timeout": settings.DB_POOL_TIMEOUT,
            }
        )
    return kwargs


class Base(DeclarativeBase):
    pass


engine = create_engine(
    normalize_database_url(settings.DATABASE_URL),
    **_engine_kwargs(settings.DATABASE_URL),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
