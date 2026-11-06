"""Поддержка: форма для всех, в том числе без входа, и очередь обращений у оператора."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import get_current_user, require_role
from liquidity.services import Invalid, support, throttle
from liquidity.web import redirect, render

router = APIRouter(tags=["Поддержка"])
staff = require_role(*config.STAFF_ROLES)


def _optional_user(request: Request, db: Session):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


@router.get("/support", summary="Написать в поддержку")
def support_page(request: Request, db: Session = Depends(get_db)):
    user = _optional_user(request, db)
    if user and user.role in config.STAFF_ROLES:
        return redirect("/operator/support")
    if user:
        return render(request, "support.html", user=user, tickets=support.for_user(db, user), topics=config.SUPPORT_TOPICS)
    return render(request, "support_public.html", topics=config.SUPPORT_TOPICS)


@router.post("/support", summary="Отправить обращение")
def create_ticket(request: Request, topic: str = Form(""), text: str = Form(""), contact_email: str = Form(""),
                  db: Session = Depends(get_db)):
    user = _optional_user(request, db)
    try:
        support.create(db, user, topic=topic, text=text, contact_email=contact_email,
                       ip=throttle.client_ip(request, config.ON_VERCEL))
    except Invalid as e:
        return redirect(f"/support?error={e.code}")
    return redirect("/support?sent=1")


@router.get("/operator/support", summary="Обращения в поддержку", tags=["Оператор"])
def queue_page(request: Request, user: models.User = Depends(staff), db: Session = Depends(get_db)):
    opened, answered = support.queue(db)
    return render(request, "operator_support.html", user=user, opened=opened, answered=answered, topics=config.SUPPORT_TOPICS)


@router.post("/operator/support/{ticket_id}/answer", summary="Ответить на обращение", tags=["Оператор"])
def answer_ticket(ticket_id: int, reply: str = Form(""), user: models.User = Depends(staff), db: Session = Depends(get_db)):
    try:
        support.answer(db, user, ticket_id, reply)
    except Invalid as e:
        return redirect(f"/operator/support?error={e.code}#ticket-{ticket_id}")
    return redirect("/operator/support?answered=1")
