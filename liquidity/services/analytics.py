"""Аналитика администратора и архив согласованных сделок."""
from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from liquidity import models


def funnel(apps, offers, published_ids=None):
    """Воронка: подано -> опубликовано -> получили предложения -> дошли до сделки -> согласовано.
    published_ids — заявки, которые были опубликованы (по истории); без неё считаем по текущему статусу."""
    if published_ids is None:
        published_ids = {a.id for a in apps if a.status in ("published", "in_work", "agreed")}
    with_offers = {o.application_id for o in offers if o.operator_decision_at is not None and o.status != "hidden"}
    total = len(apps)
    stages = [("Подано заявок", total),
              ("Опубликованы", sum(1 for a in apps if a.id in published_ids)),
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
    from liquidity.services.deals import syndicate  # здесь, чтобы не было циклического импорта

    grouped = {}
    for o in sorted(offers, key=lambda o: o.id):
        if o.status == "accepted":
            grouped.setdefault(o.application_id, []).append(o)
    accepted = {app_id: items[0] if len(items) == 1 else syndicate(items) for app_id, items in grouped.items()}
    archive = [{"app": a, "offer": accepted.get(a.id),
                "confirmed_by": db.get(models.User, a.confirmed_by) if a.confirmed_by else None}
               for a in apps if a.status == "agreed"]
    published_ids = {e.application_id for e in db.query(models.ApplicationEvent).filter(
        models.ApplicationEvent.text.like("%опубликована для кредиторов%"))}
    weeks = weekly(apps)
    return {"apps": apps, "archive": archive, "analytics": build(apps, offers),
            "funnel": funnel(apps, offers, published_ids), "weeks": weeks,
            "weeks_max": max((w["count"] for w in weeks), default=0),
            "moderation_apps": [a for a in apps if a.status == "moderation"]}


def market_rates(db: Session):
    """Средние условия предложений, которые увидели заёмщики, по валютам: подсказка для калькулятора."""
    result = {}
    rows = db.query(models.Offer).filter(models.Offer.status.in_(["shown", "accepted", "rejected"])).all()
    for offer in rows:
        if offer.application:
            result.setdefault(offer.application.currency_code, []).append(offer)
    return {cur: {"count": len(items),
                  "rate": round(sum(float(o.annual_interest_rate) for o in items) / len(items), 2),
                  "lender_commission": round(sum(float(o.lender_commission) for o in items) / len(items), 2)}
            for cur, items in result.items()}


def _avg_hours(pairs):
    hours = [(end - start).total_seconds() / 3600 for start, end in pairs if start and end and end >= start]
    return sum(hours) / len(hours) if hours else None


def charts(db: Session, weeks: int = 8, today=None):
    """Данные для графиков администратора: спрос по стране и валюте, ставки по неделям, время этапов."""
    from liquidity import config

    apps = db.query(models.Application).all()
    offers = db.query(models.Offer).filter(models.Offer.status.in_(["shown", "accepted", "rejected"])).all()

    # Тепловая карта: страна получения × валюта (число заявок и объём)
    heat = {(c, cur): {"count": 0, "volume": 0.0} for c in config.TARGET_COUNTRIES for cur in config.CURRENCIES}
    for a in apps:
        cell = heat.get((a.target_jurisdiction, a.currency_code))
        if cell is not None:
            cell["count"] += 1
            cell["volume"] += float(a.funding_amount)
    heat_max = max((v["count"] for v in heat.values()), default=0)

    # Средняя ставка показанных заёмщикам предложений по неделям, отдельно по валютам
    today = today or date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    buckets = defaultdict(list)
    for o in offers:
        if o.created_at and o.application and o.created_at.date() >= start:
            buckets[(o.application.currency_code, (o.created_at.date() - start).days // 7)].append(
                float(o.annual_interest_rate))
    trend = {cur: [round(sum(buckets[(cur, i)]) / len(buckets[(cur, i)]), 2) if buckets[(cur, i)] else None
                   for i in range(weeks)] for cur in config.CURRENCIES}
    trend_values = [v for series in trend.values() for v in series if v is not None]

    # Время этапов по журналу событий
    events = defaultdict(list)
    for e in db.query(models.ApplicationEvent).order_by(models.ApplicationEvent.id):
        events[e.application_id].append(e)

    def first(app_id, *needles):
        return next((e.created_at for e in events[app_id] if any(n in e.text for n in needles)), None)

    published = lambda i: first(i, "опубликована для кредиторов")  # noqa: E731
    shown = lambda i: first(i, "показано заёмщику", "Оператор показал")  # noqa: E731

    by_id = {a.id: a for a in apps}
    stages = [
        ("Проверка заявки", "от подачи до публикации", _avg_hours(
            (by_id[i].created_at, published(i)) for i in by_id)),
        ("Первое предложение", "от публикации до показа заёмщику", _avg_hours(
            (published(i), shown(i)) for i in by_id)),
        ("Выбор кредитора", "от первого предложения до сделки", _avg_hours(
            (shown(i), first(i, "сделка в работе", "Сделка в работе")) for i in by_id)),
        ("Согласование", "от начала сделки до подтверждения", _avg_hours(
            (first(i, "сделка в работе", "Сделка в работе"), first(i, "Сделка подтверждена")) for i in by_id)),
    ]
    stage_max = max((h for _, _, h in stages if h), default=0)
    lines = trend_lines(trend, min(trend_values, default=0), max(trend_values, default=0), weeks)
    return {"heat": heat, "heat_max": heat_max, "countries": config.TARGET_COUNTRIES, "currencies": config.CURRENCIES,
            "trend": trend, "trend_min": min(trend_values, default=0), "trend_max": max(trend_values, default=0),
            "trend_weeks": [start + timedelta(weeks=i) for i in range(weeks)],
            "stages": stages, "stage_max": stage_max, "lines": lines}


CHART_W, CHART_H, PAD_X, PAD_Y = 600, 200, 24, 20


def trend_lines(trend, low, high, weeks):
    """Координаты линий для SVG: точки по неделям, отрезки только между соседними неделями с данными."""
    span = (high - low) or 1
    low, span = low - span * 0.15, span * 1.3

    def xy(i, v):
        x = PAD_X + i * (CHART_W - 2 * PAD_X) / max(weeks - 1, 1)
        y = CHART_H - PAD_Y - (v - low) / span * (CHART_H - 2 * PAD_Y)
        return round(x, 1), round(y, 1)

    result = []
    for n, (cur, series) in enumerate(trend.items(), 1):
        points = [(i, *xy(i, v), v) for i, v in enumerate(series) if v is not None]
        segments, current = [], []
        for i, v in enumerate(series):
            if v is None:
                if len(current) > 1:
                    segments.append(current)
                current = []
            else:
                current.append(xy(i, v))
        if len(current) > 1:
            segments.append(current)
        result.append({"currency": cur, "n": n, "points": points,
                       "segments": [" ".join(f"{x},{y}" for x, y in seg) for seg in segments]})
    ticks = [round(low + span * k / 4, 1) for k in (1, 2, 3)]
    return {"series": result, "ticks": [(t, xy(0, t)[1]) for t in ticks], "w": CHART_W, "h": CHART_H}


def public_stats(db: Session):
    """Живые цифры и потоки для главной: только агрегаты, без названий компаний."""
    from liquidity import config
    from liquidity.services.costs import offer_cost

    apps = db.query(models.Application).filter(
        models.Application.status.in_(["published", "in_work", "agreed", "expired", "withdrawn"])).all()
    offers = db.query(models.Offer).filter(models.Offer.status.in_(["shown", "accepted", "rejected"])).all()
    effective = [c["effective_rate"] for o in offers if o.application
                 for c in [offer_cost(o, o.application)] if c["effective_rate"] is not None]
    agreed = [a for a in apps if a.status == "agreed"]

    demand = Counter()
    for a in apps:
        demand[a.target_jurisdiction] += 1
    supply = Counter(o.sender.country_code for o in offers if o.sender and o.sender.country_code)
    return {
        "published": len(apps), "offers": len(offers), "agreed": len(agreed),
        "avg_effective": round(sum(effective) / len(effective), 1) if effective else None,
        "creditor_countries": len(supply),
        "demand": [(code, config.COUNTRY_NAMES.get(code, code), demand.get(code, 0)) for code in config.TARGET_COUNTRIES],
        "supply": [(code, config.COUNTRY_NAMES.get(code, code), n) for code, n in supply.most_common(5)],
    }
