"""Сборка приложения FastAPI: middleware, обработчики ошибок, роутеры, инициализация БД."""
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from liquidity import config
from liquidity.database import SessionLocal, create_schema, find_database_url
from liquidity.routers import admin, auth, borrower, creditor, deals, kyc, operator
from liquidity.security import SESSION_COOKIE, csrf_cookie_middleware, verify_csrf
from liquidity.services import Forbidden, Invalid, NotFound, accounts, demo

app = FastAPI(
    title="Платформа ликвидности Сбер",
    description="MVP цифровой платформы трансграничного финансирования между юрлицами: "
                "заёмщики (РФ), кредиторы (зарубежье), оператор и администратор.",
    version="1.0.0",
    dependencies=[Depends(verify_csrf)],  # CSRF-проверка для всех изменяющих запросов
)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
for module in (auth, borrower, creditor, kyc, deals, operator, admin):
    app.include_router(module.router)


# --- Инициализация БД: если она недоступна, приложение не падает целиком ---
DB_INIT_ERROR = None


def init_db():
    global DB_INIT_ERROR
    try:
        create_schema()
        with SessionLocal() as db:
            accounts.ensure_admin(db)
            if config.DEMO_MODE:
                demo.ensure_seeded(db)
        DB_INIT_ERROR = None
    except Exception as e:  # noqa: BLE001 — показываем причину на /health вместо падения функции
        DB_INIT_ERROR = f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"


init_db()


@app.middleware("http")
async def require_db(request: Request, call_next):
    if DB_INIT_ERROR and request.url.path != "/health":
        init_db()
        if DB_INIT_ERROR:
            return HTMLResponse(f"<h3>База данных недоступна</h3><p>{DB_INIT_ERROR}</p>"
                                f"<p>Проверьте DATABASE_URL. Подробности: /health</p>", status_code=503)
    return await call_next(request)


app.middleware("http")(csrf_cookie_middleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.get("/health", tags=["Служебное"], summary="Состояние БД и переменных окружения")
def health():
    return {
        "db": "ok" if not DB_INIT_ERROR else DB_INIT_ERROR,
        "database_url_set": bool(find_database_url()),
        "secret_key_set": config.SECRET_KEY != "dev-only-secret-change-me",
        "admin_env_set": bool(config.ADMIN_EMAIL and config.ADMIN_PASSWORD),
        "demo_mode": config.DEMO_MODE,
    }


# --- Ошибки ---
def _error_page(status: int, title: str, text: str):
    return HTMLResponse(
        f'<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><title>{title}</title>'
        f'<link href="/static/css/app.css" rel="stylesheet"></head><body>'
        f'<main class="auth-main" style="min-height: 100vh;"><div class="auth-form"><h1>{title}</h1>'
        f'<p class="muted">{text}</p><a class="btn btn-secondary" href="/dashboard">В кабинет</a></div></main>'
        f'</body></html>', status_code=status)


@app.exception_handler(StarletteHTTPException)
async def http_errors(request: Request, exc: StarletteHTTPException):
    # Без входа (или с истёкшей сессией) страницы перекидывают на вход, а не показывают JSON-ошибку
    if exc.status_code == 401 and request.method == "GET":
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response
    if exc.status_code == 403 and exc.detail == "CSRF":
        return _error_page(403, "Форма устарела", "Обновите страницу и отправьте форму ещё раз.")
    return await http_exception_handler(request, exc)


@app.exception_handler(NotFound)
async def not_found(request: Request, exc: NotFound):
    return _error_page(404, "Не найдено", "Объект не существует или недоступен в текущем статусе.")


@app.exception_handler(Forbidden)
async def forbidden(request: Request, exc: Forbidden):
    return _error_page(403, "Нет доступа", "У вас нет доступа к этой странице.")


@app.exception_handler(Invalid)
async def invalid(request: Request, exc: Invalid):
    return _error_page(400, "Действие недоступно", exc.message)
