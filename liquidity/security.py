"""Пароли, сессии (JWT в httpOnly-cookie), проверка ролей и защита от CSRF."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from markupsafe import Markup
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db

SESSION_COOKIE = "access_token"
CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "csrf_token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- Пароли ---
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


# --- Сессия ---
def create_token(user: models.User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=config.SESSION_HOURS)
    return jwt.encode({"user_id": user.id, "role": user.role, "exp": expires}, config.SECRET_KEY, algorithm="HS256")


def start_session(response, user: models.User):
    response.set_cookie(SESSION_COOKIE, create_token(user), httponly=True, samesite="lax",
                        secure=config.SECURE_COOKIES, max_age=config.SESSION_HOURS * 3600)


def end_session(response):
    response.delete_cookie(SESSION_COOKIE)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Пользователь из cookie. Нет cookie, подпись неверна или срок истёк — 401 (страницы перекидывают на вход)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.get(models.User, payload.get("user_id"))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


def require_role(*roles: str):
    """Зависимость: пускает только пользователей с одной из ролей, остальным — 403."""
    def dependency(user: models.User = Depends(get_current_user)) -> models.User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return dependency


# --- CSRF (double submit cookie) ---
async def csrf_cookie_middleware(request: Request, call_next):
    """Выдаёт каждому посетителю случайный CSRF-токен в cookie; шаблоны вставляют его в формы."""
    token = request.cookies.get(CSRF_COOKIE)
    is_new = not token
    if is_new:
        token = secrets.token_urlsafe(32)
    request.state.csrf_token = token
    response = await call_next(request)
    if is_new:
        response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax", secure=config.SECURE_COOKIES)
    return response


async def verify_csrf(request: Request):
    """Глобальная зависимость: изменяющие запросы обязаны прислать токен, совпадающий с cookie.
    Чужой сайт не может прочитать cookie (httpOnly + SameSite), поэтому не подставит верный токен."""
    if request.method not in UNSAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()  # FastAPI кэширует разобранную форму, обработчик получит её же
    sent = form.get(CSRF_FIELD)
    if not cookie or not isinstance(sent, str) or not secrets.compare_digest(sent, cookie):
        raise HTTPException(status_code=403, detail="CSRF")


def csrf_field(request: Request) -> Markup:
    token = getattr(request.state, "csrf_token", "")
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">')
