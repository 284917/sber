"""Надёжность участников как набор проверяемых фактов, а не один балл: единая оценка выглядела бы
как рекомендация платформы. Названия компаний-контрагентов здесь никогда не раскрываются."""
from datetime import date

from sqlalchemy.orm import Session

from liquidity import models
from liquidity.services import company_lookup, creditors, deal_reports

PURPOSE_DETAILED = 30
EGRUL_ACTIVE = "ACTIVE"


def _approved_types(db: Session, user_ids):
    found = {uid: set() for uid in user_ids}
    for doc in db.query(models.Document).filter(models.Document.user_id.in_(list(user_ids) or [0]),
                                                models.Document.status == "approved"):
        found[doc.user_id].add(doc.doc_type)
    return found


def _verified_on(user: models.User):
    if user.kyc_status != "approved":
        return None
    if user.kyc_approved_at:
        return user.kyc_approved_at
    dates = [d.created_at for d in user.documents if d.status == "approved" and d.created_at]
    return max(dates) if dates else None


def _share(item, shown, waiting="отчётов пока мало"):
    """«5 из 5 сделок», пока отчётов мало — честная подпись вместо цифры."""
    if not shown or not item["total"]:
        return waiting
    return f"{item['ok']} из {item['total']}"


def creditor_facts(db: Session, creditor_ids):
    """{кредитор: {checks, stats, reports, ...}} для карточки предложения, сравнения и профиля."""
    ids = list(set(creditor_ids))
    users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(ids or [0]))}
    docs = _approved_types(db, ids)
    stats = creditors.stats(db, ids)
    reports = deal_reports.about_creditors(db, ids)
    result = {}
    for cid in ids:
        user, s, r = users.get(cid), stats.get(cid), reports[cid]
        if not user or not s:
            continue
        result[cid] = {
            "verified_on": _verified_on(user),
            "country": user.country_code,
            "ubo": "ubo" in docs[cid],
            "source_funds": "source_funds" in docs[cid],
            "since": user.created_at,
            "deals": s["deals_done"], "deals_in_work": s["deals_in_work"],
            "response_hours": s["response_hours"], "response": creditors.response_text(s["response_hours"]),
            "acceptance": s["acceptance"], "decided": s["decided"],
            "reports": r,
            "terms_text": _share(r["terms"], r["shown"]),
            "funds_text": _share(r["funds"], r["shown"]),
            "on_time_text": _share(r["on_time"], r["shown"]),
            "contact_text": _share(r["contact"], r["shown"]),
        }
    return result


def _amount_band(amount: float, other: float) -> bool:
    return amount / 2 <= other <= amount * 2 if amount else False


def similar_deals(db: Session, creditor_ids, application: models.Application):
    """Опыт кредитора в похожих согласованных сделках: тот же маршрут или хотя бы та же валюта."""
    ids = list(set(creditor_ids))
    rows = db.query(models.Offer, models.Application).join(
        models.Application, models.Offer.application_id == models.Application.id).filter(
        models.Offer.user_id.in_(ids or [0]), models.Offer.status == "accepted", models.Application.status == "agreed",
        models.Application.id != application.id).all()
    received = deal_reports.received_app_ids(db, {app.id for _, app in rows})
    amount = float(application.funding_amount)
    result = {cid: {"route": 0, "currency": 0, "similar_amount": 0, "received": 0} for cid in ids}
    for offer, app in rows:
        item = result[offer.user_id]
        if app.currency_code != application.currency_code:
            continue
        item["currency"] += 1
        if _amount_band(amount, float(app.funding_amount)):
            item["similar_amount"] += 1
        if app.target_jurisdiction == application.target_jurisdiction and app.transfer_method == application.transfer_method:
            item["route"] += 1
            if app.id in received:
                item["received"] += 1
    return result


def borrower_cards(db: Session, applications):
    """{заявка: карточка заёмщика для кредитора}: факты о компании и полнота данных."""
    owner_ids = {a.user_id for a in applications}
    owners = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(owner_ids or [0]))}
    docs = _approved_types(db, owner_ids)
    reports = deal_reports.about_borrowers(db, owner_ids)
    history = {}
    for uid, status in db.query(models.Application.user_id, models.Application.status).filter(
            models.Application.user_id.in_(owner_ids or [0])):
        row = history.setdefault(uid, {"total": 0, "agreed": 0})
        row["total"] += 1
        row["agreed"] += status == "agreed"
    cards = {}
    today = date.today()
    for app in applications:
        user = owners.get(app.user_id)
        if not user:
            continue
        past = history.get(user.id, {"total": 1, "agreed": 0})
        report = reports[user.id]
        years = today.year - user.registered_on.year - ((today.month, today.day) < (user.registered_on.month, user.registered_on.day)) if user.registered_on else None
        checks = [("Верификация пройдена", user.kyc_status == "approved"),
                  ("Бенефициары проверены", "beneficiaries" in docs[user.id]),
                  ("Статус в ЕГРЮЛ подтверждён", user.egrul_status == EGRUL_ACTIVE),
                  ("Назначение описано подробно", len(app.purpose or "") >= PURPOSE_DETAILED),
                  ("Контракт приложен", bool(app.contract_path)),
                  ("Есть история на платформе", past["total"] > 1 or past["agreed"] > 0)]
        cards[app.id] = {
            "verified_on": _verified_on(user), "egrul_status": user.egrul_status,
            "egrul_name": company_lookup.STATUS_NAMES.get(user.egrul_status), "registered_on": user.registered_on, "years": years,
            "apps_total": past["total"], "apps_agreed": past["agreed"], "contract": bool(app.contract_path),
            "reports": report, "on_time_text": _share(report["on_time"], report["shown"]),
            "checks": checks, "done": sum(1 for _, ok in checks if ok), "total": len(checks),
        }
    return cards
