"""Комната сделки и файлы: доступ только у участников сделки и сотрудников."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from liquidity import models
from liquidity.database import get_db
from liquidity.security import get_current_user
from liquidity.services import Invalid, NotFound, applications, deals, files, fx
from liquidity.services.costs import offer_cost
from liquidity.services.documents import contract_draft_pdf, deal_summary_pdf
from liquidity.services.events import get_events
from liquidity.web import redirect, render, templates

router = APIRouter(tags=["Сделка"])


@router.get("/deal/{app_id}", summary="Комната сделки: этапы, чат, договор")
def deal_room(app_id: int, request: Request, user: models.User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    application = deals.get_for_user(db, user, app_id)
    messages, main_contract, other_documents = deals.room(db, application)
    offer = deals.accepted_offer(db, app_id)
    cost = offer_cost(offer, application) if offer else None
    rates = fx.current_rates(db) if offer else None
    return render(request, "deal.html", user=user, application=application, messages=messages,
                  main_contract=main_contract, other_documents=other_documents, offer=offer, cost=cost,
                  overpayment_rub=fx.to_rub(cost["overpayment"], application.currency_code, rates) if cost else None,
                  fx=rates, stages=applications.stages(db, application))


@router.get("/deal/{app_id}/summary.pdf", summary="PDF-сводка согласованной сделки")
def deal_summary(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    application = deals.get_for_user(db, user, app_id)
    offer = deals.accepted_offer(db, app_id)
    if application.status != "agreed" or not offer:
        raise NotFound()
    confirmed_by = db.get(models.User, application.confirmed_by) if application.confirmed_by else None
    pdf = deal_summary_pdf(application, offer, get_events(db, app_id), confirmed_by, fx.current_rates(db))
    filename = f"sdelka-{app_id}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/deal/{app_id}/contract-draft.pdf", summary="Проект договора займа с условиями сделки")
def contract_draft(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    application = deals.get_for_user(db, user, app_id)
    offer = deals.accepted_offer(db, app_id)
    if application.status not in ("in_work", "agreed") or not offer:
        raise NotFound()
    pdf = contract_draft_pdf(application, offer, fx.current_rates(db))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="proekt-dogovora-{app_id}.pdf"'})


def render_messages(messages, user) -> str:
    template = templates.get_template("_message.html")
    return "".join(template.render(msg=m, viewer_id=user.id) for m in messages)


@router.post("/deal/{app_id}/send", summary="Сообщение в чат сделки (с файлом или без)")
def send_message(request: Request, app_id: int, text: str = Form(None), file: UploadFile = File(None),
                 user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    as_json = "application/json" in request.headers.get("accept", "")
    try:
        message = deals.send_message(db, user, app_id, text, file)
    except Invalid as e:
        if as_json:
            return JSONResponse({"ok": False, "error": e.message}, status_code=400)
        return redirect(f"/deal/{app_id}?error={e.code}")
    if as_json:
        return {"ok": True, "id": message.id if message else None,
                "html": render_messages([message], user) if message else ""}
    return redirect(f"/deal/{app_id}")


@router.get("/deal/{app_id}/messages", summary="Новые сообщения чата после указанного (без перезагрузки)")
def new_messages(app_id: int, after: int = 0, user: models.User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    messages = deals.messages_after(db, user, app_id, after)
    return {"html": render_messages(messages, user), "last_id": messages[-1].id if messages else after,
            "contract_changed": any(deals.is_contract(m) for m in messages)}


@router.post("/deal/{app_id}/contract/delete", summary="Удалить договор, чтобы загрузить новый (до подтверждения)")
def delete_contract(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        deals.delete_contract(db, user, app_id)
    except Invalid as e:
        return redirect(f"/deal/{app_id}?error={e.code}")
    return redirect(f"/deal/{app_id}?contract_deleted=1")


@router.post("/deal/{app_id}/upload_contract", summary="Загрузить подписанный договор (PDF)")
def upload_contract(app_id: int, file: UploadFile = File(...), user: models.User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    try:
        deals.upload_contract(db, user, app_id, file)
    except Invalid as e:
        return redirect(f"/deal/{app_id}?error={e.code}")
    return redirect(f"/deal/{app_id}")


@router.post("/deal/{app_id}/call_operator", summary="Позвать оператора в сделку")
def call_operator(app_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    deals.call_operator(db, user, app_id)
    return redirect(f"/deal/{app_id}")


@router.get("/uploads/{filename}", summary="Скачать файл (документ KYC или файл сделки)")
def download(filename: str, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    content, media_type = files.load_file(db, filename, user, lambda app_id: deals.get_for_user(db, user, app_id))
    if isinstance(content, bytes):
        return Response(content=content, media_type=media_type,
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}"})
    return FileResponse(content, filename=filename, content_disposition_type="inline")
