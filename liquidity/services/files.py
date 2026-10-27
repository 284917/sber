"""Загрузка и выдача файлов: безопасные имена, содержимое в БД, доступ только участникам."""
import mimetypes
import os
import re
import uuid

from fastapi import UploadFile
from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Forbidden, Invalid, NotFound

MAX_FILE_BYTES = 4 * 1024 * 1024  # у Vercel лимит тела запроса 4,5 МБ


def save_upload(db: Session, file: UploadFile, prefix: str) -> str:
    """Сохраняет файл в БД под безопасным уникальным именем; возвращает путь вида uploads/<имя>."""
    original = os.path.basename((file.filename or "").replace("\\", "/"))
    safe_name = re.sub(r"[^\w.\-]", "_", original)[:100] or "file"
    content = file.file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise Invalid("file_size", "Файл больше 4 МБ")
    path = f"{config.UPLOAD_DIR}/{prefix}_{uuid.uuid4().hex[:12]}_{safe_name}"
    db.add(models.StoredFile(path=path, content=content))
    return path


def load_file(db: Session, filename: str, user: models.User, can_access_deal):
    """Возвращает (содержимое | путь на диске, media_type). Документ KYC видят владелец и сотрудники,
    файл из сделки — её участники и сотрудники."""
    if filename != os.path.basename(filename):
        raise NotFound()
    path = f"{config.UPLOAD_DIR}/{filename}"

    doc = db.query(models.Document).filter(models.Document.file_path == path).first()
    msg = db.query(models.Message).filter(models.Message.file_path == path).first()
    if doc:
        if doc.user_id != user.id and user.role not in config.STAFF_ROLES:
            raise Forbidden()
    elif msg:
        can_access_deal(msg.application_id)
    else:
        raise NotFound()

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    stored = db.get(models.StoredFile, path)
    if stored:
        return stored.content, media_type
    if os.path.isfile(path):
        return path, media_type
    raise NotFound()
