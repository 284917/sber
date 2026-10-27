"""Подключение к БД, сессии и миграции новых колонок."""
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from liquidity import config


def find_database_url():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if url:
        return url
    # Интеграция Neon на Vercel может добавить префикс: STORAGE_DATABASE_URL, MYDB_POSTGRES_URL и т.п.
    for key in sorted(os.environ):
        if key.endswith("_DATABASE_URL") or key.endswith("_POSTGRES_URL"):
            return os.environ[key]
    return None


DATABASE_URL = find_database_url() or "postgresql+psycopg2://liquidity_user:password@localhost:5432/liquidity_db"
# Neon / Vercel Postgres отдают адрес со схемой postgres://, SQLAlchemy ждёт postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# На Vercel каждый запрос — отдельная serverless-функция, держать пул соединений нельзя.
# SQLite используется в тестах: соединение должно быть доступно из потоков тестового клиента
engine_kwargs = {"poolclass": NullPool} if config.ON_VERCEL else {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# create_all не добавляет колонки в уже существующие таблицы, поэтому новые поля дописываются здесь
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS country_code VARCHAR(2)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_number VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_reject_reason VARCHAR(500)",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS term_months INTEGER",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS transfer_method VARCHAR(30)",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS moderation_comment VARCHAR(500)",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS extra_terms VARCHAR(1000)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
]


def create_schema():
    from liquidity import models  # noqa: F401  регистрирует таблицы в Base.metadata
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":  # в SQLite таблицы создаются сразу с новыми колонками
        with engine.begin() as conn:
            for statement in MIGRATIONS:
                conn.execute(text(statement))
