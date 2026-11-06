"""Отчёт после сделки: платформа заканчивается на «Согласовано», а отчёт показывает, что было дальше.

Заполняют обе стороны. Ответы конкретной сделки видят только сам автор и оператор, остальным
показывается сводка от MIN_SHOWN отчётов и без названий компаний."""
from datetime import datetime

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Forbidden, Invalid, NotFound
from liquidity.services.events import log_event
from liquidity.services.notifications import notify_staff

MIN_SHOWN = 3
FUNDS = ("received", "partial", "none")
ON_TIME = ("yes", "no", "not_due")
YES_NO = ("yes", "no")


def _participants(db: Session, application: models.Application):
    creditors = {o.user_id for o in db.query(models.Offer).filter(models.Offer.application_id == application.id,
                                                                 models.Offer.status == "accepted")}
    return {application.user_id} | creditors


def own(db: Session, user: models.User, app_id: int):
    return db.query(models.DealReport).filter_by(application_id=app_id, user_id=user.id).first()


def for_application(db: Session, app_id: int):
    return db.query(models.DealReport).filter_by(application_id=app_id).order_by(models.DealReport.id).all()


def is_problem(report: models.DealReport) -> bool:
    return (report.funds != "received" or report.on_time == "no" or not report.terms_match
            or report.new_terms or not report.contact_ok)


def save(db: Session, user: models.User, app_id: int, *, funds: str, on_time: str, terms_match: str, new_terms: str,
         contact_ok: str, comment: str = "") -> models.DealReport:
    application = db.get(models.Application, app_id)
    if not application or application.status != "agreed":
        raise NotFound()
    if user.id not in _participants(db, application):
        raise Forbidden()
    if funds not in FUNDS or on_time not in ON_TIME or {terms_match, new_terms, contact_ok} - set(YES_NO):
        raise Invalid("report", "Ответьте на все вопросы отчёта")
    report = own(db, user, app_id)
    was_problem = bool(report and is_problem(report))
    if not report:
        report = models.DealReport(application_id=app_id, user_id=user.id, role=user.role)
        db.add(report)
        log_event(db, app_id, user, f"{user.display_name} заполнил отчёт после сделки")
    report.funds, report.on_time = funds, on_time
    report.terms_match, report.new_terms, report.contact_ok = terms_match == "yes", new_terms == "yes", contact_ok == "yes"
    report.comment = (comment or "").strip()[:500] or None
    report.updated_at = datetime.utcnow()
    if is_problem(report) and not was_problem:
        notify_staff(db, f"Сделка №{app_id}: {user.display_name} сообщил о проблеме в отчёте", f"/operator/deal/{app_id}",
                     "warn", roles=("operator",))
    db.commit()
    return report


def with_problems(db: Session):
    """Согласованные сделки, по которым участник сообщил о проблеме: задача оператора."""
    found = {}
    for report in db.query(models.DealReport).order_by(models.DealReport.id.desc()):
        if is_problem(report):
            found.setdefault(report.application_id, report)
    apps = db.query(models.Application).filter(models.Application.id.in_(list(found) or [0])).order_by(models.Application.id.desc()).all()
    return [(app, found[app.id]) for app in apps]


def _share(reports, test):
    return {"ok": sum(1 for r in reports if test(r)), "total": len(reports)}


def summary(reports):
    """Сводка отчётов. shown=False: отчётов меньше MIN_SHOWN, цифры показывать нельзя."""
    due = [r for r in reports if r.on_time != "not_due"]
    return {
        "count": len(reports), "shown": len(reports) >= MIN_SHOWN,
        "terms": _share(reports, lambda r: r.terms_match),
        "funds": _share(reports, lambda r: r.funds == "received"),
        "on_time": _share(due, lambda r: r.on_time == "yes"),
        "clean": _share(reports, lambda r: not r.new_terms),
        "contact": _share(reports, lambda r: r.contact_ok),
    }


def about_creditors(db: Session, creditor_ids):
    """{кредитор: сводка отчётов заёмщиков по сделкам, где он участвовал}."""
    ids = list(set(creditor_ids))
    pairs = db.query(models.Offer.user_id, models.DealReport).join(
        models.DealReport, models.DealReport.application_id == models.Offer.application_id).filter(
        models.Offer.user_id.in_(ids or [0]), models.Offer.status == "accepted", models.DealReport.role == "borrower").all()
    grouped = {cid: [] for cid in ids}
    for cid, report in pairs:
        grouped[cid].append(report)
    return {cid: summary(reports) for cid, reports in grouped.items()}


def about_borrowers(db: Session, borrower_ids):
    """{заёмщик: сводка отчётов кредиторов по его сделкам}."""
    ids = list(set(borrower_ids))
    pairs = db.query(models.Application.user_id, models.DealReport).join(
        models.DealReport, models.DealReport.application_id == models.Application.id).filter(
        models.Application.user_id.in_(ids or [0]), models.DealReport.role == "creditor").all()
    grouped = {bid: [] for bid in ids}
    for bid, report in pairs:
        grouped[bid].append(report)
    return {bid: summary(reports) for bid, reports in grouped.items()}


def received_app_ids(db: Session, app_ids):
    """Сделки, по которым заёмщик подтвердил получение денег."""
    return {r.application_id for r in db.query(models.DealReport).filter(
        models.DealReport.application_id.in_(list(app_ids) or [0]), models.DealReport.role == "borrower",
        models.DealReport.funds == "received")}
