"""Администратор: вкладки «Обзор», «Очередь KYC», «Модерация», «Архив», «Все заявки», «Сотрудники».

Модерацию и снятие заявок может делать любой сотрудник (оператор или администратор),
создавать и отключать сотрудников — только главный администратор.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, accounts, analytics, applications, audit, complaints, expiry, export, kyc, sla
from liquidity.web import redirect, render

router = APIRouter(prefix="/admin", tags=["Администратор"])
admin_only = require_role("admin")
staff = require_role(*config.STAFF_ROLES)
TABS = ("overview", "kyc", "moderation", "archive", "history", "staff", "journal")


def back_to(user: models.User, tab: str, **params) -> str:
    """Куда вернуть после действия: администратора — на вкладку, оператора — на его страницу модерации."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    if user.role == "operator":
        return "/operator/moderation" + (f"?{query}" if query else "")
    return f"/admin?tab={tab}" + (f"&{query}" if query else "")


@router.get("", summary="Кабинет администратора (вкладки)")
def admin_page(request: Request, tab: str = "overview", user: models.User = Depends(admin_only),
               db: Session = Depends(get_db)):
    if tab not in TABS or (tab in ("staff", "journal") and not user.is_superadmin):
        tab = "overview"
    expiry.run(db)
    data = analytics.overview(db)
    kyc_queue = kyc.review_queue(db)
    return render(request, "admin.html", user=user, tab=tab, kyc_queue=kyc_queue, **data,
                  staff_members=accounts.staff_list(db) if tab == "staff" else [],
                  journal=audit.latest(db) if tab == "journal" else [],
                  charts=analytics.charts(db) if tab == "overview" else None,
                  doc_names={k: v for docs in config.KYC_DOCS.values() for k, v in docs},
                  complaints=complaints.open_list(db),
                  nav_counts={"kyc": len(kyc_queue), "moderation": len(data["moderation_apps"])})


@router.get("/export.xlsx", summary="Выгрузка аналитики, сделок, заявок и предложений в Excel")
def export_xlsx(user: models.User = Depends(admin_only), db: Session = Depends(get_db)):
    content = export.build_workbook(analytics.overview(db), db.query(models.Offer).all())
    filename = f"liquidity-{datetime.utcnow():%Y-%m-%d}.xlsx"
    return Response(content=content, headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/application/{app_id}/moderate", summary="Модерация заявки: approve или reject с причиной")
def moderate(app_id: int, decision: str = Form(...), comment: str = Form(""),
             user: models.User = Depends(staff), db: Session = Depends(get_db)):
    try:
        applications.moderate(db, user, app_id, decision, comment)
    except Invalid as e:
        return redirect(back_to(user, "moderation", error=e.code))
    return redirect(back_to(user, "moderation", moderated=1))


@router.post("/application/{app_id}/unpublish", summary="Снять опубликованную заявку с причиной")
def unpublish(app_id: int, reason: str = Form(""), back: str = Form("history"), user: models.User = Depends(staff),
              db: Session = Depends(get_db)):
    tab = back if back in TABS else "history"
    try:
        applications.unpublish(db, user, app_id, reason)
    except Invalid as e:
        return redirect(back_to(user, tab, error=e.code))
    return redirect(back_to(user, tab, unpublished=1))


@router.post("/complaint/{complaint_id}/dismiss", summary="Отклонить жалобу: заявка остаётся опубликованной")
def dismiss_complaint(complaint_id: int, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    complaints.dismiss(db, user, complaint_id)
    return redirect(back_to(user, "moderation", dismissed=1))


@router.post("/kyc/approve_user/{target_user_id}", summary="Одобрить пакет KYC компании")
def approve_kyc(target_user_id: int, user: models.User = Depends(admin_only), db: Session = Depends(get_db)):
    kyc.approve(db, target_user_id, actor=user)
    return redirect("/admin?tab=kyc&kyc_approved=1")


@router.post("/kyc/reject_user/{target_user_id}", summary="Отклонить пакет KYC с причиной")
def reject_kyc(target_user_id: int, reason: str = Form(""), user: models.User = Depends(admin_only),
               db: Session = Depends(get_db)):
    try:
        kyc.reject(db, target_user_id, reason, actor=user)
    except Invalid as e:
        return redirect(f"/admin?tab=kyc&error={e.code}")
    return redirect("/admin?tab=kyc&kyc_rejected=1")


def superadmin(user: models.User = Depends(admin_only)) -> models.User:
    if not user.is_superadmin:
        raise HTTPException(403, detail="Только главный администратор управляет сотрудниками")
    return user


@router.post("/create_user", summary="Создать сотрудника (только главный администратор)")
def create_staff(email: str = Form(...), password: str = Form(...), role: str = Form(...),
                 user: models.User = Depends(superadmin), db: Session = Depends(get_db)):
    try:
        accounts.create_staff(db, email=email, password=password, role=role, actor=user)
    except Invalid as e:
        return redirect(f"/admin?tab=staff&error={e.code}")
    return redirect("/admin?tab=staff&success=created")


@router.post("/staff/{staff_id}/remove", summary="Отключить сотрудника (только главный администратор)")
def remove_staff(staff_id: int, user: models.User = Depends(superadmin), db: Session = Depends(get_db)):
    try:
        accounts.remove_staff(db, user, staff_id)
    except Invalid as e:
        return redirect(f"/admin?tab=staff&error={e.code}")
    return redirect("/admin?tab=staff&removed=1")
