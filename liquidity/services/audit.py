"""Журнал действий сотрудников. Пишется в той же транзакции, что и само действие."""
from sqlalchemy.orm import Session

from liquidity import models


def record(db: Session, actor, action: str, link: str = None):
    if actor is not None and actor.role in ("admin", "operator"):
        db.add(models.AuditEntry(actor_id=actor.id, action=action[:500], link=link))


def latest(db: Session, limit: int = 200):
    return db.query(models.AuditEntry).order_by(models.AuditEntry.created_at.desc(),
                                                models.AuditEntry.id.desc()).limit(limit).all()
