"""KYC/AML: пакет документов компании и решение администратора по всему пакету."""
import re

from fastapi import UploadFile
from sqlalchemy.orm import Session

from liquidity import models
from liquidity.services import Invalid, NotFound
from liquidity.services.files import save_upload
from liquidity.services.notifications import notify, notify_staff


def is_approved(db: Session, user_id: int) -> bool:
    return db.query(models.Document).filter(
        models.Document.user_id == user_id, models.Document.status == "approved"
    ).count() > 0


def upload_document(db: Session, user: models.User, doc_type: str, file: UploadFile):
    if not re.fullmatch(r"[a-z_]{1,50}", doc_type):
        raise Invalid("doc_type", "Неверный тип документа")
    path = save_upload(db, file, f"{user.id}_{doc_type}")
    db.add(models.Document(user_id=user.id, doc_type=doc_type, file_path=path, status="uploaded"))
    db.commit()


def submit_for_review(db: Session, user: models.User):
    for doc in db.query(models.Document).filter(models.Document.user_id == user.id).all():
        doc.status = "pending"
    notify_staff(db, f"{user.display_name} отправил документы на проверку KYC", "/admin#kyc", roles=("admin",))
    db.commit()


def review_queue(db: Session):
    """[(компания, [документы на проверке])] в порядке поступления."""
    queue = {}
    for doc in db.query(models.Document).filter(models.Document.status == "pending").order_by(models.Document.id):
        queue.setdefault(doc.user, []).append(doc)
    return list(queue.items())


def approve(db: Session, target_user_id: int):
    target = db.get(models.User, target_user_id)
    if not target:
        raise NotFound()
    for doc in db.query(models.Document).filter(models.Document.user_id == target_user_id,
                                                models.Document.status == "pending"):
        doc.status = "approved"
    target.kyc_reject_reason = None
    notify(db, target.id, "Верификация пройдена. " + ("Можно создавать заявки" if target.role == "borrower"
                                                      else "Можно отправлять предложения"), "/dashboard", "ok")
    db.commit()


def reject(db: Session, target_user_id: int, reason: str):
    """Отклонённый пакет удаляется: компания видит причину и загружает документы заново."""
    target = db.get(models.User, target_user_id)
    if not target:
        raise NotFound()
    reason = reason.strip()[:500]
    if not reason:
        raise Invalid("reason", "Укажите причину отказа")
    for doc in db.query(models.Document).filter(models.Document.user_id == target_user_id,
                                                models.Document.status == "pending").all():
        db.query(models.StoredFile).filter(models.StoredFile.path == doc.file_path).delete()
        db.delete(doc)
    target.kyc_reject_reason = reason
    notify(db, target.id, f"Верификация отклонена: {reason}. Загрузите документы заново", "/kyc", "bad")
    db.commit()
