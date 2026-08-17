from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str = "development-secret-key-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")


settings = Settings()


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
