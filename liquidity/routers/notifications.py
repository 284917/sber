"""Уведомления в кабинете: список, переход по уведомлению, «прочитать все»."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from liquidity import models
from liquidity.database import get_db
from liquidity.security import get_current_user
from liquidity.services import notifications
from liquidity.web import redirect, render

router = APIRouter(prefix="/notifications", tags=["Уведомления"])


@router.get("", summary="Все уведомления пользователя")
def notifications_page(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    return render(request, "notifications.html", user=user, items=notifications.latest(db, user.id))


@router.get("/{notification_id}", summary="Открыть уведомление: отметить прочитанным и перейти по ссылке")
def open_notification(notification_id: int, user: models.User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    item = notifications.mark_read(db, user.id, notification_id)
    if not item or item.user_id != user.id:
        return redirect("/notifications")
    return redirect(item.link or "/notifications")


@router.post("/read-all", summary="Отметить все уведомления прочитанными")
def read_all(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    notifications.mark_all_read(db, user.id)
    return redirect("/notifications")
