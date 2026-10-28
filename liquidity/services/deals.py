"""Сделка: доступ участников, чат, договор, проверки и подтверждение оператором."""
import hashlib
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import UploadFile
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Forbidden, Invalid, NotFound, kyc
from liquidity.services.audit import record
from liquidity.services.events import log_event
from liquidity.services.files import save_upload
from liquidity.services.notifications import notify, notify_staff


def accepted_offers(db: Session, app_id: int):
    return db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                         models.Offer.status == "accepted").order_by(models.Offer.id).all()


def syndicate(offers):
    """Несколько принятых офферов как одно предложение: сумма складывается, ставка и комиссии —
    средневзвешенные по сумме. Проценты и комиссия синдиката в деньгах равны сумме по участникам."""
    total = sum((o.funding_amount for o in offers), Decimal(0))

    def weighted(attr):
        return (sum(Decimal(getattr(o, attr)) * o.funding_amount for o in offers) / total).quantize(Decimal("0.01"))

    names = ", ".join(o.sender.display_name for o in offers if o.sender)
    terms = "; ".join(f"{o.sender.display_name}: {o.extra_terms}" for o in offers if o.extra_terms and o.sender)
    return SimpleNamespace(
        id=None, user_id=None, status="accepted", parts=offers, funding_amount=total,
        annual_interest_rate=weighted("annual_interest_rate"), lender_commission=weighted("lender_commission"),
        total_commission=weighted("total_commission"), extra_terms=terms or None,
        sender=SimpleNamespace(id=None, display_name=f"Синдикат: {names}", country_code=None, registration_number=None))


def accepted_offer(db: Session, app_id: int):
    """Условия сделки: принятый оффер или синдикат из нескольких принятых."""
    offers = accepted_offers(db, app_id)
    if not offers:
        return None
    return offers[0] if len(offers) == 1 else syndicate(offers)


def lenders(offer):
    """[(кредитор, сумма)] — один кредитор или участники синдиката."""
    if offer is None:
        return []
    return [(o.sender, o.funding_amount) for o in getattr(offer, "parts", [offer])]


def get_for_user(db: Session, user: models.User, app_id: int) -> models.Application:
    """Сделку видят только заёмщик, принятые кредиторы и сотрудники."""
    application = db.get(models.Application, app_id)
    if not application:
        raise NotFound()
    creditor_ids = {o.user_id for o in accepted_offers(db, app_id)}
    is_participant = user.id == application.user_id or (
        user.id in creditor_ids and application.status in ("in_work", "agreed"))
    if not (is_participant or user.role in config.STAFF_ROLES):
        raise Forbidden()
    return application


def contract_uploaded(db: Session, app_id: int) -> bool:
    return db.query(models.Message).filter(
        models.Message.application_id == app_id,
        models.Message.text.like("%основной договор%"),
        models.Message.file_path.isnot(None),
    ).first() is not None


def checks(db: Session, application: models.Application, offer) -> dict:
    return {
        "borrower_kyc": kyc.is_approved(db, application.user_id),
        "creditor_kyc": bool(offer) and all(kyc.is_approved(db, o.user_id) for o in getattr(offer, "parts", [offer])),
        "contract": contract_uploaded(db, application.id),
    }


CONTRACT_TEXT = "Загружен основной договор займа"


def is_contract(msg) -> bool:
    """Основной договор — только файл, загруженный кнопкой «Загрузить договор». «Проект договора» из чата им не считается."""
    return bool(msg.file_path) and "основной договор" in (msg.text or "")


def room(db: Session, application: models.Application):
    """Сообщения чата и документы сделки: основной договор отдельно, остальные файлы из чата отдельно."""
    messages = db.query(models.Message).filter(models.Message.application_id == application.id).order_by(
        models.Message.created_at, models.Message.id).all()
    main_contract = next((m for m in reversed(messages) if is_contract(m)), None)
    if main_contract:
        stored = db.get(models.StoredFile, main_contract.file_path)
        main_contract.sha256 = hashlib.sha256(stored.content).hexdigest() if stored else None
    other_documents = [m for m in messages if m.file_path and not is_contract(m)]
    return messages, main_contract, other_documents


def messages_after(db: Session, user: models.User, app_id: int, after_id: int):
    """Новые сообщения после after_id — для подгрузки без перезагрузки страницы."""
    get_for_user(db, user, app_id)
    return db.query(models.Message).filter(models.Message.application_id == app_id,
                                           models.Message.id > after_id).order_by(models.Message.id).all()


def _participants(db: Session, application):
    return [application.user_id] + [o.user_id for o in accepted_offers(db, application.id)]


def send_message(db: Session, user: models.User, app_id: int, text: str, file: UploadFile = None):
    application = get_for_user(db, user, app_id)
    path = save_upload(db, file, f"deal_{app_id}") if file and file.filename else None
    text = (text or "").strip()
    if not text and not path:
        return None
    message = models.Message(application_id=app_id, sender_id=user.id,
                             text=text[:1000] if text else "Прикреплён документ", file_path=path)
    db.add(message)
    staff = user.role in config.STAFF_ROLES
    for uid in _participants(db, application):
        if uid != user.id:
            notify(db, uid, f"Оператор подключился к сделке №{app_id} и написал в чат" if staff
                   else f"Новое сообщение от {user.display_name} в сделке №{app_id}", f"/deal/{app_id}",
                   "warn" if staff else "info")
    db.commit()
    return message


def upload_contract(db: Session, user: models.User, app_id: int, file: UploadFile):
    application = get_for_user(db, user, app_id)
    if application.status != "in_work":
        raise Invalid("closed", "Сделка уже не в работе")
    if contract_uploaded(db, app_id):
        raise Invalid("contract_exists", "Договор уже загружен. Чтобы заменить его, сначала удалите текущий")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise Invalid("pdf", "Договор нужно загрузить в PDF")
    path = save_upload(db, file, f"contract_{app_id}")
    db.add(models.Message(application_id=app_id, sender_id=user.id, text=CONTRACT_TEXT, file_path=path))
    log_event(db, app_id, user, f"{user.display_name} загрузил основной договор займа")
    notify_staff(db, f"По сделке №{app_id} загружен договор, её можно подтвердить", f"/operator/deal/{app_id}")
    db.commit()


def delete_contract(db: Session, user: models.User, app_id: int):
    """Удалить загруженный договор, чтобы загрузить исправленный. Можно до подтверждения сделки:
    тому, кто загрузил, и сотрудникам платформы."""
    application = get_for_user(db, user, app_id)
    if application.status != "in_work":
        raise Invalid("closed", "Сделка уже подтверждена, договор изменить нельзя")
    contracts = [m for m in db.query(models.Message).filter(models.Message.application_id == app_id) if is_contract(m)]
    if not contracts:
        raise NotFound()
    if user.role not in config.STAFF_ROLES and any(m.sender_id != user.id for m in contracts):
        raise Forbidden()
    for message in contracts:
        db.query(models.StoredFile).filter(models.StoredFile.path == message.file_path).delete()
        db.delete(message)
    log_event(db, app_id, user, f"{user.display_name} удалил договор займа, нужно загрузить новый")
    record(db, user, f"Удалил договор в сделке №{app_id}", f"/operator/deal/{app_id}")
    for uid in _participants(db, application):
        if uid != user.id:
            notify(db, uid, f"{user.display_name} удалил договор в сделке №{app_id}, ждём новый", f"/deal/{app_id}", "warn")
    db.commit()


def call_operator(db: Session, user: models.User, app_id: int):
    get_for_user(db, user, app_id)
    db.add(models.Message(application_id=app_id, sender_id=user.id,
                          text="Запрошено подключение оператора. Ожидайте ответа."))
    log_event(db, app_id, user, f"{user.display_name} позвал оператора в сделку")
    notify_staff(db, f"{user.display_name} зовёт оператора в сделку №{app_id}", f"/deal/{app_id}", "warn")
    db.commit()


def in_work(db: Session):
    return db.query(models.Application).filter(models.Application.status == "in_work").order_by(
        models.Application.id).all()


def confirm(db: Session, operator: models.User, app_id: int):
    """Подтвердить можно только при загруженном договоре и одобренном KYC обеих сторон."""
    application = db.get(models.Application, app_id)
    if not application or application.status != "in_work":
        raise NotFound()
    if not all(checks(db, application, accepted_offer(db, app_id)).values()):
        raise Invalid("checks")
    application.status = "agreed"
    application.confirmed_by = operator.id
    application.confirmed_at = datetime.utcnow()
    record(db, operator, f"Подтвердил сделку №{app_id}", f"/operator/deal/{app_id}")
    log_event(db, app_id, operator, "Сделка подтверждена оператором: статус «Согласовано», сделка в архиве")
    for uid in _participants(db, application):
        notify(db, uid, f"Сделка №{app_id} согласована. Сводка в PDF готова", f"/deal/{app_id}", "ok")
    db.commit()
