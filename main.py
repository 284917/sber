from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import jwt
import os
import shutil

from database import engine, Base, get_db
import models

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Платформа ликвидности Сбер")
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

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
    return templates.TemplateResponse(request=request, name="login.html", context={})


# --- Страница регистрации ---
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={})


@app.post("/register")
def register_user(request: Request, email: str = Form(...), password: str = Form(...), role: str = Form(...),
                  db: Session = Depends(get_db)):
    existing_user = db.query(models.User).filter(models.User.email == email).first()
    if existing_user:
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Пользователь уже существует"})

    new_user = models.User(email=email, password_hash=hash_password(password), role=role)
    db.add(new_user)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


# --- Выход из системы ---
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
        return templates.TemplateResponse(request=request, name="login.html",
                                          context={"error": "Неверный email или пароль"})

    token = create_token(user.id, user.role)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True)
    return response


# --- Личный кабинет (разделение по ролям) ---
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "borrower":
        apps = db.query(models.Application).filter(models.Application.user_id == user.id).all()
        user_docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
        return templates.TemplateResponse(request=request, name="borrower.html",
                                          context={"user": user, "apps": apps, "user_docs": user_docs})

    elif user.role == "creditor":
        apps = db.query(models.Application).filter(models.Application.status == "published").all()
        user_docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
        return templates.TemplateResponse(request=request, name="creditor.html",
                                          context={"user": user, "apps": apps, "user_docs": user_docs})

    elif user.role in ["admin", "operator"]:
        return RedirectResponse(url="/admin", status_code=303)
    return HTMLResponse("Роль не распознана")


# --- Действия Заемщика (Создать заявку) ---
@app.post("/create_application")
def create_application(amount: float = Form(...), currency: str = Form(...), country: str = Form(...),
                       purpose: str = Form(...), user: models.User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)

    # ПРОВЕРКА KYC
    approved_docs = db.query(models.Document).filter(
        models.Document.user_id == user.id,
        models.Document.status == "approved"
    ).count()

    if approved_docs == 0:
        raise HTTPException(400, detail="Вы не можете создать заявку, пока ваша компания не прошла верификацию (KYC).")

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
    total_comm = lender_comm + 0.8
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
    if user.role != "borrower": raise HTTPException(403)
    application = db.query(models.Application).filter(models.Application.id == app_id,
                                                      models.Application.user_id == user.id).first()
    if not application: raise HTTPException(404)
    offers = db.query(models.Offer).filter(models.Offer.application_id == app_id).all()
    return templates.TemplateResponse(request=request, name="offers.html",
                                      context={"application": application, "offers": offers, "user": user})


# --- KYC: Страница верификации ---
@app.get("/kyc", response_class=HTMLResponse)
def kyc_page(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    user_docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
    return templates.TemplateResponse(request=request, name="kyc.html", context={"user": user, "user_docs": user_docs})


# --- KYC: Загрузка файлов ---
@app.post("/kyc/upload")
async def upload_kyc(file: UploadFile = File(...), doc_type: str = Form(...),
                     user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    file_location = f"{UPLOAD_DIR}/{user.id}_{doc_type}_{file.filename}"
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    new_doc = models.Document(user_id=user.id, doc_type=doc_type, file_path=file_location, status="uploaded")
    db.add(new_doc)
    db.commit()
    return RedirectResponse(url="/kyc", status_code=303)


# --- KYC: Отправка на проверку ---
@app.post("/kyc/submit")
def submit_kyc(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
    for doc in docs:
        doc.status = "pending"
    db.commit()
    return RedirectResponse(url="/kyc?success=1", status_code=303)


# --- Кабинет Администратора ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)

    all_apps = db.query(models.Application).order_by(models.Application.created_at.desc()).all()
    all_offers = db.query(models.Offer).all()
    pending_docs = db.query(models.Document).filter(models.Document.status == "pending").all()

    total_apps = len(all_apps)
    total_offers = len(all_offers)
    avg_amount = sum([float(a.funding_amount) for a in all_apps]) / total_apps if total_apps > 0 else 0

    from collections import Counter
    countries = Counter([a.target_jurisdiction for a in all_apps])
    top_countries = countries.most_common(3)

    return templates.TemplateResponse(
        request=request, name="admin.html",
        context={
            "user": user, "apps": all_apps, "offers": all_offers,
            "total_apps": total_apps, "total_offers": total_offers,
            "avg_amount": round(avg_amount, 2), "top_countries": top_countries,
            "pending_docs": pending_docs
        }
    )


# --- Модерация заявок ---
@app.post("/admin/application/{app_id}/status")
def admin_change_app_status(app_id: int, new_status: str = Form(...), user: models.User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)
    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if app:
        app.status = new_status
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# --- Создание сотрудника ---
@app.post("/admin/create_user")
def admin_create_user(email: str = Form(...), password: str = Form(...), role: str = Form(...),
                      user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing: return RedirectResponse(url="/admin?error=exists", status_code=303)

    new_staff = models.User(email=email, password_hash=hash_password(password), role=role)
    db.add(new_staff)
    db.commit()
    return RedirectResponse(url="/admin?success=created", status_code=303)


# --- KYC: Одобрение ---
@app.post("/admin/kyc/approve_user/{target_user_id}")
def approve_user_kyc(target_user_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)
    docs = db.query(models.Document).filter(models.Document.user_id == target_user_id,
                                            models.Document.status == "pending").all()
    for doc in docs:
        doc.status = "approved"
    db.commit()
    return RedirectResponse(url="/admin?kyc_approved=1", status_code=303)