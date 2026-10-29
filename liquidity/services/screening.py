"""Автоматическая проверка заявок и предложений вместо ручной модерации каждой из них.

Жёсткие правила отклоняют сразу: пользователь видит, что исправить, оператор ничего не делает.
Мягкие правила (флаги) отправляют заявку или предложение на ручную проверку — только подозрительное.
"""
import re
from decimal import Decimal
from statistics import median

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.services import Invalid

LETTERS = re.compile(r"[a-zа-яё]", re.IGNORECASE)


def stop_word(text: str):
    """Первое стоп-слово в тексте (по началу слова) или None."""
    low = (text or "").lower()
    for word in config.STOP_WORDS:
        if re.search(r"(?<![a-zа-яё])" + re.escape(word), low):
            return word
    return None


def fmt(value) -> str:
    return f"{Decimal(value):,.0f}".replace(",", " ")


# --- Заявка ---
def check_application(amount: float, currency: str, purpose: str):
    """Жёсткие правила заявки; нарушение — Invalid с понятным текстом."""
    if not (config.APP_MIN_AMOUNT <= amount <= config.APP_MAX_AMOUNT):
        raise Invalid("amount", f"Сумма заявки — от {fmt(config.APP_MIN_AMOUNT)} до {fmt(config.APP_MAX_AMOUNT)} {currency}")
    if len(purpose) < config.PURPOSE_MIN_LENGTH or len(LETTERS.findall(purpose)) < config.PURPOSE_MIN_LENGTH // 2:
        raise Invalid("purpose", f"Опишите назначение подробнее: не короче {config.PURPOSE_MIN_LENGTH} символов, "
                                 "например контракт и контрагента")
    word = stop_word(purpose)
    if word:
        raise Invalid("stop_word", f"Назначение содержит недопустимую цель («{word}»). Такие заявки платформа не публикует")


def application_flags(db: Session, borrower: models.User, amount: float, currency: str) -> list:
    """Причины отправить заявку оператору. Пустой список — публикуем сразу."""
    flags = []
    # Компания ещё ни разу не публиковала заявку: дата окончания ставится только при публикации
    published_before = db.query(models.Application).filter(models.Application.user_id == borrower.id,
                                                           models.Application.valid_until.isnot(None)).first()
    if not published_before:
        flags.append("первая заявка компании")
    limit = config.APP_REVIEW_AMOUNT.get(currency)
    if limit and amount > limit:
        flags.append(f"сумма выше {fmt(limit)} {currency}")
    return flags


# --- Предложение ---
def market_rate(db: Session, currency: str) -> Decimal:
    """Медиана ставок показанных заёмщикам предложений в валюте, иначе ориентир из настроек."""
    rates = [o.annual_interest_rate for o in db.query(models.Offer).join(models.Application).filter(
        models.Application.currency_code == currency,
        models.Offer.status.in_(["shown", "accepted"]))]
    if len(rates) >= config.MARKET_MIN_SAMPLES:
        return Decimal(median(rates))
    return Decimal(str(config.MARKET_RATE_FALLBACK.get(currency, 8.0)))


def check_offer(rate: float, lender_comm: float, extra_terms: str):
    if not (0 < rate <= config.OFFER_RATE_MAX) or not (0 <= lender_comm <= config.OFFER_LENDER_COMM_MAX):
        raise Invalid("offer", f"Ставка — больше 0 и не выше {config.OFFER_RATE_MAX}% годовых, "
                               f"комиссия кредитора — от 0 до {config.OFFER_LENDER_COMM_MAX}%")
    word = stop_word(extra_terms)
    if word:
        raise Invalid("stop_word", f"Дополнительные условия содержат недопустимое слово («{word}»)")


def offer_flags(db: Session, application: models.Application, rate: float) -> list:
    market = market_rate(db, application.currency_code)
    if abs(Decimal(str(rate)) - market) > config.RATE_DEVIATION:
        side = "выше" if rate > market else "ниже"
        rate_text, market_text = f"{rate:g}".replace(".", ","), f"{market:.1f}".replace(".", ",")
        return [f"ставка {rate_text}% заметно {side} рынка ({market_text}% в {application.currency_code})"]
    return []
