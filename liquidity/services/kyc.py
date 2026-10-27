"""KYC/AML: пакет документов компании и решение администратора по всему пакету."""
import re

from fastapi import UploadFile
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid, NotFound
from liquidity.services.files import save_upload
from liquidity.services.notifications import notify, notify_staff


def is_approved(db: Session, user_id: int) -> bool:
    return db.query(models.Document).filter(
        models.Document.user_id == user_id, models.Document.status == "approved"
    ).count() > 0


def required_types(user: models.User):
    return [doc_type for doc_type, _ in config.KYC_DOCS.get(user.role, [])]


def upload_document(db: Session, user: models.User, doc_type: str, file: UploadFile) -> models.Document:
    """Загружает документ пакета. Пока пакет не отправлен, документ можно заменить: старый файл удаляется."""
    if not re.fullmatch(r"[a-z_]{1,50}", doc_type) or doc_type not in required_types(user):
        raise Invalid("doc_type", "Неверный тип документа")
    name = (file.filename or "").lower()
    head = file.file.read(5)
    file.file.seek(0)
    if not name.endswith(".pdf") or head != b"%PDF-":
        raise Invalid("pdf", "Нужен файл в формате PDF")
    existing = db.query(models.Document).filter(models.Document.user_id == user.id,
                                                models.Document.doc_type == doc_type).all()
    if any(d.status in ("pending", "approved") for d in existing):
        raise Invalid("locked", "Документ уже на проверке или одобрен, заменить его нельзя")
    path = save_upload(db, file, f"{user.id}_{doc_type}")
    for old in existing:  # замена до отправки: прежний файл больше не нужен
        db.query(models.StoredFile).filter(models.StoredFile.path == old.file_path).delete()
        db.delete(old)
    document = models.Document(user_id=user.id, doc_type=doc_type, file_path=path, status="uploaded")
    db.add(document)
    db.commit()
    return document


def progress(db: Session, user: models.User):
    """(загружено, всего) по обязательным документам."""
    uploaded = {d.doc_type for d in db.query(models.Document).filter(models.Document.user_id == user.id)}
    required = required_types(user)
    return sum(1 for t in required if t in uploaded), len(required)


def submit_for_review(db: Session, user: models.User):
    done, total = progress(db, user)
    if done < total:
        raise Invalid("incomplete", f"Загружено {done} из {total} документов. Загрузите все, чтобы отправить пакет")
    docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
    if not any(d.status == "uploaded" for d in docs):
        raise Invalid("already", "Пакет уже отправлен на проверку")
    for doc in docs:
        if doc.status == "uploaded":
            doc.status = "pending"
    notify_staff(db, f"{user.display_name} отправил документы на проверку KYC", "/admin?tab=kyc", roles=("admin",))
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
