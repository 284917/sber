"""Верификация компании (KYC/AML): загрузка пакета документов и отправка на проверку.

Загрузка работает и обычной формой (с перезагрузкой страницы), и в фоне из браузера:
если запрос просит JSON, ответ — состояние документа и прогресс пакета, страница не перезагружается.
"""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.database import get_db
from liquidity.security import require_role
from liquidity.services import Invalid, kyc
from liquidity.services.files import display_name
from liquidity.web import redirect, render

router = APIRouter(prefix="/kyc", tags=["KYC"])
company = require_role("borrower", "creditor")


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


@router.get("", summary="Страница верификации")
def kyc_page(request: Request, user: models.User = Depends(company), db: Session = Depends(get_db)):
    docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
    return render(request, "kyc.html", user=user, user_docs=docs, required_docs=config.KYC_DOCS.get(user.role, []))


@router.post("/upload", summary="Загрузить или заменить документ (PDF)")
def upload(request: Request, doc_type: str = Form(...), file: UploadFile = File(...),
           user: models.User = Depends(company), db: Session = Depends(get_db)):
    try:
        document = kyc.upload_document(db, user, doc_type, file)
    except Invalid as e:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": e.message}, status_code=400)
        return redirect(f"/kyc?error={e.code}")
    if wants_json(request):
        done, total = kyc.progress(db, user)
        filename = display_name(document.file_path)
        return {"ok": True, "doc_type": doc_type, "filename": filename, "url": "/" + document.file_path,
                "done": done, "total": total}
    return redirect("/kyc")


@router.post("/submit", summary="Отправить пакет на проверку администратору")
def submit(user: models.User = Depends(company), db: Session = Depends(get_db)):
    try:
        kyc.submit_for_review(db, user)
    except Invalid as e:
        return redirect(f"/kyc?error={e.code}")
    return redirect("/kyc?success=1")
