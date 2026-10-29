"""Предложения кредиторов: отправка с автоматической проверкой, ручная проверка подозрительных, решение заёмщика.

Заявку можно собрать из нескольких предложений (частичное финансирование): заёмщик принимает офферы,
пока сумма не покрыта, или начинает сделку с уже собранной частью.
"""
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid, NotFound, kyc, screening
from liquidity.services.audit import record
from liquidity.services.events import log_event
from liquidity.services.notifications import notify, notify_staff

ACTIVE_STATUSES = ("pending", "shown", "accepted")


def funded(db: Session, app_id: int) -> Decimal:
    """Сколько по заявке уже собрано принятыми предложениями."""
    return sum((o.funding_amount for o in db.query(models.Offer).filter(
        models.Offer.application_id == app_id, models.Offer.status == "accepted")), Decimal(0))


def funded_map(db: Session, app_ids) -> dict:
    result = {}
    for o in db.query(models.Offer).filter(models.Offer.application_id.in_(list(app_ids) or [0]),
                                           models.Offer.status == "accepted"):
        result[o.application_id] = result.get(o.application_id, Decimal(0)) + o.funding_amount
    return result


def remaining(db: Session, application: models.Application) -> Decimal:
    return max(Decimal(0), Decimal(application.funding_amount) - funded(db, application.id))


def min_offer(application: models.Application, left: Decimal) -> Decimal:
    """Минимальная сумма предложения: MIN_OFFER_SHARE% заявки, но не больше несобранного остатка."""
    share = (Decimal(application.funding_amount) * config.MIN_OFFER_SHARE / 100).quantize(Decimal("0.01"))
    return min(share, left)


def create(db: Session, creditor: models.User, app_id: int, *, amount: float, rate: float, lender_comm: float,
           extra_terms: str = "", valid_days: int = config.OFFER_VALID_DEFAULT) -> models.Offer:
    if not kyc.is_approved(db, creditor.id):
        raise Invalid("kyc")
    application = db.get(models.Application, app_id)
    if not application or application.status != "published":
        raise NotFound()
    extra_terms = extra_terms.strip()
    # Жёсткие правила: доля заявки, не больше несобранной части, ставка и комиссия в разумных пределах
    left = remaining(db, application)
    if 0 < amount < float(min_offer(application, left)):
        raise Invalid("offer_min", f"Минимальное предложение — {config.MIN_OFFER_SHARE}% суммы заявки")
    if not (0 < amount <= float(left)) or len(extra_terms) > 1000 or valid_days not in config.OFFER_VALID_CHOICES:
        raise Invalid("offer")
    screening.check_offer(rate, lender_comm, extra_terms)
    if db.query(models.Offer).filter(models.Offer.application_id == app_id, models.Offer.user_id == creditor.id,
                                     models.Offer.status.in_(ACTIVE_STATUSES)).first():
        raise Invalid("duplicate")

    flags = screening.offer_flags(db, application, rate)
    offer = models.Offer(application_id=app_id, user_id=creditor.id, funding_amount=amount,
                         annual_interest_rate=rate, lender_commission=lender_comm,
                         platform_commission=config.PLATFORM_COMMISSION,
                         total_commission=lender_comm + config.PLATFORM_COMMISSION,
                         extra_terms=extra_terms or None, status="pending",
                         valid_until=date.today() + timedelta(days=valid_days))
    db.add(offer)
    if flags:
        offer.review_reason = "; ".join(flags)[:255]
        # Имя кредитора в истории не раскрываем, пока оператор не показал оффер
        log_event(db, app_id, creditor, "Получено новое предложение, ждёт проверки оператором")
        notify_staff(db, f"Предложение {creditor.display_name} по заявке №{app_id} ждёт проверки: {offer.review_reason}",
                     "/operator/offers")
    else:
        db.flush()
        _show(db, offer, creditor, f"Предложение {creditor.display_name} прошло автоматическую проверку и показано заёмщику")
    db.commit()
    return offer


def _show(db: Session, offer: models.Offer, actor: models.User, event_text: str):
    """Предложение становится видно заёмщику (сразу после автопроверки или после оператора)."""
    offer.status = "shown"
    offer.operator_decision_at = datetime.utcnow()  # момент, когда оффер стал виден заёмщику
    log_event(db, offer.application_id, actor, event_text)
    notify(db, offer.application.user_id, f"Новое предложение от {offer.sender.display_name} по заявке "
                                          f"№{offer.application_id}", f"/applications/{offer.application_id}/offers", "ok")


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


def _start_deal(db: Session, borrower: models.User, application: models.Application):
    """Заявка уходит в работу с принятыми офферами; открытые предложения закрываются."""
    application.status = "in_work"
    accepted = []
    for other in db.query(models.Offer).filter(models.Offer.application_id == application.id).all():
        if other.status == "accepted":
            accepted.append(other)
        elif other.status in ("pending", "shown"):
            other.status = "rejected"
            notify(db, other.user_id, f"{borrower.display_name} выбрал другие предложения по заявке №{application.id}",
                   "/dashboard#my-offers", "warn")
    for offer in accepted:
        text = (f"{borrower.display_name} принял ваше предложение по заявке №{application.id}. Сделка в работе"
                if len(accepted) == 1 else
                f"Сделка по заявке №{application.id} в работе: синдикат из {len(accepted)} кредиторов")
        notify(db, offer.user_id, text, f"/deal/{application.id}", "ok")
    names = ", ".join(o.sender.display_name for o in accepted if o.sender)
    log_event(db, application.id, borrower, f"Сделка в работе с кредиторами: {names}" if len(accepted) > 1
              else f"Принято предложение {names}, сделка в работе")


def accept(db: Session, borrower: models.User, offer_id: int) -> models.Application:
    """Принять оффер. Если он покрывает остаток заявки — сделка начинается, остальные офферы отклоняются;
    если меньше остатка — заявка остаётся открытой, чтобы собрать остальную сумму."""
    offer, application = _own_shown_offer(db, borrower, offer_id)
    if offer.counter_status == "pending":
        raise Invalid("counter_pending", "Дождитесь ответа кредитора на встречное предложение")
    left = remaining(db, application)
    if offer.funding_amount > left:
        raise Invalid("exceeds", "Сумма предложения больше несобранной части заявки")
    offer.status = "accepted"
    if offer.funding_amount >= left:
        _start_deal(db, borrower, application)
    else:
        share = round(100 * (Decimal(application.funding_amount) - left + offer.funding_amount)
                      / Decimal(application.funding_amount))
        log_event(db, application.id, borrower,
                  f"Принято предложение {offer.sender.display_name} на часть суммы, собрано {share}%")
        notify(db, offer.user_id, f"{borrower.display_name} принял ваше предложение по заявке №{application.id}. "
                                  f"Собрано {share}% суммы, сделка начнётся, когда заёмщик закроет сбор",
               "/dashboard#my-offers", "ok")
    db.commit()
    return application


def start_deal(db: Session, borrower: models.User, app_id: int) -> models.Application:
    """Заёмщик начинает сделку с уже принятой частью суммы, не дожидаясь полного покрытия."""
    application = db.get(models.Application, app_id)
    if not application or application.user_id != borrower.id:
        raise NotFound()
    if application.status != "published" or funded(db, app_id) <= 0:
        raise Invalid("nothing", "Сначала примите хотя бы одно предложение")
    _start_deal(db, borrower, application)
    db.commit()
    return application


def counter(db: Session, borrower: models.User, offer_id: int, rate: float, comment: str = ""):
    """Встречное предложение: заёмщик просит снизить ставку. Одно на каждый оффер."""
    offer, application = _own_shown_offer(db, borrower, offer_id)
    if offer.counter_status is not None:
        raise Invalid("counter_used", "Встречное предложение по этому офферу уже отправлялось")
    if not (0 < rate < float(offer.annual_interest_rate)):
        raise Invalid("counter_rate", "Встречная ставка должна быть ниже текущей и больше 0")
    offer.counter_rate = rate
    offer.counter_comment = (comment or "").strip()[:500] or None
    offer.counter_status = "pending"
    log_event(db, application.id, borrower, f"Заёмщик предложил {offer.sender.display_name} ставку {rate:g}% "
                                            f"вместо {offer.annual_interest_rate:g}%")
    notify(db, offer.user_id, f"{borrower.display_name} предлагает ставку {rate:g}% вместо "
                              f"{offer.annual_interest_rate:g}% по заявке №{application.id}", "/dashboard#my-offers", "info")
    db.commit()


def answer_counter(db: Session, creditor: models.User, offer_id: int, agree: bool):
    offer = db.get(models.Offer, offer_id)
    if not offer or offer.user_id != creditor.id:
        raise NotFound()
    if offer.counter_status != "pending" or offer.status != "shown":
        raise Invalid("counter_closed", "Встречное предложение уже не актуально")
    application = offer.application
    if agree:
        old = offer.annual_interest_rate
        offer.annual_interest_rate = offer.counter_rate
        offer.counter_status = "accepted"
        log_event(db, offer.application_id, creditor, f"{creditor.display_name} согласился на ставку "
                                                      f"{offer.counter_rate:g}% вместо {old:g}%")
        notify(db, application.user_id, f"{creditor.display_name} согласился на ставку {offer.counter_rate:g}% "
                                        f"по заявке №{offer.application_id}",
               f"/applications/{offer.application_id}/offers", "ok")
    else:
        offer.counter_status = "declined"
        log_event(db, offer.application_id, creditor, f"{creditor.display_name} оставил исходную ставку "
                                                      f"{offer.annual_interest_rate:g}%")
        notify(db, application.user_id, f"{creditor.display_name} оставил ставку {offer.annual_interest_rate:g}% "
                                        f"по заявке №{offer.application_id}",
               f"/applications/{offer.application_id}/offers", "warn")
    db.commit()


def reject(db: Session, borrower: models.User, offer_id: int) -> models.Application:
    offer, application = _own_shown_offer(db, borrower, offer_id)
    offer.status = "rejected"
    log_event(db, application.id, borrower, f"Отклонено предложение {offer.sender.display_name}")
    notify(db, offer.user_id, f"{borrower.display_name} отклонил ваше предложение по заявке №{application.id}",
           "/dashboard#my-offers", "warn")
    db.commit()
    return application


# --- Оператор ---
def review_list(db: Session):
    """Офферы, отправленные на ручную проверку, с причиной и подсказкой, что не так относительно заявки."""
    result = []
    for offer in db.query(models.Offer).filter(models.Offer.status == "pending").order_by(models.Offer.id):
        application = offer.application
        if not application:
            continue
        problems = []
        if offer.funding_amount > remaining(db, application):
            problems.append("сумма больше несобранной части заявки")
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
    record(db, operator, f"Показал заёмщику предложение {offer.sender.display_name} по заявке №{offer.application_id}",
           f"/operator/deal/{offer.application_id}")
    _show(db, offer, operator, f"Оператор проверил предложение {offer.sender.display_name}, оно показано заёмщику")
    notify(db, offer.user_id, f"Ваше предложение по заявке №{offer.application_id} проверено и показано заёмщику",
           "/dashboard#my-offers", "ok")
    db.commit()


def hide(db: Session, operator: models.User, offer_id: int, reason: str):
    offer = _pending(db, offer_id)
    offer.status = "hidden"
    offer.hide_reason = (reason.strip() or "Скрыт оператором")[:255]
    offer.operator_decision_at = datetime.utcnow()
    record(db, operator, f"Скрыл предложение {offer.sender.display_name} по заявке №{offer.application_id}: "
                         f"{offer.hide_reason}", f"/operator/deal/{offer.application_id}")
    log_event(db, offer.application_id, operator, f"Оператор скрыл предложение {offer.sender.display_name}")
    notify(db, offer.user_id, f"Оператор скрыл ваше предложение по заявке №{offer.application_id}: {offer.hide_reason}",
           "/dashboard#my-offers", "bad")
    db.commit()
