"""Сделка: доступ участников, чат, договор, проверки и подтверждение оператором."""
from datetime import datetime

from fastapi import UploadFile
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Forbidden, Invalid, NotFound, kyc
from liquidity.services.events import log_event
from liquidity.services.files import save_upload
from liquidity.services.notifications import notify, notify_staff


def accepted_offer(db: Session, app_id: int):
    return db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                         models.Offer.status == "accepted").first()


def get_for_user(db: Session, user: models.User, app_id: int) -> models.Application:
    """Сделку видят только заёмщик, принятый кредитор и сотрудники."""
    application = db.get(models.Application, app_id)
    if not application:
        raise NotFound()
    offer = accepted_offer(db, app_id)
    is_participant = user.id == application.user_id or (offer is not None and offer.user_id == user.id)
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
        "creditor_kyc": bool(offer) and kyc.is_approved(db, offer.user_id),
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
    other_documents = [m for m in messages if m.file_path and not is_contract(m)]
    return messages, main_contract, other_documents


def messages_after(db: Session, user: models.User, app_id: int, after_id: int):
    """Новые сообщения после after_id — для подгрузки без перезагрузки страницы."""
    get_for_user(db, user, app_id)
    return db.query(models.Message).filter(models.Message.application_id == app_id,
                                           models.Message.id > after_id).order_by(models.Message.id).all()


def _participants(db: Session, application):
    offer = accepted_offer(db, application.id)
    return [application.user_id] + ([offer.user_id] if offer else [])


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
    log_event(db, app_id, operator, "Сделка подтверждена оператором: статус «Согласовано», сделка в архиве")
    for uid in _participants(db, application):
        notify(db, uid, f"Сделка №{app_id} согласована. Сводка в PDF готова", f"/deal/{app_id}", "ok")
    db.commit()
