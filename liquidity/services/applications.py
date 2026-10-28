"""Заявки на ликвидность: создание, модерация, поиск для кредиторов и этапы."""
from datetime import date, timedelta

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid, NotFound, kyc
from liquidity.services.audit import record
from liquidity.services.events import log_event
from liquidity.services.notifications import notify, notify_staff

WITHDRAWABLE = ("moderation", "published")

STAGES = [("Модерация", "Проверка заявки администратором"), ("Публикация", "Заявку видят кредиторы"),
          ("Предложения", "Выбор лучшего предложения"), ("Сделка", "Обсуждение условий в чате"),
          ("Договор", "Подписанный договор загружен"), ("Согласовано", "Подтверждено оператором")]


def create(db: Session, borrower: models.User, *, amount: float, currency: str, country: str, purpose: str,
           term_months: int, transfer_method: str) -> models.Application:
    """Новая заявка уходит на модерацию; создать её может только верифицированный заёмщик."""
    if not kyc.is_approved(db, borrower.id):
        raise Invalid("kyc")
    purpose = purpose.strip()
    if (amount <= 0 or currency not in config.CURRENCIES or country not in config.TARGET_COUNTRIES
            or not purpose or len(purpose) > 255 or not (1 <= term_months <= config.MAX_TERM_MONTHS)
            or transfer_method not in config.TRANSFER_METHODS):
        raise Invalid("form")
    application = models.Application(user_id=borrower.id, funding_amount=amount, currency_code=currency,
                                     target_jurisdiction=country, purpose=purpose, term_months=term_months,
                                     transfer_method=transfer_method, status="moderation")
    db.add(application)
    db.flush()
    log_event(db, application.id, borrower, "Заявка создана и отправлена на модерацию")
    notify_staff(db, f"Заявка №{application.id} от {borrower.display_name} ждёт модерации", "/operator/moderation",
                 roles=("admin", "operator"))
    db.commit()
    return application


def moderate(db: Session, admin: models.User, app_id: int, decision: str, comment: str = ""):
    application = db.get(models.Application, app_id)
    if not application or application.status != "moderation":
        raise NotFound()
    comment = comment.strip()[:500]
    if decision == "approve":
        application.status = "published"
        application.moderation_comment = None
        application.valid_until = date.today() + timedelta(days=config.APP_VALID_DAYS)
        record(db, admin, f"Опубликовал заявку №{app_id}", f"/operator/deal/{app_id}")
        log_event(db, app_id, admin, "Заявка прошла модерацию и опубликована для кредиторов")
        notify(db, application.user_id, f"Заявка №{app_id} опубликована, кредиторы её видят",
               f"/applications/{app_id}/offers", "ok")
        from liquidity.services.saved_searches import notify_matching  # здесь, чтобы не было циклического импорта
        notify_matching(db, application)
    elif decision == "reject":
        if not comment:
            raise Invalid("reason")
        application.status = "rejected"
        application.moderation_comment = comment
        record(db, admin, f"Отклонил заявку №{app_id}: {comment}", f"/operator/deal/{app_id}")
        log_event(db, app_id, admin, f"Заявка отклонена модератором: {comment}")
        notify(db, application.user_id, f"Заявка №{app_id} отклонена: {comment}", f"/applications/{app_id}/offers", "bad")
    else:
        raise Invalid("decision")
    db.commit()


def withdraw(db: Session, borrower: models.User, app_id: int) -> models.Application:
    """Заёмщик снимает заявку, пока по ней нет сделки; открытые предложения закрываются."""
    application = get_own(db, borrower, app_id)
    if application.status not in WITHDRAWABLE:
        raise Invalid("withdraw", "Заявку в работе или согласованную отозвать нельзя")
    application.status = "withdrawn"
    for offer in db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                               models.Offer.status.in_(["pending", "shown", "accepted"])).all():
        offer.status = "rejected"
        notify(db, offer.user_id, f"Заёмщик отозвал заявку №{app_id}, ваше предложение закрыто",
               "/dashboard#my-offers", "warn")
    log_event(db, app_id, borrower, "Заёмщик отозвал заявку, открытые предложения закрыты")
    db.commit()
    return application


def unpublish(db: Session, staff: models.User, app_id: int, reason: str):
    """Сотрудник снимает опубликованную заявку (например, по жалобе или ошибке в данных).
    Заявка получает статус «Отклонена» с причиной, открытые предложения закрываются."""
    application = db.get(models.Application, app_id)
    if not application or application.status != "published":
        raise NotFound()
    reason = reason.strip()[:500]
    if not reason:
        raise Invalid("reason", "Укажите причину")
    application.status = "rejected"
    application.moderation_comment = reason
    for offer in db.query(models.Offer).filter(models.Offer.application_id == app_id,
                                               models.Offer.status.in_(["pending", "shown", "accepted"])).all():
        offer.status = "rejected"
        notify(db, offer.user_id, f"Заявка №{app_id} снята с публикации, ваше предложение закрыто", "/dashboard#my-offers", "warn")
    record(db, staff, f"Снял с публикации заявку №{app_id}: {reason}", f"/operator/deal/{app_id}")
    log_event(db, app_id, staff, f"Заявка снята с публикации: {reason}")
    notify(db, application.user_id, f"Заявка №{app_id} снята с публикации: {reason}", f"/applications/{app_id}/offers", "bad")
    db.commit()


def for_borrower(db: Session, borrower: models.User):
    return db.query(models.Application).filter(models.Application.user_id == borrower.id).order_by(
        models.Application.created_at.desc(), models.Application.id.desc()).all()


def get_own(db: Session, borrower: models.User, app_id: int) -> models.Application:
    application = db.query(models.Application).filter(models.Application.id == app_id,
                                                      models.Application.user_id == borrower.id).first()
    if not application:
        raise NotFound()
    return application


def parse_number(value):
    """Пустое поле фильтра -> None; «100 000,5» -> 100000.5."""
    if value is None or not str(value).strip():
        return None
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def search_published(db: Session, filters: dict):
    """Опубликованные заявки для кредитора с фильтрами по стране, валюте, сумме и сроку."""
    query = db.query(models.Application).filter(models.Application.status == "published")
    if filters.get("country") in config.TARGET_COUNTRIES:
        query = query.filter(models.Application.target_jurisdiction == filters["country"])
    if filters.get("currency") in config.CURRENCIES:
        query = query.filter(models.Application.currency_code == filters["currency"])
    amount_min, amount_max = parse_number(filters.get("amount_min")), parse_number(filters.get("amount_max"))
    term_max = parse_number(filters.get("term_max"))
    if amount_min is not None:
        query = query.filter(models.Application.funding_amount >= amount_min)
    if amount_max is not None:
        query = query.filter(models.Application.funding_amount <= amount_max)
    if term_max is not None:
        query = query.filter(models.Application.term_months <= term_max)
    return query.order_by(models.Application.created_at.desc(), models.Application.id.desc()).all()


def stages(db: Session, application: models.Application, shown_offers: int = 0):
    """Шкала этапов заявки: для каждого этапа state = done / current / failed / todo."""
    from liquidity.services.deals import contract_uploaded

    status = application.status
    failed = status in ("rejected", "withdrawn", "expired")
    current = {"rejected": 0, "moderation": 0, "published": 2, "withdrawn": 2, "expired": 2,
               "agreed": len(STAGES)}.get(status)
    if status == "in_work":
        current = 5 if contract_uploaded(db, application.id) else 3

    result = []
    for i, (label, note) in enumerate(STAGES):
        state = "done" if i < current else ("failed" if failed else "current") if i == current else "todo"
        result.append({"label": label, "note": note, "state": state})
    if status == "published":
        from liquidity.services.offers import funded  # здесь, чтобы не было циклического импорта
        collected = funded(db, application.id)
        if collected:
            result[2]["note"] = f"Собрано {round(100 * collected / application.funding_amount)}% суммы"
        else:
            result[2]["note"] = f"Получено предложений: {shown_offers}" if shown_offers else "Ждём предложений кредиторов"
    if status == "expired":
        result[2]["note"] = "Срок публикации истёк"
    if status == "rejected":
        result[0]["note"] = "Отклонена: " + (application.moderation_comment or "без комментария")
    if status == "withdrawn":
        result[2]["note"] = "Заявка отозвана заёмщиком"
    if status == "in_work" and current == 5:
        result[5]["note"] = "Ждёт подтверждения оператором"
    return result
