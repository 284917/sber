"""История заявки: журнал изменений статуса и ключевых действий участников."""
from sqlalchemy.orm import Session

from liquidity import models


def log_event(db: Session, application_id: int, actor, text: str):
    db.add(models.ApplicationEvent(application_id=application_id, actor_id=actor.id if actor else None, text=text))


def get_events(db: Session, application_id: int):
    return db.query(models.ApplicationEvent).filter(
        models.ApplicationEvent.application_id == application_id
    ).order_by(models.ApplicationEvent.created_at, models.ApplicationEvent.id).all()
