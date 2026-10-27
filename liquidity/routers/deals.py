"""Комната сделки и файлы: доступ только у участников сделки и сотрудников."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from liquidity import models
from liquidity.database import get_db
from liquidity.security import get_current_user
from liquidity.services import Invalid, applications, deals, files
from liquidity.web import redirect, render

router = APIRouter(tags=["Сделка"])


@router.get("/deal/{app_id}", summary="Комната сделки: этапы, чат, договор")
def deal_room(app_id: int, request: Request, user: models.User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    application = deals.get_for_user(db, user, app_id)
    messages, main_contract, other_documents = deals.room(db, application)
    return render(request, "deal.html", user=user, application=application, messages=messages,
                  main_contract=main_contract, other_documents=other_documents,
                  offer=deals.accepted_offer(db, app_id), stages=applications.stages(db, application))


@router.post("/deal/{app_id}/send", summary="Сообщение в чат сделки (с файлом или без)")
def send_message(app_id: int, text: str = Form(None), file: UploadFile = File(None),
                 user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        deals.send_message(db, user, app_id, text, file)
    except Invalid as e:
        return redirect(f"/deal/{app_id}?error={e.code}")
    return redirect(f"/deal/{app_id}")


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
