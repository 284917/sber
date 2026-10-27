import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

load_dotenv()

def find_database_url():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if url:
        return url
    # Интеграция Neon на Vercel может добавить префикс: STORAGE_DATABASE_URL, MYDB_POSTGRES_URL и т.п.
    for key in sorted(os.environ):
        if key.endswith("_DATABASE_URL") or key.endswith("_POSTGRES_URL"):
            return os.environ[key]
    return None


DATABASE_URL = find_database_url() or "postgresql+psycopg2://liquidity_user:SberHack2026@localhost:5432/liquidity_db"
# Neon / Vercel Postgres отдают адрес со схемой postgres://, SQLAlchemy ждёт postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# На Vercel каждый запрос — отдельная serverless-функция, держать пул соединений нельзя.
# SQLite используется в тестах: соединение должно быть доступно из потоков тестового клиента
engine_kwargs = {"poolclass": NullPool} if os.getenv("VERCEL") else {}
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
