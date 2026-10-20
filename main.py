from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import jwt
import os

from database import engine, Base, get_db
import models

# Автоматически создаем таблицы в PostgreSQL при запуске
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Платформа ликвидности Сбер")
templates = Jinja2Templates(directory="templates")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "sber_hackathon_secret_key_12345")


def hash_password(password: str):
    return pwd_context.hash(password)


def create_token(user_id: int, role: str):
    return jwt.encode({"user_id": user_id, "role": role}, SECRET_KEY, algorithm="HS256")


def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user = db.query(models.User).filter(models.User.id == payload["user_id"]).first()
        if not user:
            raise HTTPException(status_code=401)
        return user
    except:
        raise HTTPException(status_code=401, detail="Invalid token")


# --- Главная страница (Логин) ---
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={}
    )


# --- Страница регистрации ---
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={}
    )


@app.post("/register")
def register_user(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        role: str = Form(...),
        db: Session = Depends(get_db)
):
    existing_user = db.query(models.User).filter(models.User.email == email).first()
    if existing_user:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Пользователь с таким email уже существует"}
        )

    new_user = models.User(
        email=email,
        password_hash=hash_password(password),
        role=role
    )
    db.add(new_user)
    db.commit()

    return RedirectResponse(url="/", status_code=303)

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response

# --- Обработка выхода---
@app.get("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response

# --- Обработка входа ---
@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not pwd_context.verify(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверный email или пароль"}
        )

    token = create_token(user.id, user.role)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True)
    return response


# --- Личный кабинет (разделение по ролям) ---
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "borrower":
        apps = db.query(models.Application).filter(models.Application.user_id == user.id).all()
        return templates.TemplateResponse(
            request=request, name="borrower.html", context={"user": user, "apps": apps}
        )
    elif user.role == "creditor":
        apps = db.query(models.Application).filter(models.Application.status == "published").all()
        return templates.TemplateResponse(
            request=request, name="creditor.html", context={"user": user, "apps": apps}
        )

    elif user.role == "admin":
        return RedirectResponse(url="/admin", status_code=303)

    return HTMLResponse("Роль не распознана")


# --- Действия Заемщика (Создать заявку) ---
@app.post("/create_application")
def create_application(amount: float = Form(...), currency: str = Form(...), country: str = Form(...),
                       purpose: str = Form(...), user: models.User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)
    new_app = models.Application(user_id=user.id, funding_amount=amount, currency_code=currency,
                                 target_jurisdiction=country, purpose=purpose)
    db.add(new_app)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


# --- Действия Кредитора (Отправить оффер) ---
@app.post("/create_offer/{app_id}")
def create_offer(app_id: int, amount: float = Form(...), rate: float = Form(...), lender_comm: float = Form(...),
                 user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "creditor": raise HTTPException(403)
    total_comm = lender_comm + 0.8  # 0.8 - комиссия платформы
    new_offer = models.Offer(application_id=app_id, user_id=user.id, funding_amount=amount, annual_interest_rate=rate,
                             lender_commission=lender_comm, total_commission=total_comm)
    db.add(new_offer)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


# --- Принятие оффера Заемщиком ---
@app.post("/accept_offer/{offer_id}")
def accept_offer(offer_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)
    offer = db.query(models.Offer).filter(models.Offer.id == offer_id).first()
    if not offer: raise HTTPException(404)

    offer.status = "accepted"
    app = db.query(models.Application).filter(models.Application.id == offer.application_id).first()
    app.status = "in_work"
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


# --- Страница просмотра офферов для Заемщика ---
@app.get("/applications/{app_id}/offers", response_class=HTMLResponse)
def view_offers(app_id: int, request: Request, user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    # Проверяем, что это заемщик
    if user.role != "borrower":
        raise HTTPException(403, detail="Доступ только для заемщиков")

    # Ищем заявку и проверяем, что она принадлежит этому заемщику
    application = db.query(models.Application).filter(
        models.Application.id == app_id,
        models.Application.user_id == user.id
    ).first()

    if not application:
        raise HTTPException(404, detail="Заявка не найдена")

    # Достаем все офферы по этой заявке
    offers = db.query(models.Offer).filter(models.Offer.application_id == app_id).all()

    return templates.TemplateResponse(
        request=request,
        name="offers.html",
        context={"application": application, "offers": offers, "user": user}
    )


# --- Кабинет Администратора (Аналитика и Модерация) ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Проверка роли
    if user.role != "admin":
        raise HTTPException(403, detail="Доступ только для администратора")

    # Получаем все заявки
    all_apps = db.query(models.Application).order_by(models.Application.created_at.desc()).all()
    all_offers = db.query(models.Offer).all()
    all_users = db.query(models.User).all()

    # Считаем базовую аналитику (требование кейса)
    total_apps = len(all_apps)
    total_offers = len(all_offers)
    # Средняя сумма (если заявок нет, то 0)
    avg_amount = sum([float(a.funding_amount) for a in all_apps]) / total_apps if total_apps > 0 else 0

    # Топ стран (простая группировка)
    from collections import Counter
    countries = Counter([a.target_jurisdiction for a in all_apps])
    top_countries = countries.most_common(3)  # Топ-3 страны

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "user": user,
            "apps": all_apps,
            "offers": all_offers,
            "users": all_users,
            "total_apps": total_apps,
            "total_offers": total_offers,
            "avg_amount": round(avg_amount, 2),
            "top_countries": top_countries
        }
    )


# --- Экшн: Изменить статус заявки (Модерация) ---
@app.post("/admin/application/{app_id}/status")
def admin_change_app_status(app_id: int, new_status: str = Form(...), user: models.User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if app:
        app.status = new_status
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)