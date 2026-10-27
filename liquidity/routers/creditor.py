"""Кредитор: поиск заявок с фильтрами, отправка предложений, свои сделки."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, applications, offers
from liquidity.web import redirect, render

router = APIRouter(tags=["Кредитор"])
FILTER_KEYS = ("country", "currency", "amount_min", "amount_max", "term_max")


def dashboard(request: Request, user: models.User, db: Session):
    filters = {key: request.query_params.get(key, "") for key in FILTER_KEYS}
    my_offers = offers.for_creditor(db, user)
    my_deals = [o.application for o in my_offers
                if o.status == "accepted" and o.application and o.application.status in ("in_work", "agreed")]
    return render(request, "creditor.html", user=user, filters=filters,
                  available_apps=applications.search_published(db, filters), my_offers=my_offers, my_deals=my_deals,
                  offered_app_ids={o.application_id for o in my_offers if o.status in offers.ACTIVE_STATUSES},
                  currencies=config.CURRENCIES, countries=config.TARGET_COUNTRIES)


@router.post("/create_offer/{app_id}", summary="Отправить предложение по опубликованной заявке")
def create_offer(app_id: int, amount: float = Form(...), rate: float = Form(...), lender_comm: float = Form(...),
                 extra_terms: str = Form(""), user: models.User = Depends(require_role("creditor")),
                 db: Session = Depends(get_db)):
    try:
        offers.create(db, user, app_id, amount=amount, rate=rate, lender_comm=lender_comm, extra_terms=extra_terms)
    except Invalid as e:
        return redirect(f"/dashboard?error={e.code}")
    return redirect("/dashboard?offer_sent=1")
