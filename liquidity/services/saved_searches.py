"""Сохранённые фильтры кредитора: уведомление о каждой новой опубликованной заявке, которая под них подходит."""
from decimal import Decimal

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid, NotFound
from liquidity.services.applications import parse_number
from liquidity.services.notifications import notify
from liquidity.web import format_money


def for_user(db: Session, user: models.User):
    return db.query(models.SavedSearch).filter(models.SavedSearch.user_id == user.id).order_by(
        models.SavedSearch.id).all()


def is_saved(db: Session, user: models.User, filters: dict) -> bool:
    """Сохранён ли уже фильтр с такими условиями (чтобы не предлагать сохранить его ещё раз)."""
    amounts = {k: parse_number(filters.get(k)) for k in ("amount_min", "amount_max", "term_max")}
    probe = models.SavedSearch(
        country=filters.get("country") if filters.get("country") in config.TARGET_COUNTRIES else None,
        currency=filters.get("currency") if filters.get("currency") in config.CURRENCIES else None,
        amount_min=Decimal(str(amounts["amount_min"])) if amounts["amount_min"] is not None else None,
        amount_max=Decimal(str(amounts["amount_max"])) if amounts["amount_max"] is not None else None,
        term_max=int(amounts["term_max"]) if amounts["term_max"] is not None else None)
    return any(describe(s) == describe(probe) for s in for_user(db, user))


def create(db: Session, user: models.User, filters: dict) -> models.SavedSearch:
    country = filters.get("country") if filters.get("country") in config.TARGET_COUNTRIES else None
    currency = filters.get("currency") if filters.get("currency") in config.CURRENCIES else None
    amount_min, amount_max = parse_number(filters.get("amount_min")), parse_number(filters.get("amount_max"))
    term_max = parse_number(filters.get("term_max"))
    if not any([country, currency, amount_min, amount_max, term_max]):
        raise Invalid("empty", "Задайте хотя бы одно условие фильтра")
    if any(v is not None and v < 0 for v in (amount_min, amount_max, term_max)) or (
            amount_min is not None and amount_max is not None and amount_min > amount_max):
        raise Invalid("range", "Проверьте диапазон суммы")
    existing = for_user(db, user)
    if len(existing) >= config.MAX_SAVED_SEARCHES:
        raise Invalid("limit", f"Можно сохранить не больше {config.MAX_SAVED_SEARCHES} фильтров")
    search = models.SavedSearch(user_id=user.id, country=country, currency=currency,
                                amount_min=Decimal(str(amount_min)) if amount_min is not None else None,
                                amount_max=Decimal(str(amount_max)) if amount_max is not None else None,
                                term_max=int(term_max) if term_max is not None else None)
    if any(describe(s) == describe(search) for s in existing):
        raise Invalid("duplicate", "Такой фильтр уже сохранён")
    db.add(search)
    db.commit()
    return search


def delete(db: Session, user: models.User, search_id: int):
    search = db.get(models.SavedSearch, search_id)
    if not search or search.user_id != user.id:
        raise NotFound()
    db.delete(search)
    db.commit()


def matches(search: models.SavedSearch, application: models.Application) -> bool:
    amount = application.funding_amount
    return ((not search.country or application.target_jurisdiction == search.country)
            and (not search.currency or application.currency_code == search.currency)
            and (search.amount_min is None or amount >= search.amount_min)
            and (search.amount_max is None or amount <= search.amount_max)
            and (search.term_max is None or (application.term_months or 0) <= search.term_max))


def describe(search: models.SavedSearch) -> str:
    """Человекочитаемое описание фильтра: «EUR · Германия · от 1 000 000 · до 12 мес.»."""
    parts = []
    if search.currency:
        parts.append(search.currency)
    if search.country:
        parts.append(config.TARGET_COUNTRIES.get(search.country, search.country))
    if search.amount_min is not None:
        parts.append(f"от {format_money(search.amount_min)}")
    if search.amount_max is not None:
        parts.append(f"до {format_money(search.amount_max)}")
    if search.term_max is not None:
        parts.append(f"срок до {search.term_max} мес.")
    return " · ".join(parts)


def as_query(search: models.SavedSearch) -> str:
    """Ссылка на ленту заявок с этим фильтром."""
    params = {"country": search.country, "currency": search.currency,
              "amount_min": search.amount_min, "amount_max": search.amount_max, "term_max": search.term_max}
    return "&".join(f"{k}={int(v) if isinstance(v, Decimal) and v == int(v) else v}"
                    for k, v in params.items() if v is not None)


def notify_matching(db: Session, application: models.Application):
    """Вызывается при публикации заявки, в той же транзакции. Один кредитор получает одно уведомление,
    даже если заявка подходит под несколько его фильтров."""
    notified = set()
    for search in db.query(models.SavedSearch).all():
        if search.user_id in notified or not matches(search, application):
            continue
        notified.add(search.user_id)
        notify(db, search.user_id,
               f"Новая заявка №{application.id} по вашему фильтру «{describe(search)}»: "
               f"{format_money(application.funding_amount)} {application.currency_code}, "
               f"{config.TARGET_COUNTRIES.get(application.target_jurisdiction, '')}",
               f"/dashboard?{as_query(search)}#app-{application.id}", "ok")
