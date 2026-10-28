"""Вход, регистрация юрлиц, выход, общий вход в кабинет и демо-вход."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from liquidity import config, i18n, models
from liquidity.database import get_db
from liquidity.routers import borrower, creditor
from liquidity.security import end_session, get_current_user, require_role, start_session
from liquidity.services import Invalid, accounts, analytics, company_lookup, demo, fx, throttle
from liquidity.web import redirect, render

router = APIRouter(tags=["Вход и регистрация"])
# Код ошибки регистрации -> поле формы, под которым её показать
FIELD_BY_ERROR = {"company": "company_name", "inn": "registration_number", "regnum": "registration_number",
                  "country": "country_code", "password": "password", "exists": "email"}


@router.get("/", summary="Главная страница (лендинг)")
def landing(request: Request, db: Session = Depends(get_db)):
    rates = fx.current_rates(db)
    calc = {"rates": {k: float(v) for k, v in rates["rates"].items()} if rates else {},
            "rates_date": rates["date"].strftime("%d.%m.%Y") if rates else None,
            "market": analytics.market_rates(db), "platform_commission": config.PLATFORM_COMMISSION,
            "currencies": config.CURRENCIES, "max_term": config.MAX_TERM_MONTHS, "lang": i18n.pick(request)}
    return render(request, "landing.html", calc=calc, stats=analytics.public_stats(db))


@router.get("/security", summary="Безопасность и документы")
def security_page(request: Request):
    return render(request, "security.html", kyc_docs=config.KYC_DOCS, sla=config.SLA_HOURS)


@router.get("/login", summary="Страница входа")
def login_page(request: Request):
    return render(request, "login.html")


@router.get("/privacy", summary="Политика конфиденциальности")
def privacy(request: Request):
    return render(request, "legal.html", page="privacy")


@router.get("/terms", summary="Условия использования")
def terms(request: Request):
    return render(request, "legal.html", page="terms")


@router.post("/login", summary="Вход по email и паролю")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    ip = throttle.client_ip(request, config.ON_VERCEL)
    left = throttle.locked_for(db, email, ip)
    if left:
        minutes = max(1, -(-int(left.total_seconds()) // 60))
        return render(request, "login.html", status_code=429, email=email,
                      error=f"Слишком много неудачных попыток. Вход временно заблокирован, повторите через {minutes} мин.")
    user = accounts.authenticate(db, email, password)
    if not user:
        remaining = throttle.register_failure(db, email, ip)
        hint = f" Осталось попыток: {remaining}." if remaining else " Вход заблокирован на 15 минут."
        return render(request, "login.html", status_code=400, email=email,
                      error="Неверный email или пароль." + (hint if remaining is not None and remaining < 3 else ""))
    throttle.register_success(db, email)
    response = redirect("/dashboard")
    start_session(response, user)
    return response


@router.get("/logout", summary="Выход")
def logout():
    response = redirect("/")
    end_session(response)
    return response


@router.get("/register", summary="Форма регистрации юрлица")
def register_page(request: Request):
    role = "creditor" if request.query_params.get("role") == "creditor" else "borrower"
    return render(request, "register.html", form={"role": role}, field_errors={},
                  creditor_countries=config.CREDITOR_COUNTRIES, inn_lookup=company_lookup.enabled())


@router.post("/register", summary="Регистрация заёмщика (РФ) или кредитора (зарубежье)")
def register(request: Request, email: str = Form(...), password: str = Form(...), role: str = Form(...),
             company_name: str = Form(""), registration_number: str = Form(""), country_code: str = Form(""),
             db: Session = Depends(get_db)):
    form = {"email": email, "role": role, "company_name": company_name,
            "registration_number": registration_number, "country_code": country_code}
    try:
        accounts.register_company(db, email=email, password=password, role=role, company_name=company_name,
                                  registration_number=registration_number, country_code=country_code)
    except Invalid as e:
        field = FIELD_BY_ERROR.get(e.code)
        return render(request, "register.html", status_code=400, error=None if field else e.message,
                      field_errors={field: e.message} if field else {}, form=form,
                      creditor_countries=config.CREDITOR_COUNTRIES, inn_lookup=company_lookup.enabled())
    return redirect("/login?registered=1")


@router.get("/dashboard", summary="Кабинет: открывается по роли пользователя")
def dashboard(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "borrower":
        return borrower.dashboard(request, user, db)
    if user.role == "creditor":
        return creditor.dashboard(request, user, db)
    return redirect("/admin" if user.role == "admin" else "/operator/offers")


# --- Демо-режим (только при DEMO_MODE=1) ---
def require_demo():
    if not config.DEMO_MODE:
        raise HTTPException(404)


@router.post("/demo/login/{role}", summary="Демо: вход в роль одной кнопкой", dependencies=[Depends(require_demo)])
def demo_login(role: str, db: Session = Depends(get_db)):
    if role not in demo.DEMO_LOGINS:
        raise HTTPException(404)
    demo.ensure_seeded(db)
    user = db.query(models.User).filter(models.User.email == demo.DEMO_LOGINS[role]).first()
    response = redirect("/dashboard")
    start_session(response, user)
    return response


@router.post("/demo/reset", summary="Демо: пересоздать демо-данные", dependencies=[Depends(require_demo)])
def demo_reset(user: models.User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    was_demo_admin = demo.is_demo_email(user.email)
    demo.reset(db)
    response = redirect("/admin?demo_reset=1")
    if was_demo_admin:  # демо-админ пересоздан с новым id — выдаём сессию заново, чтобы не выкинуло на вход
        start_session(response, db.query(models.User).filter(models.User.email == demo.DEMO_LOGINS["admin"]).first())
    return response


@router.get("/api/company", summary="Компания по ИНН из ЕГРЮЛ (DaData) для автозаполнения регистрации")
def company_by_inn(inn: str, request: Request):
    if not company_lookup.enabled():
        return JSONResponse({"enabled": False})
    inn = inn.strip()
    if not accounts.inn_is_valid(inn):
        return JSONResponse({"enabled": True, "found": False, "error": "inn"}, status_code=400)
    # Не даём перебирать ИНН через наш ключ DaData
    if not company_lookup.allow(throttle.client_ip(request, config.ON_VERCEL)):
        return JSONResponse({"enabled": True, "found": False, "error": "busy"}, status_code=429)
    company = company_lookup.find_by_inn(inn)
    return JSONResponse({"enabled": True, "found": bool(company), **(company or {})})


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    """Service worker отдаётся с корня сайта, чтобы управлять всеми страницами (PWA)."""
    return FileResponse(f"{config.STATIC_DIR}/sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(f"{config.STATIC_DIR}/manifest.webmanifest", media_type="application/manifest+json")


@router.get("/offline", include_in_schema=False)
def offline(request: Request):
    return render(request, "offline.html")


@router.get("/lang/{code}", summary="Язык интерфейса: ru или en (английский — для гостей и кредиторов)")
def set_language(code: str, next: str = "/"):
    if code not in ("ru", "en"):
        raise HTTPException(404)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = redirect(target)
    response.set_cookie("lang", code, max_age=365 * 24 * 3600, samesite="lax", secure=config.SECURE_COOKIES)
    return response
