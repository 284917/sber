"""Администратор: проверка KYC, модерация заявок, аналитика, архив, сотрудники."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, accounts, analytics, applications, kyc
from liquidity.web import redirect, render

router = APIRouter(prefix="/admin", tags=["Администратор"])
admin_only = require_role("admin")


@router.get("", summary="Кабинет администратора")
def admin_page(request: Request, user: models.User = Depends(admin_only), db: Session = Depends(get_db)):
    data = analytics.overview(db)
    kyc_queue = kyc.review_queue(db)
    return render(request, "admin.html", user=user, kyc_queue=kyc_queue, **data,
                  nav_counts={"kyc": len(kyc_queue), "moderation": len(data["moderation_apps"])})


@router.post("/application/{app_id}/moderate", summary="Модерация заявки: approve или reject с причиной")
def moderate(app_id: int, decision: str = Form(...), comment: str = Form(""),
             user: models.User = Depends(admin_only), db: Session = Depends(get_db)):
    try:
        applications.moderate(db, user, app_id, decision, comment)
    except Invalid as e:
        return redirect(f"/admin?error={e.code}#moderation")
    return redirect("/admin?moderated=1#moderation")


@router.post("/kyc/approve_user/{target_user_id}", summary="Одобрить пакет KYC компании")
def approve_kyc(target_user_id: int, user: models.User = Depends(admin_only), db: Session = Depends(get_db)):
    kyc.approve(db, target_user_id)
    return redirect("/admin?kyc_approved=1#kyc")


@router.post("/kyc/reject_user/{target_user_id}", summary="Отклонить пакет KYC с причиной")
def reject_kyc(target_user_id: int, reason: str = Form(""), user: models.User = Depends(admin_only),
               db: Session = Depends(get_db)):
    try:
        kyc.reject(db, target_user_id, reason)
    except Invalid as e:
        return redirect(f"/admin?error={e.code}#kyc")
    return redirect("/admin?kyc_rejected=1#kyc")


@router.post("/create_user", summary="Создать сотрудника (оператор или администратор)")
def create_staff(email: str = Form(...), password: str = Form(...), role: str = Form(...),
                 user: models.User = Depends(admin_only), db: Session = Depends(get_db)):
    try:
        accounts.create_staff(db, email=email, password=password, role=role)
    except Invalid as e:
        return redirect(f"/admin?error={e.code}#staff")
    return redirect("/admin?success=created#staff")
