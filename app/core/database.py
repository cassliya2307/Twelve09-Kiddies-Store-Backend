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
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_TIMEOUT: int = 30
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:5173"
    CORS_ALLOW_CREDENTIALS: bool = True
    ENVIRONMENT: str = "development"
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")


settings = Settings()


class Base(DeclarativeBase):
    pass


engine = create_engine(
    normalize_database_url(settings.DATABASE_URL),
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_timeout=settings.DB_POOL_TIMEOUT,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
