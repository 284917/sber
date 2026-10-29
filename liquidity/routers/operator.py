"""Оператор: ручная проверка того, на что сработали правила, жалобы и подтверждение сделок (администратору тоже доступно)."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, NotFound, applications, complaints, deals, expiry, offers
from liquidity.services.events import get_events
from liquidity.web import redirect, render

router = APIRouter(prefix="/operator", tags=["Оператор"])
staff = require_role(*config.STAFF_ROLES)


@router.get("/moderation", summary="Заявки на ручной проверке, жалобы кредиторов и опубликованные заявки")
def moderation_page(request: Request, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    expiry.run(db)
    apps = db.query(models.Application).filter(models.Application.status.in_(["moderation", "published"])).order_by(
        models.Application.created_at, models.Application.id).all()
    return render(request, "operator_moderation.html", user=user,
                  moderation_apps=[a for a in apps if a.status == "moderation"],
                  published_apps=[a for a in apps if a.status == "published"],
                  complaints=complaints.open_list(db), complaint_counts=complaints.open_counts(db))


@router.get("/offers", summary="Предложения, отправленные автопроверкой на ручную проверку")
def offers_page(request: Request, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    expiry.run(db)
    return render(request, "operator_offers.html", user=user, offers_data=offers.review_list(db))


@router.post("/offer/{offer_id}/show", summary="Показать предложение заёмщику")
def show_offer(offer_id: int, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    offers.show(db, user, offer_id)
    return redirect("/operator/offers")


@router.post("/offer/{offer_id}/hide", summary="Скрыть предложение с причиной")
def hide_offer(offer_id: int, reason: str = Form(""), user: models.User = Depends(staff),
               db: Session = Depends(get_db)):
    offers.hide(db, user, offer_id, reason)
    return redirect("/operator/offers")


@router.get("/deals", summary="Сделки в работе")
def deals_page(request: Request, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    items = deals.in_work(db)
    return render(request, "operator_deals.html", user=user, deals=items,
                  contracts=deals.main_contracts(db, [a.id for a in items]))


@router.get("/deal/{app_id}", summary="Проверка сделки перед подтверждением")
def deal_page(app_id: int, request: Request, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    application = db.get(models.Application, app_id)
    if not application:
        raise NotFound()
    offer = deals.accepted_offer(db, app_id)
    checks = deals.checks(db, application, offer)
    return render(request, "operator_deal.html", user=user, app=application, offer=offer, checks=checks,
                  lenders=deals.lenders(offer), contract=deals.room(db, application)[1],
                  stages=applications.stages(db, application), events=get_events(db, app_id),
                  can_confirm=application.status == "in_work" and all(checks.values()))


@router.post("/deal/{app_id}/confirm", summary="Подтвердить сделку: статус «Согласовано»")
def confirm(app_id: int, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    try:
        deals.confirm(db, user, app_id)
    except Invalid:
        return redirect(f"/operator/deal/{app_id}?error=checks")
    return redirect("/operator/deals")
