"""Проверка компании по ИНН через DaData (данные ЕГРЮЛ): название, адрес, статус, ОГРН.

Работает, только если задан DADATA_API_KEY. Ответы кэшируются в памяти на сутки,
сетевые ошибки не мешают регистрации: форма просто не заполняется автоматически.
"""
import json
import urllib.request
from datetime import datetime, timedelta

from liquidity import config

URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
TIMEOUT_SECONDS = 3
CACHE_TTL = timedelta(hours=24)
STATUS_NAMES = {"ACTIVE": "действующая", "LIQUIDATING": "ликвидируется", "LIQUIDATED": "ликвидирована",
                "BANKRUPT": "банкротство", "REORGANIZING": "в процессе присоединения"}
LOOKUPS_PER_WINDOW = 30
WINDOW = timedelta(minutes=10)
_cache = {}
_hits = {}


def enabled() -> bool:
    return bool(config.DADATA_API_KEY)


def allow(ip: str, now: datetime = None) -> bool:
    """Не больше LOOKUPS_PER_WINDOW запросов с одного IP за WINDOW (в памяти процесса)."""
    now = now or datetime.utcnow()
    recent = [t for t in _hits.get(ip, []) if now - t < WINDOW]
    if len(recent) >= LOOKUPS_PER_WINDOW:
        _hits[ip] = recent
        return False
    _hits[ip] = recent + [now]
    return True


def _request(inn: str) -> dict:
    request = urllib.request.Request(URL, data=json.dumps({"query": inn, "count": 1}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "Authorization": f"Token {config.DADATA_API_KEY}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def parse(payload: dict):
    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return None
    item = suggestions[0]
    data = item.get("data") or {}
    state = (data.get("state") or {}).get("status")
    return {
        "name": (data.get("name") or {}).get("short_with_opf") or item.get("value"),
        "full_name": (data.get("name") or {}).get("full_with_opf"),
        "ogrn": data.get("ogrn"),
        "address": ((data.get("address") or {}).get("value")),
        "status": state,
        "status_name": STATUS_NAMES.get(state, state or "—"),
        "active": state == "ACTIVE",
    }


def find_by_inn(inn: str):
    """dict с данными компании, None если не найдена; исключение не пробрасывается."""
    if not enabled():
        return None
    cached = _cache.get(inn)
    if cached and datetime.utcnow() - cached[0] < CACHE_TTL:
        return cached[1]
    try:
        result = parse(_request(inn))
    except Exception:  # noqa: BLE001 — сеть или DaData недоступны: просто без автозаполнения
        return None
    _cache[inn] = (datetime.utcnow(), result)
    return result
