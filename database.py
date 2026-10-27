import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
                or "postgresql+psycopg2://liquidity_user:SberHack2026@localhost:5432/liquidity_db")
# Neon / Vercel Postgres отдают адрес со схемой postgres://, SQLAlchemy ждёт postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# На Vercel каждый запрос — отдельная serverless-функция, держать пул соединений нельзя
engine = create_engine(DATABASE_URL, poolclass=NullPool) if os.getenv("VERCEL") else create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
