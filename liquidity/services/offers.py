"""Предложения кредиторов: отправка, проверка оператором, решение заёмщика."""
from collections import Counter
from datetime import datetime

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid, NotFound, kyc
from liquidity.services.events import log_event

ACTIVE_STATUSES = ("pending", "shown", "accepted")


def create(db: Session, creditor: models.User, app_id: int, *, amount: float, rate: float, lender_comm: float,
           extra_terms: str = "") -> models.Offer:
    if not kyc.is_approved(db, creditor.id):
        raise Invalid("kyc")
    application = db.get(models.Application, app_id)
    if not application or application.status != "published":
        raise NotFound()
    extra_terms = extra_terms.strip()
    # Проверки из макета: сумма не больше запрошенной, ставка больше 0
    if (not (0 < amount <= float(application.funding_amount)) or not (0 < rate < 100)
            or not (0 <= lender_comm < 100) or len(extra_terms) > 1000):
        raise Invalid("offer")
    if db.query(models.Offer).filter(models.Offer.application_id == app_id, models.Offer.user_id == creditor.id,
                                     models.Offer.status.in_(ACTIVE_STATUSES)).first():
        raise Invalid("duplicate")

    offer = models.Offer(application_id=app_id, user_id=creditor.id, funding_amount=amount,
                         annual_interest_rate=rate, lender_commission=lender_comm,
                         total_commission=lender_comm + config.PLATFORM_COMMISSION,
                         extra_terms=extra_terms or None, status="pending")
    db.add(offer)
    # Имя кредитора в истории не раскрываем: до проверки оператором оффер видит только оператор
    log_event(db, app_id, creditor, "Получено новое предложение, ждёт проверки оператором")
    db.commit()
    return offer


def withdraw(db: Session, creditor: models.User, offer_id: int):
    """Кредитор отзывает своё предложение, пока заёмщик его не принял."""
    offer = db.get(models.Offer, offer_id)
    if not offer or offer.user_id != creditor.id:
        raise NotFound()
    if offer.status not in ("pending", "shown"):
        raise Invalid("withdraw", "Отозвать можно только предложение, по которому ещё нет решения")
    offer.status = "withdrawn"
    log_event(db, offer.application_id, creditor,
              "Кредитор отозвал предложение" if offer.operator_decision_at is None
              else f"{creditor.display_name} отозвал предложение")
    db.commit()


def for_creditor(db: Session, creditor: models.User):
    return db.query(models.Offer).filter(models.Offer.user_id == creditor.id).order_by(
        models.Offer.created_at.desc(), models.Offer.id.desc()).all()


def visible_to_borrower(db: Session, app_id: int):
    """Заёмщик видит только офферы, прошедшие оператора: показанные и уже решённые им самим."""
    return db.query(models.Offer).filter(
        models.Offer.application_id == app_id,
        models.Offer.status.in_(["shown", "accepted", "rejected", "withdrawn"]),
        models.Offer.operator_decision_at.isnot(None),
    ).all()


def shown_counts(db: Session, app_ids):
    return Counter(o.application_id for o in db.query(models.Offer).filter(
        models.Offer.application_id.in_(list(app_ids) or [0]), models.Offer.status == "shown"))


def _own_shown_offer(db: Session, borrower: models.User, offer_id: int):
    offer = db.get(models.Offer, offer_id)
    if not offer:
        raise NotFound()
    application = db.get(models.Application, offer.application_id)
    if not application or application.user_id != borrower.id:
        raise NotFound()
    if offer.status != "shown" or application.status != "published":
        raise Invalid("decided", "По этому предложению решение уже принято")
    return offer, application


def accept(db: Session, borrower: models.User, offer_id: int) -> models.Application:
    """Принять можно один оффер: остальные по заявке отклоняются, заявка уходит в работу."""
    offer, application = _own_shown_offer(db, borrower, offer_id)
    offer.status = "accepted"
    application.status = "in_work"
    db.query(models.Offer).filter(
        models.Offer.application_id == application.id, models.Offer.id != offer.id,
        models.Offer.status.in_(["pending", "shown"]),
    ).update({"status": "rejected"}, synchronize_session=False)
    log_event(db, application.id, borrower, f"Принято предложение {offer.sender.display_name}, сделка в работе")
    db.commit()
    return application


def reject(db: Session, borrower: models.User, offer_id: int) -> models.Application:
    offer, application = _own_shown_offer(db, borrower, offer_id)
    offer.status = "rejected"
    log_event(db, application.id, borrower, f"Отклонено предложение {offer.sender.display_name}")
    db.commit()
    return application


# --- Оператор ---
def review_list(db: Session):
    """Офферы на проверке с подсказкой: что не так с условиями относительно заявки."""
    result = []
    for offer in db.query(models.Offer).filter(models.Offer.status == "pending").order_by(models.Offer.id):
        application = offer.application
        if not application:
            continue
        problems = []
        if offer.funding_amount > application.funding_amount:
            problems.append("сумма больше запрошенной")
        if offer.annual_interest_rate <= 0:
            problems.append("ставка не больше 0")
        if application.status != "published":
            problems.append("заявка уже не открыта")
        result.append({"offer": offer, "app": application, "is_valid": not problems, "problems": problems})
    return result


def _pending(db: Session, offer_id: int) -> models.Offer:
    offer = db.get(models.Offer, offer_id)
    if not offer or offer.status != "pending":
        raise NotFound()
    return offer


def show(db: Session, operator: models.User, offer_id: int):
    offer = _pending(db, offer_id)
    offer.status = "shown"
    offer.operator_decision_at = datetime.utcnow()
    log_event(db, offer.application_id, operator, f"Оператор показал заёмщику предложение {offer.sender.display_name}")
    db.commit()


def hide(db: Session, operator: models.User, offer_id: int, reason: str):
    offer = _pending(db, offer_id)
    offer.status = "hidden"
    offer.hide_reason = (reason.strip() or "Скрыт оператором")[:255]
    offer.operator_decision_at = datetime.utcnow()
    log_event(db, offer.application_id, operator, f"Оператор скрыл предложение {offer.sender.display_name}")
    db.commit()
