"""Сроки ответа платформы: сколько задача ждёт сотрудника и не вышел ли срок (config.SLA_HOURS)."""
from datetime import datetime

from liquidity import config


def _hours_text(hours: int) -> str:
    if hours < 1:
        return "меньше часа"
    if hours < 48:
        return f"{hours} ч"
    return f"{hours // 24} дн."


def status(since, kind: str, now: datetime = None):
    """{"label", "overdue", "soon", "hours"} для задачи, ждущей с момента since; None, если время неизвестно."""
    if since is None:
        return None
    limit = config.SLA_HOURS[kind]
    hours = int(((now or datetime.utcnow()) - since).total_seconds() // 3600)
    if hours >= limit:
        late = hours - limit
        label = f"Просрочено на {_hours_text(late)}" if late >= 1 else "Срок ответа истёк"
        return {"label": label, "overdue": True, "soon": False, "hours": hours}
    left = limit - hours
    return {"label": f"Ждёт {_hours_text(hours)} · осталось {_hours_text(left)}", "overdue": False,
            "soon": left <= max(2, limit // 6), "hours": hours}


def overdue_count(items, kind: str, now: datetime = None) -> int:
    """Сколько задач просрочено; items — даты, с которых задачи ждут."""
    return sum(1 for since in items if (s := status(since, kind, now)) and s["overdue"])
