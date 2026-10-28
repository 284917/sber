"""Заёмщик: заявки, сравнение предложений, решение по ним."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, applications, creditors, expiry, fx, kyc, offers
from liquidity.services.costs import compare_offers, payment_schedule
from liquidity.services.events import get_events
from liquidity.web import redirect, render

router = APIRouter(tags=["Заёмщик"])
borrower_only = require_role("borrower")


def onboarding(db: Session, user: models.User, apps):
    """Чек-лист первых шагов заёмщика; None, когда всё пройдено."""
    done, total = kyc.progress(db, user)
    steps = [("Компания зарегистрирована", True, None),
             ("Документы KYC загружены и отправлены", user.kyc_status in ("pending", "approved"), "/kyc"),
             ("Верификация пройдена", user.kyc_status == "approved", "/kyc"),
             ("Первая заявка опубликована", any(a.status not in ("moderation", "rejected") for a in apps), "#new-app"),
             ("Первая сделка", any(a.status in ("in_work", "agreed") for a in apps), None)]
    return None if all(ok for _, ok, _ in steps) else steps


def dashboard(request: Request, user: models.User, db: Session):
    expiry.run(db)
    apps = applications.for_borrower(db, user)
    return render(request, "borrower.html", user=user, apps=apps, onboarding=onboarding(db, user, apps),
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
    expiry.run(db)
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
                  funded=offers.funded(db, app_id), remaining=offers.remaining(db, application),
                  days_left=expiry.days_left(application.valid_until),
                  stages=applications.stages(db, application, shown))


@router.post("/applications/{app_id}/withdraw", summary="Отозвать заявку (до сделки)")
def withdraw_application(app_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    applications.withdraw(db, user, app_id)
    return redirect(f"/applications/{app_id}/offers?withdrawn=1")


@router.post("/accept_offer/{offer_id}", summary="Принять предложение (остальные отклоняются)")
def accept_offer(offer_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    offer = db.get(models.Offer, offer_id)
    try:
        application = offers.accept(db, user, offer_id)
    except Invalid as e:
        return redirect(f"/applications/{offer.application_id}/offers?error={e.code}" if offer else "/dashboard")
    if application.status == "in_work":
        return redirect(f"/deal/{application.id}")
    return redirect(f"/applications/{application.id}/offers?partial=1")


@router.post("/applications/{app_id}/start_deal", summary="Начать сделку с уже собранной частью суммы")
def start_deal(app_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    try:
        offers.start_deal(db, user, app_id)
    except Invalid as e:
        return redirect(f"/applications/{app_id}/offers?error={e.code}")
    return redirect(f"/deal/{app_id}")


@router.post("/offers/{offer_id}/counter", summary="Встречное предложение: попросить кредитора снизить ставку")
def counter_offer(offer_id: int, rate: str = Form(...), comment: str = Form(""),
                  user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    offer = db.get(models.Offer, offer_id)
    back = f"/applications/{offer.application_id}/offers" if offer else "/dashboard"
    value = applications.parse_number(rate)
    try:
        offers.counter(db, user, offer_id, value if value is not None else -1, comment)
    except Invalid as e:
        return redirect(f"{back}?error={e.code}")
    return redirect(f"{back}?countered=1")


@router.post("/reject_offer/{offer_id}", summary="Отклонить предложение")
def reject_offer(offer_id: int, user: models.User = Depends(borrower_only), db: Session = Depends(get_db)):
    application = offers.reject(db, user, offer_id)
    return redirect(f"/applications/{application.id}/offers")
