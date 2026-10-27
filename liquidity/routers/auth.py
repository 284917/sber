"""Вход, регистрация юрлиц, выход, общий вход в кабинет и демо-вход."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.routers import borrower, creditor
from liquidity.security import end_session, get_current_user, require_role, start_session
from liquidity.services import Invalid, accounts, demo
from liquidity.web import redirect, render

router = APIRouter(tags=["Вход и регистрация"])


@router.get("/", summary="Страница входа")
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login", summary="Вход по email и паролю")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = accounts.authenticate(db, email, password)
    if not user:
        return render(request, "login.html", status_code=400, error="Неверный email или пароль")
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
    return render(request, "register.html", form={}, creditor_countries=config.CREDITOR_COUNTRIES)


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
        return render(request, "register.html", status_code=400, error=e.message, form=form,
                      creditor_countries=config.CREDITOR_COUNTRIES)
    return redirect("/?registered=1")


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
