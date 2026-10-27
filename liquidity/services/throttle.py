"""Защита от перебора пароля.

Неудачные входы считаются отдельно по email и по IP-адресу в скользящем окне 15 минут.
5 ошибок для одного email или 20 с одного IP блокируют вход на 15 минут. Успешный вход сбрасывает счётчик email.
Счётчики хранятся в БД: на Vercel каждый запрос может обработать новый экземпляр функции.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from liquidity import models

WINDOW = timedelta(minutes=15)
LOCK = timedelta(minutes=15)
LIMITS = {"email": 5, "ip": 20}


def _keys(email: str, ip: str):
    return [("email", f"email:{(email or '').strip().lower()}"), ("ip", f"ip:{ip or 'unknown'}")]


def locked_for(db: Session, email: str, ip: str, now: datetime = None):
    """Сколько ещё длится блокировка (timedelta) или None, если входить можно."""
    now = now or datetime.utcnow()
    longest = None
    for _, key in _keys(email, ip):
        row = db.get(models.LoginAttempt, key)
        if row and row.locked_until and row.locked_until > now:
            left = row.locked_until - now
            longest = max(longest, left) if longest else left
    return longest


def register_failure(db: Session, email: str, ip: str, now: datetime = None):
    """Учитывает неудачную попытку; возвращает, сколько попыток для этого email осталось до блокировки."""
    now = now or datetime.utcnow()
    remaining = None
    for kind, key in _keys(email, ip):
        row = db.get(models.LoginAttempt, key)
        if not row:
            row = models.LoginAttempt(key=key, failures=0, window_start=now)
            db.add(row)
        if now - row.window_start > WINDOW:  # окно истекло — считаем заново
            row.failures, row.window_start, row.locked_until = 0, now, None
        row.failures += 1
        if row.failures >= LIMITS[kind]:
            row.locked_until = now + LOCK
        if kind == "email":
            remaining = max(LIMITS[kind] - row.failures, 0)
    db.commit()
    return remaining


def register_success(db: Session, email: str):
    row = db.get(models.LoginAttempt, _keys(email, "")[0][1])
    if row:
        db.delete(row)
        db.commit()


def client_ip(request, on_vercel: bool) -> str:
    """На Vercel реальный адрес клиента приходит в x-real-ip (заголовок ставит сам Vercel);
    локально доверять заголовкам нельзя — берём адрес соединения."""
    if on_vercel:
        forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
