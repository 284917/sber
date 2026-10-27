"""Сделка: доступ участников, чат, договор, проверки и подтверждение оператором."""
from datetime import datetime

from fastapi import UploadFile
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Forbidden, Invalid, NotFound, kyc
from liquidity.services.events import log_event
from liquidity.services.files import save_upload


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


def room(db: Session, application: models.Application):
    """Сообщения чата и документы сделки: первый файл с «договор» в тексте — основной договор."""
    messages = db.query(models.Message).filter(models.Message.application_id == application.id).order_by(
        models.Message.created_at, models.Message.id).all()
    main_contract, other_documents = None, []
    for msg in messages:
        if msg.file_path:
            if "договор" in msg.text.lower() and not main_contract:
                main_contract = msg
            else:
                other_documents.append(msg)
    return messages, main_contract, other_documents


def send_message(db: Session, user: models.User, app_id: int, text: str, file: UploadFile = None):
    get_for_user(db, user, app_id)
    path = save_upload(db, file, f"deal_{app_id}") if file and file.filename else None
    text = (text or "").strip()
    if not text and not path:
        return
    db.add(models.Message(application_id=app_id, sender_id=user.id,
                          text=text[:1000] if text else "Прикреплён документ", file_path=path))
    db.commit()


def upload_contract(db: Session, user: models.User, app_id: int, file: UploadFile):
    get_for_user(db, user, app_id)
    path = save_upload(db, file, f"contract_{app_id}")
    db.add(models.Message(application_id=app_id, sender_id=user.id, text="Загружен основной договор займа",
                          file_path=path))
    log_event(db, app_id, user, f"{user.display_name} загрузил основной договор займа")
    db.commit()


def call_operator(db: Session, user: models.User, app_id: int):
    get_for_user(db, user, app_id)
    db.add(models.Message(application_id=app_id, sender_id=user.id,
                          text="Запрошено подключение оператора. Ожидайте ответа."))
    log_event(db, app_id, user, f"{user.display_name} позвал оператора в сделку")
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
    db.commit()
