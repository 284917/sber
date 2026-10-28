"""Сроки действия заявок и предложений: просроченные закрываются автоматически.

Отдельного планировщика на Vercel нет, поэтому проверка запускается при открытии рабочих страниц
(не чаще раза в минуту на процесс) — для участников это выглядит как мгновенное закрытие в срок.
"""
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from liquidity import models
from liquidity.services.events import log_event
from liquidity.services.notifications import notify

THROTTLE = timedelta(minutes=1)
_last_run = {"at": None}


def run(db: Session, today: date = None, force: bool = False) -> dict:
    now = datetime.utcnow()
    if not force and _last_run["at"] and now - _last_run["at"] < THROTTLE:
        return {"offers": 0, "applications": 0}
    _last_run["at"] = now
    today = today or now.date()
    expired_offers = expired_apps = 0

    for offer in db.query(models.Offer).filter(models.Offer.status.in_(["pending", "shown"]),
                                               models.Offer.valid_until.isnot(None),
                                               models.Offer.valid_until < today).all():
        was_shown = offer.status == "shown"
        offer.status = "expired"
        expired_offers += 1
        log_event(db, offer.application_id, None, "Истёк срок действия предложения"
                  + (f" {offer.sender.display_name}" if was_shown and offer.sender else ""))
        notify(db, offer.user_id, f"Истёк срок вашего предложения по заявке №{offer.application_id}. "
                                  f"Можно отправить новое", "/dashboard#my-offers", "warn")
        if was_shown and offer.application:
            notify(db, offer.application.user_id, f"Истёк срок предложения {offer.sender.display_name} "
                                                  f"по заявке №{offer.application_id}",
                   f"/applications/{offer.application_id}/offers", "warn")

    for application in db.query(models.Application).filter(models.Application.status == "published",
                                                           models.Application.valid_until.isnot(None),
                                                           models.Application.valid_until < today).all():
        offers = db.query(models.Offer).filter(models.Offer.application_id == application.id).all()
        if any(o.status == "accepted" for o in offers):
            continue  # часть суммы уже собрана: решение за заёмщиком, заявку не закрываем
        application.status = "expired"
        expired_apps += 1
        for offer in offers:
            if offer.status in ("pending", "shown"):
                offer.status = "expired"
                notify(db, offer.user_id, f"Заявка №{application.id} закрыта по сроку, ваше предложение тоже",
                       "/dashboard#my-offers", "warn")
        log_event(db, application.id, None, "Срок публикации заявки истёк, заявка закрыта")
        notify(db, application.user_id, f"Срок публикации заявки №{application.id} истёк. "
                                        f"Подайте новую, если финансирование ещё нужно",
               f"/applications/{application.id}/offers", "warn")

    if expired_offers or expired_apps:
        db.commit()
    return {"offers": expired_offers, "applications": expired_apps}


def days_left(valid_until, today: date = None):
    if not valid_until:
        return None
    return (valid_until - (today or date.today())).days
