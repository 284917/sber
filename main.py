"""Точка входа: Vercel и uvicorn ищут здесь объект app (uvicorn main:app).
Вся логика — в пакете liquidity (см. liquidity/__init__.py)."""
from liquidity.app import app  # noqa: F401
