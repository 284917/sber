"""Мой аккаунт: данные компании и сотрудника, смена пароля, настройки интерфейса."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import get_current_user
from liquidity.services import Invalid, accounts, kyc
from liquidity.web import redirect, render

router = APIRouter(prefix="/account", tags=["Аккаунт"])


@router.get("", summary="Мой аккаунт")
def account_page(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    done, total = kyc.progress(db, user) if user.role in config.PUBLIC_ROLES else (0, 0)
    return render(request, "account.html", user=user, kyc_done=done, kyc_total=total,
                  min_length=config.PASSWORD_MIN_LENGTH, session_hours=config.SESSION_HOURS)


@router.post("/password", summary="Сменить пароль")
def change_password(current_password: str = Form(...), new_password: str = Form(...), repeat_password: str = Form(...),
                    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        accounts.change_password(db, user, current_password, new_password, repeat_password)
    except Invalid as e:
        return redirect(f"/account?error={e.code}#password")
    return redirect("/account?password_changed=1")
