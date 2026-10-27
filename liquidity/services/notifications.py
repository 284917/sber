"""Уведомления в кабинете: о событиях узнаёшь сразу, а не только зайдя на нужную страницу.

Сервисы вызывают notify() в той же транзакции, что и само действие: уведомление сохраняется вместе с ним.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from liquidity import models


def notify(db: Session, user_id: int, text: str, link: str = None, kind: str = "info"):
    """kind: info / ok / warn / bad — определяет иконку и цвет."""
    if user_id:
        db.add(models.Notification(user_id=user_id, text=text[:500], link=link, kind=kind))


def notify_staff(db: Session, text: str, link: str = None, kind: str = "info", roles=("operator",)):
    for user in db.query(models.User).filter(models.User.role.in_(roles)):
        notify(db, user.id, text, link, kind)


def unread_count(db: Session, user_id: int) -> int:
    return db.query(models.Notification).filter(models.Notification.user_id == user_id,
                                                models.Notification.read_at.is_(None)).count()


def latest(db: Session, user_id: int, limit: int = 50):
    return db.query(models.Notification).filter(models.Notification.user_id == user_id).order_by(
        models.Notification.created_at.desc(), models.Notification.id.desc()).limit(limit).all()


def unread(db: Session, user_id: int, limit: int = 3):
    return db.query(models.Notification).filter(models.Notification.user_id == user_id,
                                                models.Notification.read_at.is_(None)).order_by(
        models.Notification.created_at.desc(), models.Notification.id.desc()).limit(limit).all()


def mark_all_read(db: Session, user_id: int):
    db.query(models.Notification).filter(models.Notification.user_id == user_id,
                                         models.Notification.read_at.is_(None)).update(
        {"read_at": datetime.utcnow()}, synchronize_session=False)
    db.commit()


def mark_read(db: Session, user_id: int, notification_id: int):
    item = db.get(models.Notification, notification_id)
    if item and item.user_id == user_id and item.read_at is None:
        item.read_at = datetime.utcnow()
        db.commit()
    return item
