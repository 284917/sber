"""Курсы ЦБ РФ для рублёвого эквивалента.

Источник — официальный XML ЦБ (cbr.ru/scripts/XML_daily.asp), запасной — его зеркало cbr-xml-daily.ru.
Курсы кэшируются в памяти на 6 часов и сохраняются в таблицу fx_rates: если ЦБ недоступен,
показываем последний сохранённый курс с его датой. Если курсов нет совсем — рубли просто не показываются.
"""
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from liquidity import config, models

CBR_XML_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_JSON_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_TTL = timedelta(hours=6)
RETRY_AFTER = timedelta(minutes=10)
TIMEOUT_SECONDS = 3

_cache = {"at": None, "value": None}


def parse_cbr_xml(content: bytes):
    """XML ЦБ -> (дата, {код: рублей за 1 единицу}). Номинал учитывается: курс за 10 или 100 единиц делится."""
    root = ET.fromstring(content)
    rate_date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    rates = {}
    for valute in root.findall("Valute"):
        code = valute.findtext("CharCode")
        if code in config.CURRENCIES:
            value = Decimal(valute.findtext("Value").replace(",", "."))
            rates[code] = value / Decimal(valute.findtext("Nominal"))
    return rate_date, rates


def parse_cbr_json(content: bytes):
    data = json.loads(content)
    rate_date = datetime.fromisoformat(data["Date"]).date()
    rates = {code: Decimal(str(v["Value"])) / Decimal(str(v["Nominal"]))
             for code, v in data["Valute"].items() if code in config.CURRENCIES}
    return rate_date, rates


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "liquidity-platform/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def fetch_from_cbr():
    try:
        return parse_cbr_xml(_download(CBR_XML_URL))
    except Exception:  # noqa: BLE001 — переходим на запасной источник
        return parse_cbr_json(_download(CBR_JSON_URL))


def _save(db: Session, rate_date: date, rates: dict):
    for code, rate in rates.items():
        row = db.query(models.FxRate).filter_by(rate_date=rate_date, currency_code=code).first()
        if row:
            row.rate = rate
        else:
            db.add(models.FxRate(rate_date=rate_date, currency_code=code, rate=rate))
    db.commit()


def _latest_saved(db: Session):
    latest = db.query(models.FxRate).order_by(models.FxRate.rate_date.desc()).first()
    if not latest:
        return None
    rows = db.query(models.FxRate).filter_by(rate_date=latest.rate_date).all()
    return {"date": latest.rate_date, "rates": {r.currency_code: Decimal(r.rate) for r in rows}}


def current_rates(db: Session):
    """{"date": дата курса, "rates": {"EUR": Decimal, ...}} или None, если курсов нет."""
    now = datetime.utcnow()
    if _cache["at"] and now - _cache["at"] < CACHE_TTL:
        return _cache["value"]
    value, cached_at = None, now
    try:
        rate_date, rates = fetch_from_cbr()
        if rates:
            _save(db, rate_date, rates)
            value = {"date": rate_date, "rates": rates}
    except Exception:  # noqa: BLE001 — ЦБ недоступен: берём последний сохранённый курс
        db.rollback()
    if value is None:
        value = _latest_saved(db)
        cached_at = now - CACHE_TTL + RETRY_AFTER  # неудачу помним недолго, чтобы скоро повторить запрос
    _cache.update(at=cached_at, value=value)
    return value


def to_rub(amount, currency: str, fx):
    if amount is None or not fx or currency not in fx["rates"]:
        return None
    return float(Decimal(str(amount)) * fx["rates"][currency])


def reset_cache():
    _cache.update(at=None, value=None)
