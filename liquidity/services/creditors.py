"""Профиль и рейтинг кредитора: сделки, ставки, скорость ответа. Помогает заёмщику выбрать, кому доверять."""
from sqlalchemy.orm import Session

from liquidity import models
from liquidity.services import Forbidden, NotFound

PUBLISHED_EVENT = "%опубликована для кредиторов%"
MIN_DECISIONS = 3


def can_view(db: Session, viewer: models.User, creditor_id: int) -> bool:
    """Профиль видят сам кредитор, сотрудники и заёмщики, которым оператор показал его предложение."""
    if viewer.role in ("admin", "operator") or viewer.id == creditor_id:
        return True
    if viewer.role != "borrower":
        return False
    return db.query(models.Offer).join(models.Application, models.Offer.application_id == models.Application.id).filter(
        models.Offer.user_id == creditor_id, models.Application.user_id == viewer.id,
        models.Offer.status.in_(["shown", "accepted", "rejected"])).first() is not None


def stats(db: Session, creditor_ids):
    """Показатели по кредиторам: {id: {...}}. Скорость ответа — от публикации заявки до предложения."""
    ids = list(set(creditor_ids))
    if not ids:
        return {}
    offers = db.query(models.Offer).filter(models.Offer.user_id.in_(ids)).all()
    app_ids = {o.application_id for o in offers}
    published_at = {}
    if app_ids:
        for event in db.query(models.ApplicationEvent).filter(
                models.ApplicationEvent.application_id.in_(app_ids),
                models.ApplicationEvent.text.like(PUBLISHED_EVENT)).order_by(models.ApplicationEvent.id):
            published_at.setdefault(event.application_id, event.created_at)

    result = {}
    for cid in ids:
        own = [o for o in offers if o.user_id == cid]
        reviewed = [o for o in own if o.status in ("shown", "accepted", "rejected")]
        accepted = [o for o in own if o.status == "accepted"]
        decided = [o for o in own if o.status in ("accepted", "rejected")]
        done = [o for o in accepted if o.application and o.application.status == "agreed"]
        hours = [(o.created_at - published_at[o.application_id]).total_seconds() / 3600
                 for o in own if o.created_at and published_at.get(o.application_id)
                 and o.created_at >= published_at[o.application_id]]
        base = reviewed or own
        row = {
            "offers": len(own),
            "deals_done": len(done),
            "deals_in_work": sum(1 for o in accepted if o.application and o.application.status == "in_work"),
            "avg_rate": sum(float(o.annual_interest_rate) for o in base) / len(base) if base else None,
            "avg_commission": sum(float(o.total_commission) for o in base) / len(base) if base else None,
            "acceptance": round(100 * len(accepted) / len(decided)) if decided else None,
            "decided": len(decided),
            "response_hours": sum(hours) / len(hours) if hours else None,
            "currencies": sorted({o.application.currency_code for o in own if o.application}),
        }
        row["rating"] = rating(row)
        result[cid] = row
    return result


def rating(row):
    """Рейтинг от 1 до 5 из трёх понятных частей. Пока данных мало (нет завершённых сделок и меньше
    MIN_DECISIONS решений заёмщиков), рейтинга нет: одно отклонённое предложение не должно ронять оценку.
    Завершённые сделки (до 2 баллов), доля принятых предложений (до 1,5), скорость ответа (до 1,5)."""
    if not row["deals_done"] and row.get("decided", 0) < MIN_DECISIONS:
        return None
    parts = {
        "deals": min(2.0, row["deals_done"] * 0.5),
        "acceptance": 1.5 * (row["acceptance"] or 0) / 100,
        "speed": 1.0 if row["response_hours"] is None else
                 1.5 if row["response_hours"] <= 24 else 1.0 if row["response_hours"] <= 72 else 0.5,
    }
    return {"score": round(max(1.0, sum(parts.values())), 1), "parts": parts}


def response_text(hours):
    if hours is None:
        return "—"
    if hours < 1:
        return "меньше часа"
    if hours < 48:
        return f"{round(hours)} ч"
    return f"{round(hours / 24)} дн."


def profile(db: Session, viewer: models.User, creditor_id: int):
    creditor = db.get(models.User, creditor_id)
    if not creditor or creditor.role != "creditor":
        raise NotFound()
    if not can_view(db, viewer, creditor_id):
        raise Forbidden()
    recent = db.query(models.Offer).filter(models.Offer.user_id == creditor_id,
                                           models.Offer.status == "accepted").order_by(models.Offer.id.desc()).limit(5).all()
    return {"creditor": creditor, "stats": stats(db, [creditor_id])[creditor_id],
            "recent_deals": [o for o in recent if o.application and o.application.status in ("in_work", "agreed")]}
