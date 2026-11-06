"""Обращения в поддержку: пишет любой пользователь или гость без входа, отвечает оператор."""
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid, NotFound
from liquidity.services.audit import record
from liquidity.services.notifications import notify, notify_staff

GUEST_LIMIT = 5  # обращений с одного адреса за GUEST_WINDOW: форма доступна без входа
GUEST_WINDOW = timedelta(hours=1)
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _check_guest_limit(db: Session, ip: str, now: datetime):
    key = f"support:{ip or 'unknown'}"
    row = db.get(models.LoginAttempt, key)
    if not row:
        row = models.LoginAttempt(key=key, failures=0, window_start=now)
        db.add(row)
    if now - row.window_start > GUEST_WINDOW:
        row.failures, row.window_start = 0, now
    if row.failures >= GUEST_LIMIT:
        raise Invalid("limit", "Слишком много обращений с этого адреса. Попробуйте через час")
    row.failures += 1


def create(db: Session, user, *, topic: str, text: str, contact_email: str = "", ip: str = "") -> models.SupportTicket:
    text, contact_email = (text or "").strip(), (contact_email or "").strip()
    if topic not in config.SUPPORT_TOPICS:
        raise Invalid("topic", "Выберите тему обращения")
    if not (config.SUPPORT_MIN_LENGTH <= len(text) <= config.SUPPORT_MAX_LENGTH):
        raise Invalid("text", f"Опишите вопрос: от {config.SUPPORT_MIN_LENGTH} до {config.SUPPORT_MAX_LENGTH} символов")
    if user is None:
        if not EMAIL.match(contact_email) or len(contact_email) > 255:
            raise Invalid("email", "Укажите почту, на которую ответить")
        _check_guest_limit(db, ip, datetime.utcnow())
    ticket = models.SupportTicket(user_id=user.id if user else None, topic=topic, text=text,
                                  contact_email=None if user else contact_email)
    db.add(ticket)
    db.flush()
    author = user.display_name if user else contact_email
    notify_staff(db, f"Новое обращение в поддержку №{ticket.id} от {author}: {config.SUPPORT_TOPICS[topic]}",
                 f"/operator/support#ticket-{ticket.id}", roles=("operator",))
    db.commit()
    return ticket


def for_user(db: Session, user: models.User):
    return db.query(models.SupportTicket).filter(models.SupportTicket.user_id == user.id).order_by(
        models.SupportTicket.id.desc()).all()


def queue(db: Session):
    """Обращения для оператора: (открытые от старых к новым, последние закрытые)."""
    base = db.query(models.SupportTicket)
    return (base.filter(models.SupportTicket.status == "open").order_by(models.SupportTicket.id).all(),
            base.filter(models.SupportTicket.status == "answered").order_by(models.SupportTicket.id.desc()).limit(30).all())


def answer(db: Session, staff: models.User, ticket_id: int, reply: str):
    """Ответ оператора. Гостю ответ уходит на почту вручную, в обращении он остаётся как запись."""
    ticket = db.get(models.SupportTicket, ticket_id)
    if not ticket:
        raise NotFound()
    reply = (reply or "").strip()
    if ticket.status != "open":
        raise Invalid("closed", "По обращению уже есть ответ")
    if not (1 <= len(reply) <= config.SUPPORT_MAX_LENGTH):
        raise Invalid("reply", "Напишите ответ")
    ticket.reply, ticket.status, ticket.answered_by, ticket.answered_at = reply, "answered", staff.id, datetime.utcnow()
    record(db, staff, f"Ответил на обращение в поддержку №{ticket.id}", "/operator/support")
    if ticket.user_id:
        notify(db, ticket.user_id, f"Поддержка ответила на обращение №{ticket.id}", f"/support#ticket-{ticket.id}", "ok")
    db.commit()
