"""Кредитор: поиск заявок с фильтрами, отправка предложений, свои сделки."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import get_current_user, require_role
from liquidity.services import Invalid, applications, complaints, creditors, expiry, kyc, offers, saved_searches
from liquidity.web import redirect, render

router = APIRouter(tags=["Кредитор"])
FILTER_KEYS = ("country", "currency", "amount_min", "amount_max", "term_max")


def onboarding(user: models.User, my_offers, my_deals):
    steps = [("Компания зарегистрирована", True, None),
             ("Документы KYC загружены и отправлены", user.kyc_status in ("pending", "approved"), "/kyc"),
             ("Верификация пройдена", user.kyc_status == "approved", "/kyc"),
             ("Первое предложение отправлено", bool(my_offers), "#market"),
             ("Первая сделка", bool(my_deals), None)]
    return None if all(ok for _, ok, _ in steps) else steps


def dashboard(request: Request, user: models.User, db: Session):
    expiry.run(db)
    filters = {key: request.query_params.get(key, "") for key in FILTER_KEYS}
    my_offers = offers.for_creditor(db, user)
    my_deals = [o.application for o in my_offers
                if o.status == "accepted" and o.application and o.application.status in ("in_work", "agreed")]
    available_apps = applications.search_published(db, filters)
    return render(request, "creditor.html", user=user, filters=filters, onboarding=onboarding(user, my_offers, my_deals),
                  available_apps=available_apps, my_offers=my_offers, my_deals=my_deals,
                  funded=offers.funded_map(db, [a.id for a in available_apps]), min_share=config.MIN_OFFER_SHARE,
                  valid_choices=config.OFFER_VALID_CHOICES, valid_default=config.OFFER_VALID_DEFAULT,
                  offered_app_ids={o.application_id for o in my_offers if o.status in offers.ACTIVE_STATUSES},
                  complained_app_ids=complaints.complained_app_ids(db, user),
                  saved=[(s, saved_searches.describe(s), saved_searches.as_query(s))
                         for s in saved_searches.for_user(db, user)],
                  max_saved=config.MAX_SAVED_SEARCHES, filter_saved=saved_searches.is_saved(db, user, filters),
                  currencies=config.CURRENCIES, countries=config.TARGET_COUNTRIES)


@router.post("/create_offer/{app_id}", summary="Отправить предложение по опубликованной заявке")
def create_offer(app_id: int, amount: float = Form(...), rate: float = Form(...), lender_comm: float = Form(...),
                 extra_terms: str = Form(""), valid_days: int = Form(config.OFFER_VALID_DEFAULT),
                 user: models.User = Depends(require_role("creditor")), db: Session = Depends(get_db)):
    try:
        offer = offers.create(db, user, app_id, amount=amount, rate=rate, lender_comm=lender_comm,
                              extra_terms=extra_terms, valid_days=valid_days)
    except Invalid as e:
        return redirect(f"/dashboard?error={e.code}")
    return redirect("/dashboard?offer_sent=" + ("review" if offer.status == "pending" else "1"))


@router.post("/applications/{app_id}/complain", summary="Пожаловаться на опубликованную заявку")
def complain(app_id: int, reason: str = Form(""), user: models.User = Depends(require_role("creditor")),
             db: Session = Depends(get_db)):
    try:
        complaints.create(db, user, app_id, reason)
    except Invalid as e:
        return redirect(f"/dashboard?error={e.code}#app-{app_id}")
    return redirect("/dashboard?complained=1")


@router.post("/offers/{offer_id}/withdraw", summary="Отозвать своё предложение (пока нет решения заёмщика)")
def withdraw_offer(offer_id: int, user: models.User = Depends(require_role("creditor")), db: Session = Depends(get_db)):
    offers.withdraw(db, user, offer_id)
    return redirect("/dashboard?offer_withdrawn=1#my-offers")


@router.get("/creditors/{creditor_id}", summary="Профиль и рейтинг кредитора")
def creditor_profile(creditor_id: int, request: Request, user: models.User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    data = creditors.profile(db, user, creditor_id)
    return render(request, "creditor_profile.html", user=user, response_text=creditors.response_text, **data)


@router.post("/saved-searches", summary="Сохранить фильтр и получать уведомления о новых заявках")
def save_search(country: str = Form(""), currency: str = Form(""), amount_min: str = Form(""),
                amount_max: str = Form(""), term_max: str = Form(""),
                user: models.User = Depends(require_role("creditor")), db: Session = Depends(get_db)):
    filters = {"country": country, "currency": currency, "amount_min": amount_min, "amount_max": amount_max,
               "term_max": term_max}
    query = "&".join(f"{k}={v}" for k, v in filters.items() if v)
    try:
        saved_searches.create(db, user, filters)
    except Invalid as e:
        return redirect(f"/dashboard?{query}&search_error={e.code}#saved")
    return redirect(f"/dashboard?{query}&search_saved=1#saved")


@router.post("/saved-searches/{search_id}/delete", summary="Удалить сохранённый фильтр")
def delete_search(search_id: int, user: models.User = Depends(require_role("creditor")), db: Session = Depends(get_db)):
    saved_searches.delete(db, user, search_id)
    return redirect("/dashboard?search_deleted=1#saved")


@router.post("/offers/{offer_id}/counter/{answer}", summary="Ответ кредитора на встречную ставку: agree или decline")
def answer_counter(offer_id: int, answer: str, user: models.User = Depends(require_role("creditor")),
                   db: Session = Depends(get_db)):
    if answer not in ("agree", "decline"):
        return redirect("/dashboard#my-offers")
    try:
        offers.answer_counter(db, user, offer_id, answer == "agree")
    except Invalid as e:
        return redirect(f"/dashboard?error={e.code}#my-offers")
    return redirect(f"/dashboard?counter_{answer}=1#my-offers")
