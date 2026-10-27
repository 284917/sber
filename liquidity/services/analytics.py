"""Аналитика администратора и архив согласованных сделок."""
from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from liquidity import models


def funnel(apps, offers, published_ids=None):
    """Воронка: подано -> опубликовано -> получили предложения -> дошли до сделки -> согласовано.
    published_ids — заявки, прошедшие модерацию (по истории); без неё считаем по текущему статусу."""
    if published_ids is None:
        published_ids = {a.id for a in apps if a.status in ("published", "in_work", "agreed")}
    with_offers = {o.application_id for o in offers if o.operator_decision_at is not None and o.status != "hidden"}
    total = len(apps)
    stages = [("Подано заявок", total),
              ("Прошли модерацию", sum(1 for a in apps if a.id in published_ids)),
              ("Получили предложения", sum(1 for a in apps if a.id in with_offers)),
              ("Дошли до сделки", sum(1 for a in apps if a.status in ("in_work", "agreed"))),
              ("Согласованы", sum(1 for a in apps if a.status == "agreed"))]
    return [{"label": label, "count": count, "share": round(100 * count / total) if total else 0}
            for label, count in stages]


def weekly(apps, weeks=8, today=None):
    """Новые заявки по неделям (понедельник — начало недели), последние `weeks` недель включая текущую."""
    today = today or date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    counts = Counter()
    for a in apps:
        if a.created_at:
            d = a.created_at.date()
            if d >= start:
                counts[(d - start).days // 7] += 1
    return [{"start": start + timedelta(weeks=i), "count": counts.get(i, 0)} for i in range(weeks)]


def build(apps, offers):
    total_apps, total_offers = len(apps), len(offers)
    with_deal = sum(1 for a in apps if a.status in ("in_work", "agreed"))

    # Средний размер считается отдельно по каждой валюте: складывать EUR, USD и CNY нельзя
    by_currency = defaultdict(list)
    for a in apps:
        by_currency[a.currency_code].append(float(a.funding_amount))
    avg_by_currency = [{"currency": cur, "count": len(vals), "avg": sum(vals) / len(vals), "total": sum(vals)}
                       for cur, vals in sorted(by_currency.items(), key=lambda kv: -len(kv[1]))]

    # Спрос: куда заёмщики хотят получить средства. Предложение: из каких стран кредиторы делают офферы
    demand = Counter(a.target_jurisdiction for a in apps).most_common(5)
    supply = Counter((o.sender.country_code if o.sender and o.sender.country_code else "—")
                     for o in offers).most_common(5)
    return {"total_apps": total_apps, "total_offers": total_offers,
            "deal_share": round(100 * with_deal / total_apps) if total_apps else 0,
            "avg_by_currency": avg_by_currency, "top_demand": demand, "top_supply": supply,
            "status_counts": Counter(a.status for a in apps)}


def overview(db: Session):
    apps = db.query(models.Application).order_by(models.Application.created_at.desc(),
                                                 models.Application.id.desc()).all()
    offers = db.query(models.Offer).all()
    accepted = {o.application_id: o for o in offers if o.status == "accepted"}
    archive = [{"app": a, "offer": accepted.get(a.id),
                "confirmed_by": db.get(models.User, a.confirmed_by) if a.confirmed_by else None}
               for a in apps if a.status == "agreed"]
    published_ids = {e.application_id for e in db.query(models.ApplicationEvent).filter(
        models.ApplicationEvent.text.like("%прошла модерацию%"))}
    weeks = weekly(apps)
    return {"apps": apps, "archive": archive, "analytics": build(apps, offers),
            "funnel": funnel(apps, offers, published_ids), "weeks": weeks,
            "weeks_max": max((w["count"] for w in weeks), default=0),
            "moderation_apps": [a for a in apps if a.status == "moderation"]}
