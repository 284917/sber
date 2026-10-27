from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import jwt
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote
import mimetypes
import os
import re
import uuid

from database import engine, Base, get_db, find_database_url
import models

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Платформа ликвидности Сбер")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
# Bootstrap лежит в проекте, чтобы вёрстка не зависела от доступности CDN
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# Без входа (или с истёкшей сессией) страницы перекидывают на вход, а не показывают JSON-ошибку
@app.exception_handler(StarletteHTTPException)
async def redirect_unauthenticated(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 401 and request.method == "GET":
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie("access_token")
        return response
    return await http_exception_handler(request, exc)

# Новые файлы хранятся в БД (таблица stored_files), чтобы работать и на Vercel без диска.
# Папка uploads нужна только для старых файлов, загруженных до этого изменения.
UPLOAD_DIR = "uploads"

# Роли, которые можно выбрать при самостоятельной регистрации
PUBLIC_ROLES = {"borrower", "creditor"}
STAFF_ROLES = {"admin", "operator"}

# --- Справочники ---
CURRENCIES = ["EUR", "USD", "CNY"]
# Куда заёмщик получает средства (как на макете в Miro)
TARGET_COUNTRIES = {"DE": "Германия", "TR": "Турция", "CN": "Китай"}
# Откуда могут быть кредиторы
CREDITOR_COUNTRIES = {"CY": "Кипр", "AE": "ОАЭ", "CN": "Китай", "TR": "Турция", "DE": "Германия",
                      "KZ": "Казахстан", "HK": "Гонконг", "IN": "Индия"}
COUNTRY_NAMES = {"RU": "Россия", **CREDITOR_COUNTRIES, **TARGET_COUNTRIES}
TRANSFER_METHODS = {"swift": "SWIFT", "spfs": "СПФС", "cips": "CIPS", "agent": "Платёжный агент"}
MAX_TERM_MONTHS = 120

APPLICATION_STATUSES = {"moderation": "На модерации", "rejected": "Отклонена", "published": "Опубликована",
                        "in_work": "В работе", "agreed": "Согласовано"}
OFFER_STATUSES = {"pending": "На проверке у оператора", "shown": "Получено заёмщиком", "hidden": "Скрыто оператором",
                  "accepted": "Принято", "rejected": "Отклонено"}
ROLE_NAMES = {"borrower": "Заёмщик", "creditor": "Кредитор", "admin": "Администратор", "operator": "Оператор"}


def format_money(value):
    """500000 -> «500 000», 612500.5 -> «612 500,50» (неразрывные пробелы между разрядами)."""
    if value is None:
        return "—"
    value = Decimal(str(value)).quantize(Decimal("0.01"))
    whole, frac = f"{value:.2f}".split(".")
    whole = f"{int(whole):,}".replace(",", " ")
    return whole if frac == "00" else f"{whole},{frac}"


def format_percent(value):
    if value is None:
        return "—"
    return f"{Decimal(str(value)).normalize():f}".replace(".", ",") + "%"


templates.env.filters["money"] = format_money
templates.env.filters["percent"] = format_percent
templates.env.globals.update(
    APPLICATION_STATUSES=APPLICATION_STATUSES, OFFER_STATUSES=OFFER_STATUSES, COUNTRY_NAMES=COUNTRY_NAMES,
    TRANSFER_METHODS=TRANSFER_METHODS, ROLE_NAMES=ROLE_NAMES,
)

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
            db.add(models.User(email=email, password_hash=hash_password(password), role="admin",
                               company_name="Администрация платформы"))
            db.commit()
    finally:
        db.close()


# create_all не добавляет колонки в уже существующие таблицы, поэтому новые поля дописываются здесь
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS country_code VARCHAR(2)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS registration_number VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_reject_reason VARCHAR(500)",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS term_months INTEGER",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS transfer_method VARCHAR(30)",
    "ALTER TABLE applications ADD COLUMN IF NOT EXISTS moderation_comment VARCHAR(500)",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS extra_terms VARCHAR(1000)",
]


def run_migrations():
    if engine.dialect.name != "postgresql":
        return  # в SQLite (тесты, локальный запуск) таблицы создаются сразу с новыми колонками
    with engine.begin() as conn:
        for statement in MIGRATIONS:
            conn.execute(sql_text(statement))


# Если БД недоступна, приложение не падает целиком: ошибка видна на /health и на страницах (503)
DB_INIT_ERROR = None


def init_db():
    global DB_INIT_ERROR
    try:
        Base.metadata.create_all(bind=engine)
        run_migrations()
        ensure_admin()
        DB_INIT_ERROR = None
    except Exception as e:
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


@app.get("/health")
def health():
    return {
        "db": "ok" if not DB_INIT_ERROR else DB_INIT_ERROR,
        "database_url_set": bool(find_database_url()),
        "secret_key_set": bool(os.getenv("SECRET_KEY")),
        "admin_env_set": bool(os.getenv("ADMIN_EMAIL") and os.getenv("ADMIN_PASSWORD")),
    }


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


def is_kyc_approved(user_id: int, db: Session) -> bool:
    return db.query(models.Document).filter(
        models.Document.user_id == user_id, models.Document.status == "approved"
    ).count() > 0


def log_event(db: Session, application_id: int, actor: models.User, text: str):
    db.add(models.ApplicationEvent(application_id=application_id, actor_id=actor.id if actor else None, text=text))


def get_events(db: Session, application_id: int):
    return db.query(models.ApplicationEvent).filter(
        models.ApplicationEvent.application_id == application_id
    ).order_by(models.ApplicationEvent.created_at, models.ApplicationEvent.id).all()


def parse_float(value: str):
    """Пустое поле фильтра -> None; «100 000,5» -> 100000.5."""
    if value is None or not value.strip():
        return None
    try:
        return float(value.replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


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


# --- Регистрация юрлица ---
def register_context(**extra):
    return {"creditor_countries": CREDITOR_COUNTRIES, **extra}


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context=register_context(form={}))


@app.post("/register")
def register_user(request: Request, email: str = Form(...), password: str = Form(...), role: str = Form(...),
                  company_name: str = Form(""), registration_number: str = Form(""), country_code: str = Form(""),
                  db: Session = Depends(get_db)):
    form = {"email": email, "role": role, "company_name": company_name,
            "registration_number": registration_number, "country_code": country_code}

    def fail(message, status_code=400):
        return templates.TemplateResponse(request=request, name="register.html",
                                          context=register_context(error=message, form=form), status_code=status_code)

    if role not in PUBLIC_ROLES:
        return fail("Недопустимая роль")
    company_name, registration_number = company_name.strip(), registration_number.strip()
    if not company_name or len(company_name) > 255:
        return fail("Укажите название компании")
    if len(password) < 8:
        return fail("Пароль должен быть не короче 8 символов")
    if role == "borrower":
        # Заёмщик — российское юрлицо: ИНН из 10 цифр
        if not re.fullmatch(r"\d{10}", registration_number):
            return fail("ИНН юрлица должен состоять из 10 цифр")
        country_code = "RU"
    else:
        if country_code not in CREDITOR_COUNTRIES:
            return fail("Выберите страну регистрации кредитора")
        if not registration_number or len(registration_number) > 50:
            return fail("Укажите регистрационный номер компании")

    if db.query(models.User).filter(models.User.email == email).first():
        return fail("Пользователь с таким email уже существует")

    db.add(models.User(email=email, password_hash=hash_password(password), role=role, company_name=company_name,
                       registration_number=registration_number, country_code=country_code))
    db.commit()
    return RedirectResponse(url="/?registered=1", status_code=303)


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
        apps = db.query(models.Application).filter(models.Application.user_id == user.id).order_by(
            models.Application.created_at.desc(), models.Application.id.desc()).all()
        shown_counts = Counter(o.application_id for o in db.query(models.Offer).filter(
            models.Offer.application_id.in_([a.id for a in apps] or [0]), models.Offer.status == "shown"))
        user_docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
        return templates.TemplateResponse(request=request, name="borrower.html", context={
            "user": user, "apps": apps, "user_docs": user_docs, "shown_counts": shown_counts,
            "currencies": CURRENCIES, "countries": TARGET_COUNTRIES, "max_term": MAX_TERM_MONTHS})

    elif user.role == "creditor":
        # Фильтры по стране, валюте, сумме и сроку (из задания)
        qp = request.query_params
        filters = {"country": qp.get("country", ""), "currency": qp.get("currency", ""),
                   "amount_min": qp.get("amount_min", ""), "amount_max": qp.get("amount_max", ""),
                   "term_max": qp.get("term_max", "")}
        query = db.query(models.Application).filter(models.Application.status == "published")
        if filters["country"] in TARGET_COUNTRIES:
            query = query.filter(models.Application.target_jurisdiction == filters["country"])
        if filters["currency"] in CURRENCIES:
            query = query.filter(models.Application.currency_code == filters["currency"])
        amount_min, amount_max = parse_float(filters["amount_min"]), parse_float(filters["amount_max"])
        term_max = parse_float(filters["term_max"])
        if amount_min is not None:
            query = query.filter(models.Application.funding_amount >= amount_min)
        if amount_max is not None:
            query = query.filter(models.Application.funding_amount <= amount_max)
        if term_max is not None:
            query = query.filter(models.Application.term_months <= term_max)
        available_apps = query.order_by(models.Application.created_at.desc(), models.Application.id.desc()).all()

        my_offers = db.query(models.Offer).filter(models.Offer.user_id == user.id).order_by(
            models.Offer.created_at.desc(), models.Offer.id.desc()).all()
        offered_app_ids = {o.application_id for o in my_offers if o.status in ("pending", "shown", "accepted")}
        my_deals = db.query(models.Application).join(models.Offer).filter(
            models.Offer.user_id == user.id,
            models.Offer.status == "accepted",
            models.Application.status.in_(["in_work", "agreed"])
        ).all()
        user_docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
        return templates.TemplateResponse(request=request, name="creditor.html", context={
            "user": user, "available_apps": available_apps, "my_deals": my_deals, "my_offers": my_offers,
            "offered_app_ids": offered_app_ids, "user_docs": user_docs, "filters": filters,
            "currencies": CURRENCIES, "countries": TARGET_COUNTRIES})

    elif user.role == "admin":
        return RedirectResponse(url="/admin", status_code=303)

    elif user.role == "operator":
        return RedirectResponse(url="/operator/offers", status_code=303)

    return HTMLResponse("Роль не распознана")


# --- Действия Заемщика (Создать заявку) ---
@app.post("/create_application")
def create_application(amount: float = Form(...), currency: str = Form(...), country: str = Form(...),
                       purpose: str = Form(...), term_months: int = Form(...), transfer_method: str = Form(...),
                       user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)
    if not is_kyc_approved(user.id, db):
        return RedirectResponse(url="/dashboard?error=kyc", status_code=303)

    purpose = purpose.strip()
    if (amount <= 0 or currency not in CURRENCIES or country not in TARGET_COUNTRIES or not purpose
            or len(purpose) > 255 or not (1 <= term_months <= MAX_TERM_MONTHS)
            or transfer_method not in TRANSFER_METHODS):
        return RedirectResponse(url="/dashboard?error=form", status_code=303)

    new_app = models.Application(user_id=user.id, funding_amount=amount, currency_code=currency,
                                 target_jurisdiction=country, purpose=purpose, term_months=term_months,
                                 transfer_method=transfer_method, status="moderation")
    db.add(new_app)
    db.flush()
    log_event(db, new_app.id, user, "Заявка создана и отправлена на модерацию")
    db.commit()
    return RedirectResponse(url="/dashboard?created=1", status_code=303)


# --- Действия Кредитора (Отправить оффер) ---
@app.post("/create_offer/{app_id}")
def create_offer(app_id: int, amount: float = Form(...), rate: float = Form(...), lender_comm: float = Form(...),
                 extra_terms: str = Form(""), user: models.User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    if user.role != "creditor": raise HTTPException(403)
    if not is_kyc_approved(user.id, db):
        return RedirectResponse(url="/dashboard?error=kyc", status_code=303)

    # Проверки из макета: заявка открыта, сумма не больше запрошенной, ставка больше 0
    application = db.query(models.Application).filter(models.Application.id == app_id).first()
    if not application or application.status != "published": raise HTTPException(404)
    extra_terms = extra_terms.strip()
    if (not (0 < amount <= float(application.funding_amount)) or not (0 < rate < 100)
            or not (0 <= lender_comm < 100) or len(extra_terms) > 1000):
        return RedirectResponse(url="/dashboard?error=offer", status_code=303)
    already = db.query(models.Offer).filter(
        models.Offer.application_id == app_id, models.Offer.user_id == user.id,
        models.Offer.status.in_(["pending", "shown", "accepted"])).first()
    if already:
        return RedirectResponse(url="/dashboard?error=duplicate", status_code=303)

    total_comm = lender_comm + 0.8
    new_offer = models.Offer(
        application_id=app_id, user_id=user.id, funding_amount=amount,
        annual_interest_rate=rate, lender_commission=lender_comm,
        total_commission=total_comm, extra_terms=extra_terms or None, status="pending"
    )
    db.add(new_offer)
    # Имя кредитора в истории не раскрываем: до проверки оператором оффер видит только оператор
    log_event(db, app_id, user, "Получено новое предложение, ждёт проверки оператором")
    db.commit()
    return RedirectResponse(url="/dashboard?offer_sent=1", status_code=303)


def get_own_offer_for_decision(offer_id: int, user: models.User, db: Session):
    if user.role != "borrower": raise HTTPException(403)
    offer = db.query(models.Offer).filter(models.Offer.id == offer_id).first()
    if not offer: raise HTTPException(404)
    app = db.query(models.Application).filter(models.Application.id == offer.application_id).first()
    # Решать можно только по показанному оператором офферу на свою ещё открытую заявку
    if not app or app.user_id != user.id: raise HTTPException(404)
    if offer.status != "shown" or app.status != "published":
        raise HTTPException(400, detail="По этому предложению решение уже принято")
    return offer, app


# --- Принятие оффера Заемщиком ---
@app.post("/accept_offer/{offer_id}")
def accept_offer(offer_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    offer, app = get_own_offer_for_decision(offer_id, user, db)

    offer.status = "accepted"
    app.status = "in_work"
    db.query(models.Offer).filter(
        models.Offer.application_id == app.id,
        models.Offer.id != offer.id,
        models.Offer.status.in_(["pending", "shown"])
    ).update({"status": "rejected"}, synchronize_session=False)
    log_event(db, app.id, user, f"Принято предложение {offer.sender.display_name}, сделка в работе")
    db.commit()
    return RedirectResponse(url=f"/deal/{app.id}", status_code=303)


# --- Отклонение оффера Заемщиком ---
@app.post("/reject_offer/{offer_id}")
def reject_offer(offer_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    offer, app = get_own_offer_for_decision(offer_id, user, db)
    offer.status = "rejected"
    log_event(db, app.id, user, f"Отклонено предложение {offer.sender.display_name}")
    db.commit()
    return RedirectResponse(url=f"/applications/{app.id}/offers", status_code=303)


# --- Карточка заявки: предложения и история (для заёмщика) ---
@app.get("/applications/{app_id}/offers", response_class=HTMLResponse)
def view_offers(app_id: int, request: Request, user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    if user.role != "borrower": raise HTTPException(403)
    application = db.query(models.Application).filter(models.Application.id == app_id,
                                                      models.Application.user_id == user.id).first()
    if not application: raise HTTPException(404)

    # Заёмщик видит только офферы, прошедшие оператора: показанные и уже решённые им самим
    offers = db.query(models.Offer).filter(
        models.Offer.application_id == app_id,
        models.Offer.status.in_(["shown", "accepted", "rejected"]),
        models.Offer.operator_decision_at.isnot(None)
    ).order_by(models.Offer.annual_interest_rate).all()
    return templates.TemplateResponse(request=request, name="offers.html", context={
        "application": application, "offers": offers, "user": user, "events": get_events(db, app_id)})


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
def build_analytics(apps, offers):
    total_apps, total_offers = len(apps), len(offers)
    with_deal = sum(1 for a in apps if a.status in ("in_work", "agreed"))

    # Средний размер считается отдельно по каждой валюте: складывать EUR, USD и CNY нельзя
    by_currency = defaultdict(list)
    for a in apps:
        by_currency[a.currency_code].append(float(a.funding_amount))
    avg_by_currency = [{"currency": cur, "count": len(vals), "avg": sum(vals) / len(vals), "total": sum(vals)}
                       for cur, vals in sorted(by_currency.items(), key=lambda kv: -len(kv[1]))]

    # Спрос: куда заёмщики хотят получить средства. Предложение: из каких стран кредиторы делают офферы
    demand = Counter(a.target_jurisdiction for a in apps).most_common(5)
    supply = Counter((o.sender.country_code if o.sender and o.sender.country_code else "—")
                     for o in offers).most_common(5)
    return {"total_apps": total_apps, "total_offers": total_offers,
            "deal_share": round(100 * with_deal / total_apps) if total_apps else 0,
            "avg_by_currency": avg_by_currency, "top_demand": demand, "top_supply": supply,
            "status_counts": Counter(a.status for a in apps)}


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)

    all_apps = db.query(models.Application).order_by(models.Application.created_at.desc(),
                                                     models.Application.id.desc()).all()
    all_offers = db.query(models.Offer).all()
    pending_docs = db.query(models.Document).filter(models.Document.status == "pending").all()
    kyc_queue = defaultdict(list)
    for doc in pending_docs:
        kyc_queue[doc.user].append(doc)

    accepted_by_app = {o.application_id: o for o in all_offers if o.status == "accepted"}
    archive = [{"app": a, "offer": accepted_by_app.get(a.id),
                "confirmed_by": db.get(models.User, a.confirmed_by) if a.confirmed_by else None}
               for a in all_apps if a.status == "agreed"]

    return templates.TemplateResponse(request=request, name="admin.html", context={
        "user": user, "apps": all_apps, "kyc_queue": list(kyc_queue.items()),
        "moderation_apps": [a for a in all_apps if a.status == "moderation"],
        "archive": archive, "analytics": build_analytics(all_apps, all_offers)})


# --- Модерация заявок ---
@app.post("/admin/application/{app_id}/moderate")
def admin_moderate_application(app_id: int, decision: str = Form(...), comment: str = Form(""),
                               user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if not app or app.status != "moderation": raise HTTPException(404)
    comment = comment.strip()[:500]

    if decision == "approve":
        app.status = "published"
        app.moderation_comment = None
        log_event(db, app.id, user, "Заявка прошла модерацию и опубликована для кредиторов")
    elif decision == "reject":
        if not comment:
            return RedirectResponse(url="/admin?error=reason#moderation", status_code=303)
        app.status = "rejected"
        app.moderation_comment = comment
        log_event(db, app.id, user, f"Заявка отклонена модератором: {comment}")
    else:
        raise HTTPException(400)
    db.commit()
    return RedirectResponse(url="/admin?moderated=1#moderation", status_code=303)


# --- Создание сотрудника ---
@app.post("/admin/create_user")
def admin_create_user(email: str = Form(...), password: str = Form(...), role: str = Form(...),
                      user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    if role not in STAFF_ROLES: raise HTTPException(400, detail="Недопустимая роль")
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing: return RedirectResponse(url="/admin?error=exists#staff", status_code=303)

    new_staff = models.User(email=email, password_hash=hash_password(password), role=role,
                            company_name="Администрация платформы")
    db.add(new_staff)
    db.commit()
    return RedirectResponse(url="/admin?success=created#staff", status_code=303)


# --- KYC: Одобрение и отклонение (решение по всему пакету документов компании) ---
@app.post("/admin/kyc/approve_user/{target_user_id}")
def approve_user_kyc(target_user_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    target = db.get(models.User, target_user_id)
    if not target: raise HTTPException(404)
    docs = db.query(models.Document).filter(models.Document.user_id == target_user_id,
                                            models.Document.status == "pending").all()
    for doc in docs:
        doc.status = "approved"
    target.kyc_reject_reason = None
    db.commit()
    return RedirectResponse(url="/admin?kyc_approved=1#kyc", status_code=303)


@app.post("/admin/kyc/reject_user/{target_user_id}")
def reject_user_kyc(target_user_id: int, reason: str = Form(""), user: models.User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    target = db.get(models.User, target_user_id)
    if not target: raise HTTPException(404)
    reason = reason.strip()[:500]
    if not reason:
        return RedirectResponse(url="/admin?error=reason#kyc", status_code=303)
    # Отклонённый пакет удаляется: компания видит причину и загружает документы заново
    docs = db.query(models.Document).filter(models.Document.user_id == target_user_id,
                                            models.Document.status == "pending").all()
    for doc in docs:
        db.query(models.StoredFile).filter(models.StoredFile.path == doc.file_path).delete()
        db.delete(doc)
    target.kyc_reject_reason = reason
    db.commit()
    return RedirectResponse(url="/admin?kyc_rejected=1#kyc", status_code=303)


# --- ОПЕРАТОР: Офферы на проверке ---
@app.get("/operator/offers", response_class=HTMLResponse)
def operator_offers(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in STAFF_ROLES: raise HTTPException(403)

    pending_offers = db.query(models.Offer).filter(models.Offer.status == "pending").all()
    offers_data = []
    for offer in pending_offers:
        app = db.query(models.Application).filter(models.Application.id == offer.application_id).first()
        if app:
            problems = []
            if offer.funding_amount > app.funding_amount:
                problems.append("сумма больше запрошенной")
            if offer.annual_interest_rate <= 0:
                problems.append("ставка не больше 0")
            if app.status != "published":
                problems.append("заявка уже не открыта")
            offers_data.append({"offer": offer, "app": app, "is_valid": not problems, "problems": problems})

    return templates.TemplateResponse(
        request=request, name="operator_offers.html",
        context={"user": user, "offers_data": offers_data}
    )


# --- ОПЕРАТОР: Мои сделки (в работе) ---
@app.get("/operator/deals", response_class=HTMLResponse)
def operator_deals(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in STAFF_ROLES: raise HTTPException(403)

    deals = db.query(models.Application).filter(models.Application.status == "in_work").all()
    return templates.TemplateResponse(
        request=request, name="operator_deals.html",
        context={"user": user, "deals": deals}
    )


# --- ОПЕРАТОР: Действия с оффером ---
def get_pending_offer(offer_id: int, user: models.User, db: Session) -> models.Offer:
    if user.role not in STAFF_ROLES: raise HTTPException(403)
    offer = db.query(models.Offer).filter(models.Offer.id == offer_id).first()
    if not offer or offer.status != "pending": raise HTTPException(404)
    return offer


@app.post("/operator/offer/{offer_id}/show")
def operator_show_offer(offer_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    offer = get_pending_offer(offer_id, user, db)
    offer.status = "shown"
    offer.operator_decision_at = datetime.utcnow()
    log_event(db, offer.application_id, user, f"Оператор показал заёмщику предложение {offer.sender.display_name}")
    db.commit()
    return RedirectResponse(url="/operator/offers", status_code=303)


@app.post("/operator/offer/{offer_id}/hide")
def operator_hide_offer(offer_id: int, reason: str = Form("Скрыт оператором"),
                        user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    offer = get_pending_offer(offer_id, user, db)
    offer.status = "hidden"
    offer.hide_reason = (reason.strip() or "Скрыт оператором")[:255]
    offer.operator_decision_at = datetime.utcnow()
    log_event(db, offer.application_id, user, f"Оператор скрыл предложение {offer.sender.display_name}")
    db.commit()
    return RedirectResponse(url="/operator/offers", status_code=303)


# --- ОПЕРАТОР: Экран проверки конкретной сделки ---
def contract_uploaded(app_id: int, db: Session) -> bool:
    return db.query(models.Message).filter(
        models.Message.application_id == app_id,
        models.Message.text.like("%основной договор%"),
        models.Message.file_path.isnot(None)
    ).first() is not None


def deal_checks(app: models.Application, offer, db: Session):
    return {
        "contract": contract_uploaded(app.id, db),
        "borrower_kyc": is_kyc_approved(app.user_id, db),
        "creditor_kyc": bool(offer) and is_kyc_approved(offer.user_id, db),
    }


@app.get("/operator/deal/{app_id}", response_class=HTMLResponse)
def operator_deal(app_id: int, request: Request, user: models.User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    if user.role not in STAFF_ROLES: raise HTTPException(403)

    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if not app: raise HTTPException(404)

    offer = db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                          models.Offer.status == "accepted").first()
    checks = deal_checks(app, offer, db)
    return templates.TemplateResponse(request=request, name="operator_deal.html", context={
        "user": user, "app": app, "offer": offer, "checks": checks,
        "can_confirm": app.status == "in_work" and all(checks.values()), "events": get_events(db, app_id)})


# --- ОПЕРАТОР: Подтвердить сделку ---
@app.post("/operator/deal/{app_id}/confirm")
def operator_confirm_deal(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in STAFF_ROLES: raise HTTPException(403)

    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if not app or app.status != "in_work": raise HTTPException(404)
    offer = db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                          models.Offer.status == "accepted").first()
    # Подтвердить можно только при загруженном договоре и одобренном KYC обеих сторон
    if not all(deal_checks(app, offer, db).values()):
        return RedirectResponse(url=f"/operator/deal/{app_id}?error=checks", status_code=303)

    app.status = "agreed"
    app.confirmed_by = user.id
    app.confirmed_at = datetime.utcnow()
    log_event(db, app.id, user, "Сделка подтверждена оператором: статус «Согласовано», сделка в архиве")
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
        text=(text[:1000] if text else "Прикреплен документ"), file_path=file_location
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
    log_event(db, app_id, user, f"{user.display_name} загрузил основной договор займа")
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
        text="Запрошено подключение оператора. Ожидайте ответа."
    )
    db.add(sys_msg)
    log_event(db, app_id, user, f"{user.display_name} позвал оператора в сделку")
    db.commit()
    return RedirectResponse(url=f"/deal/{app_id}", status_code=303)
