"""Жалобы кредиторов на опубликованные заявки — проверка уже опубликованного вместо модерации каждой заявки.

Оператор либо снимает заявку с публикации (жалобы закрываются как принятые), либо отклоняет жалобу.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from liquidity import models
from liquidity.services import Invalid, NotFound
from liquidity.services.audit import record
from liquidity.services.notifications import notify, notify_staff


def create(db: Session, creditor: models.User, app_id: int, reason: str) -> models.Complaint:
    application = db.get(models.Application, app_id)
    if not application or application.status != "published":
        raise NotFound()
    reason = (reason or "").strip()[:500]
    if len(reason) < 5:
        raise Invalid("complaint_reason", "Опишите, что не так с заявкой")
    if db.query(models.Complaint).filter_by(application_id=app_id, user_id=creditor.id, status="open").first():
        raise Invalid("complaint_duplicate", "Вы уже пожаловались на эту заявку, оператор её проверяет")
    complaint = models.Complaint(application_id=app_id, user_id=creditor.id, reason=reason, status="open")
    db.add(complaint)
    notify_staff(db, f"Жалоба {creditor.display_name} на заявку №{app_id}: {reason}", "/operator/moderation#complaints",
                 "warn", roles=("admin", "operator"))
    db.commit()
    return complaint


def open_list(db: Session):
    """Открытые жалобы на заявки, которые всё ещё опубликованы."""
    return db.query(models.Complaint).join(models.Application).filter(
        models.Complaint.status == "open", models.Application.status == "published").order_by(models.Complaint.id).all()


def open_counts(db: Session) -> dict:
    counts = {}
    for c in open_list(db):
        counts[c.application_id] = counts.get(c.application_id, 0) + 1
    return counts


def complained_app_ids(db: Session, creditor: models.User) -> set:
    return {c.application_id for c in db.query(models.Complaint).filter_by(user_id=creditor.id, status="open")}


def _resolve(complaint: models.Complaint, staff: models.User, status: str):
    complaint.status = status
    complaint.resolved_by = staff.id
    complaint.resolved_at = datetime.utcnow()


def dismiss(db: Session, staff: models.User, complaint_id: int):
    """Заявка в порядке: жалоба закрывается, кредитор получает ответ."""
    complaint = db.get(models.Complaint, complaint_id)
    if not complaint or complaint.status != "open":
        raise NotFound()
    _resolve(complaint, staff, "dismissed")
    record(db, staff, f"Отклонил жалобу на заявку №{complaint.application_id}", f"/operator/deal/{complaint.application_id}")
    notify(db, complaint.user_id, f"Оператор проверил заявку №{complaint.application_id} по вашей жалобе "
                                  "и оставил её опубликованной", "/dashboard", "info")
    db.commit()


def close_for_application(db: Session, staff: models.User, application: models.Application):
    """Заявку сняли с публикации: открытые жалобы на неё приняты (коммит делает вызывающий)."""
    for complaint in db.query(models.Complaint).filter_by(application_id=application.id, status="open"):
        _resolve(complaint, staff, "accepted")
        notify(db, complaint.user_id, f"Заявка №{application.id} снята с публикации по вашей жалобе", "/dashboard", "ok")
