"""Заёмщик: заявки, сравнение предложений, решение по ним."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, applications, creditors, fx, offers
from liquidity.services.costs import compare_offers, payment_schedule
from liquidity.services.events import get_events
from liquidity.web import redirect, render

router = APIRouter(tags=["Заёмщик"])
borrower_only = require_role("borrower")


def dashboard(request: Request, user: models.User, db: Session):
    apps = applications.for_borrower(db, user)
    return render(request, "borrower.html", user=user, apps=apps,
                  shown_counts=offers.shown_counts(db, [a.id for a in apps]),
                  currencies=config.CURRENCIES, countries=config.TARGET_COUNTRIES, max_term=config.MAX_TERM_MONTHS)


@router.post("/create_application", summary="Создать заявку (уходит на модерацию)")
def create_application(amount: float = Form(...), currency: str = Form(...), country: str = Form(...),
                       purpose: str = Form(...), term_months: int = Form(...), transfer_method: str = Form(...),
                       user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    try:
        applications.create(db, user, amount=amount, currency=currency, country=country, purpose=purpose,
                            term_months=term_months, transfer_method=transfer_method)
    except Invalid as e:
        return redirect(f"/dashboard?error={e.code}")
    return redirect("/dashboard?created=1")


@router.get("/applications/{app_id}/offers", summary="Карточка заявки: этапы, сравнение предложений, история")
def application_page(app_id: int, request: Request, user: models.User = Depends(borrower_only),
                     db: Session = Depends(get_db)):
    application = applications.get_own(db, user, app_id)
    rows, best_id, savings = compare_offers(application, offers.visible_to_borrower(db, app_id))
    for row in rows:
        row["schedule"] = payment_schedule(row["offer"], application)
    shown = sum(1 for r in rows if r["offer"].status == "shown")
    rates = fx.current_rates(db)
    rub = lambda amount: fx.to_rub(amount, application.currency_code, rates)  # noqa: E731
    return render(request, "offers.html", user=user, application=application, rows=rows, best_id=best_id,
                  savings=savings, events=get_events(db, app_id), fx=rates, rub=rub,
                  creditor_stats=creditors.stats(db, [r["offer"].user_id for r in rows]),
                  stages=applications.stages(db, application, shown))


@router.post("/applications/{app_id}/withdraw", summary="Отозвать заявку (до сделки)")
def withdraw_application(app_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    applications.withdraw(db, user, app_id)
    return redirect(f"/applications/{app_id}/offers?withdrawn=1")


@router.post("/accept_offer/{offer_id}", summary="Принять предложение (остальные отклоняются)")
def accept_offer(offer_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    application = offers.accept(db, user, offer_id)
    return redirect(f"/deal/{application.id}")


@router.post("/reject_offer/{offer_id}", summary="Отклонить предложение")
def reject_offer(offer_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    application = offers.reject(db, user, offer_id)
    return redirect(f"/applications/{application.id}/offers")
