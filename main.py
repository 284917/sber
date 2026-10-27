from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import jwt
from datetime import datetime
from urllib.parse import quote
import mimetypes
import os
import re
import uuid

from database import engine, Base, get_db
import models

Base.metadata.create_all(bind=engine)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Платформа ликвидности Сбер")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Новые файлы хранятся в БД (таблица stored_files), чтобы работать и на Vercel без диска.
# Папка uploads нужна только для старых файлов, загруженных до этого изменения.
UPLOAD_DIR = "uploads"

# Роли, которые можно выбрать при самостоятельной регистрации
PUBLIC_ROLES = {"borrower", "creditor"}
STAFF_ROLES = {"admin", "operator"}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "sber_hackathon_secret_key_12345")


def hash_password(password: str):
    return pwd_context.hash(password)


def ensure_admin():
    """Создаёт первого админа из ADMIN_EMAIL / ADMIN_PASSWORD, если его ещё нет (нужно для пустой БД)."""
    email, password = os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PASSWORD")
    if not email or not password:
        return
    from database import SessionLocal
    db = SessionLocal()
    try:
        if not db.query(models.User).filter(models.User.email == email).first():
            db.add(models.User(email=email, password_hash=hash_password(password), role="admin"))
            db.commit()
    finally:
        db.close()


ensure_admin()


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


# --- Файлы: безопасное имя, содержимое сохраняется в БД ---
def save_upload(file: UploadFile, prefix: str, db: Session) -> str:
    original = os.path.basename((file.filename or "").replace("\\", "/"))
    safe_name = re.sub(r"[^\w.\-]", "_", original)[:100] or "file"
    file_location = f"{UPLOAD_DIR}/{prefix}_{uuid.uuid4().hex[:12]}_{safe_name}"
    db.add(models.StoredFile(path=file_location, content=file.file.read()))
    return file_location


# --- Сделка: доступ только у заёмщика, принятого кредитора и сотрудников ---
def get_deal_for_user(app_id: int, user: models.User, db: Session) -> models.Application:
    application = db.query(models.Application).filter(models.Application.id == app_id).first()
    if not application:
        raise HTTPException(404, detail="Заявка не найдена")

    is_borrower = (user.id == application.user_id)
    is_creditor = db.query(models.Offer).filter(
        models.Offer.application_id == app_id,
        models.Offer.user_id == user.id,
        models.Offer.status == "accepted"
    ).first() is not None
    is_staff = user.role in STAFF_ROLES

    if not (is_borrower or is_creditor or is_staff):
        raise HTTPException(403, detail="У вас нет доступа к этой сделке")
    return application


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
    if role not in PUBLIC_ROLES:
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Недопустимая роль"}, status_code=400)

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
        available_apps = db.query(models.Application).filter(models.Application.status == "published").all()
        my_deals = db.query(models.Application).join(models.Offer).filter(
            models.Offer.user_id == user.id,
            models.Offer.status == "accepted",
            models.Application.status.in_(["in_work", "agreed"])
        ).all()
        user_docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
        return templates.TemplateResponse(
            request=request, name="creditor.html",
            context={"user": user, "available_apps": available_apps, "my_deals": my_deals, "user_docs": user_docs}
        )

    elif user.role == "admin":
        return RedirectResponse(url="/admin", status_code=303)

    elif user.role == "operator":
        return RedirectResponse(url="/operator/offers", status_code=303)

    return HTMLResponse("Роль не распознана")


# --- Действия Заемщика (Создать заявку) ---
@app.post("/create_application")
def create_application(amount: float = Form(...), currency: str = Form(...), country: str = Form(...),
                       purpose: str = Form(...), user: models.User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)
    approved_docs = db.query(models.Document).filter(
        models.Document.user_id == user.id, models.Document.status == "approved"
    ).count()
    if approved_docs == 0:
        return RedirectResponse(url="/dashboard?error=kyc", status_code=303)

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
    approved_docs = db.query(models.Document).filter(
        models.Document.user_id == user.id, models.Document.status == "approved"
    ).count()
    if approved_docs == 0:
        return RedirectResponse(url="/dashboard?error=kyc", status_code=303)

    total_comm = lender_comm + 0.8
    new_offer = models.Offer(
        application_id=app_id, user_id=user.id, funding_amount=amount,
        annual_interest_rate=rate, lender_commission=lender_comm,
        total_commission=total_comm, status="pending"
    )
    db.add(new_offer)
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


# --- Принятие оффера Заемщиком ---
@app.post("/accept_offer/{offer_id}")
def accept_offer(offer_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)
    offer = db.query(models.Offer).filter(models.Offer.id == offer_id).first()
    if not offer: raise HTTPException(404)

    app = db.query(models.Application).filter(models.Application.id == offer.application_id).first()
    # Принять можно только показанный оператором оффер по своей ещё открытой заявке
    if not app or app.user_id != user.id: raise HTTPException(404)
    if offer.status != "shown" or app.status != "published":
        raise HTTPException(400, detail="Оффер нельзя принять")

    offer.status = "accepted"
    app.status = "in_work"
    db.query(models.Offer).filter(
        models.Offer.application_id == app.id,
        models.Offer.id != offer.id,
        models.Offer.status.in_(["pending", "shown"])
    ).update({"status": "rejected"}, synchronize_session=False)
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

    offers = db.query(models.Offer).filter(
        models.Offer.application_id == app_id,
        models.Offer.status == "shown"
    ).all()
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
    if not re.fullmatch(r"[a-z_]{1,50}", doc_type): raise HTTPException(400, detail="Неверный тип документа")
    file_location = save_upload(file, f"{user.id}_{doc_type}", db)

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
    if user.role != "admin": raise HTTPException(403)

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
    if user.role != "admin": raise HTTPException(403)
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
    if role not in STAFF_ROLES: raise HTTPException(400, detail="Недопустимая роль")
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


# --- ОПЕРАТОР: Офферы на проверке ---
@app.get("/operator/offers", response_class=HTMLResponse)
def operator_offers(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)

    pending_offers = db.query(models.Offer).filter(models.Offer.status == "pending").all()
    offers_data = []
    for offer in pending_offers:
        app = db.query(models.Application).filter(models.Application.id == offer.application_id).first()
        if app:
            is_valid = offer.funding_amount <= app.funding_amount and offer.annual_interest_rate > 0
            offers_data.append({"offer": offer, "app": app, "is_valid": is_valid})

    return templates.TemplateResponse(
        request=request, name="operator_offers.html",
        context={"user": user, "offers_data": offers_data}
    )


# --- ОПЕРАТОР: Мои сделки (в работе) ---
@app.get("/operator/deals", response_class=HTMLResponse)
def operator_deals(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)

    deals = db.query(models.Application).filter(models.Application.status == "in_work").all()
    return templates.TemplateResponse(
        request=request, name="operator_deals.html",
        context={"user": user, "deals": deals}
    )


# --- ОПЕРАТОР: Действия с оффером ---
@app.post("/operator/offer/{offer_id}/show")
def operator_show_offer(offer_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)
    offer = db.query(models.Offer).filter(models.Offer.id == offer_id).first()
    if offer:
        offer.status = "shown"
        offer.operator_decision_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/operator/offers", status_code=303)


@app.post("/operator/offer/{offer_id}/hide")
def operator_hide_offer(offer_id: int, reason: str = Form("Скрыт оператором"),
                        user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)
    offer = db.query(models.Offer).filter(models.Offer.id == offer_id).first()
    if offer:
        offer.status = "hidden"
        offer.hide_reason = reason
        offer.operator_decision_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/operator/offers", status_code=303)


# --- ОПЕРАТОР: Экран проверки конкретной сделки ---
@app.get("/operator/deal/{app_id}", response_class=HTMLResponse)
def operator_deal(app_id: int, request: Request, user: models.User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)

    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if not app: raise HTTPException(404)

    offer = db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                          models.Offer.status == "accepted").first()

    contract_uploaded = db.query(models.Message).filter(
        models.Message.application_id == app_id,
        models.Message.text.like("%основной договор%")
    ).first() is not None

    return templates.TemplateResponse(
        request=request, name="operator_deal.html",
        context={"user": user, "app": app, "offer": offer, "contract_uploaded": contract_uploaded}
    )


# --- ОПЕРАТОР: Подтвердить сделку ---
@app.post("/operator/deal/{app_id}/confirm")
def operator_confirm_deal(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in ["admin", "operator"]: raise HTTPException(403)

    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if app:
        app.status = "agreed"
        app.confirmed_by = user.id
        app.confirmed_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/operator/deals", status_code=303)


# --- Комната сделки (Чат) ---
@app.get("/deal/{app_id}", response_class=HTMLResponse)
def deal_room(app_id: int, request: Request, user: models.User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    application = get_deal_for_user(app_id, user, db)

    messages = db.query(models.Message).filter(models.Message.application_id == app_id).order_by(
        models.Message.created_at).all()

    main_contract = None
    other_documents = []

    for msg in messages:
        if msg.file_path:
            if "договор" in msg.text.lower() and not main_contract:
                main_contract = msg
            else:
                other_documents.append(msg)

    return templates.TemplateResponse(
        request=request, name="deal.html",
        context={
            "application": application,
            "messages": messages,
            "main_contract": main_contract,
            "other_documents": other_documents,
            "user": user
        }
    )


# --- Отправка сообщения ---
@app.post("/deal/{app_id}/send")
async def send_message(
        app_id: int, text: str = Form(None), file: UploadFile = File(None),
        user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    get_deal_for_user(app_id, user, db)
    file_location = None
    if file and file.filename:
        file_location = save_upload(file, f"deal_{app_id}", db)

    if not text and not file_location:
        return RedirectResponse(url=f"/deal/{app_id}", status_code=303)

    new_msg = models.Message(
        application_id=app_id, sender_id=user.id,
        text=text if text else "Прикреплен документ", file_path=file_location
    )
    db.add(new_msg)
    db.commit()
    return RedirectResponse(url=f"/deal/{app_id}", status_code=303)


# --- Загрузка ОСНОВНОГО договора ---
@app.post("/deal/{app_id}/upload_contract")
async def upload_contract(app_id: int, file: UploadFile = File(...), user: models.User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    get_deal_for_user(app_id, user, db)
    file_location = save_upload(file, f"contract_{app_id}", db)

    new_msg = models.Message(application_id=app_id, sender_id=user.id, text="Загружен основной договор займа",
                             file_path=file_location)
    db.add(new_msg)
    db.commit()
    return RedirectResponse(url=f"/deal/{app_id}", status_code=303)


# --- Скачивание файлов: только владелец документа, участники сделки и сотрудники ---
@app.get("/uploads/{filename}")
def download_file(filename: str, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if filename != os.path.basename(filename): raise HTTPException(404)
    file_location = f"{UPLOAD_DIR}/{filename}"

    doc = db.query(models.Document).filter(models.Document.file_path == file_location).first()
    msg = db.query(models.Message).filter(models.Message.file_path == file_location).first()
    if doc:
        if doc.user_id != user.id and user.role not in STAFF_ROLES: raise HTTPException(403)
    elif msg:
        get_deal_for_user(msg.application_id, user, db)
    else:
        raise HTTPException(404)

    stored = db.query(models.StoredFile).filter(models.StoredFile.path == file_location).first()
    if stored:
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return Response(content=stored.content, media_type=media_type,
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}"})

    if not os.path.isfile(file_location): raise HTTPException(404)
    return FileResponse(file_location, filename=filename, content_disposition_type="inline")


# --- Кнопка "Позвать оператора" ---
@app.post("/deal/{app_id}/call_operator")
def call_operator(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_deal_for_user(app_id, user, db)
    sys_msg = models.Message(
        application_id=app_id, sender_id=user.id,
        text="🔔 Запрошено подключение оператора. Ожидайте ответа."
    )
    db.add(sys_msg)
    db.commit()
    return RedirectResponse(url=f"/deal/{app_id}", status_code=303)