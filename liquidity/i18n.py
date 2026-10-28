"""Английская версия для зарубежных кредиторов.

Шаблоны написаны по-русски и не меняются: готовый HTML переводится на выходе по словарю (i18n_en.py).
Переводятся текст между тегами, пользовательские подписи в атрибутах (placeholder, title, aria-label,
тексты подтверждений) и строковые литералы в скриптах, если они целиком есть в словаре.
Названия компаний, назначение заявок и сообщения чата остаются как ввёл пользователь.
Английский включается на публичных страницах и в кабинете кредитора; у заёмщика и сотрудников всегда русский.
"""
import html
import re
from contextvars import ContextVar

from liquidity.i18n_en import EN, PATTERNS

LANG = ContextVar("lang", default="ru")
LANGS = ("ru", "en")

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_SCRIPT = re.compile(r"(<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>)", re.S | re.I)
_TEXT = re.compile(r">([^<]+)<")
_ATTR = re.compile(r'(\s(?:placeholder|title|aria-label|alt|content|data-confirm|data-confirm-text|data-confirm-ok|'
                   r'data-tip|data-text|data-accent)=")([^"]*)(")')
_JS_STRING = re.compile(r"'([^'\\\n]*[А-Яа-яЁё][^'\\\n]*)'")
_COMPILED = [(re.compile(p), r) for p, r in PATTERNS]
# Числа в русском формате на витрине: «500 000 EUR», «11,6%» -> «500,000 EUR», «11.6%»
_NUMERIC = re.compile(r"[\d\u00a0 ]*\d(?:,\d+)?%?(?:[\u00a0 ](?:EUR|USD|CNY|₽))?")


def _en_number(text: str) -> str:
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)
    return re.sub(r"(\d)[\u00a0 ](?=\d{3}(?:\D|$))", r"\1,", text)


def is_en() -> bool:
    return LANG.get() == "en"


def pick(request, user=None) -> str:
    """Язык страницы: английский по cookie lang=en — для гостей и кредиторов."""
    if request is None or request.cookies.get("lang") != "en":
        return "ru"
    return "en" if user is None or getattr(user, "role", None) == "creditor" else "ru"


def translate(text: str) -> str:
    """Перевод одной строки: словарь, затем шаблоны с числами, затем по частям через « · »."""
    core = text.strip()
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    if core and _NUMERIC.fullmatch(core):
        return lead + _en_number(core) + trail
    if not core or not _CYRILLIC.search(core):
        return text
    if core in EN:
        result = EN[core]
    else:
        result = None
        for pattern, repl in _COMPILED:
            match = pattern.fullmatch(core)
            if match:
                result = match.expand(repl) if isinstance(repl, str) else repl(match)
                break
        if result is None and " · " in core:
            parts = core.split(" · ")
            translated = [translate(p) for p in parts]
            if translated != parts:
                result = " · ".join(translated)
        if result is None:
            return text
    return lead + result + trail


def _text_node(match):
    raw = match.group(1)
    plain = html.unescape(raw)
    out = translate(plain)
    return ">" + (raw if out == plain else html.escape(out, quote=False)) + "<"


def _attr(match):
    plain = html.unescape(match.group(2))
    out = translate(plain)
    return match.group(1) + (match.group(2) if out == plain else html.escape(out, quote=True)) + match.group(3)


def _script(block: str) -> str:
    if block.lower().startswith("<style"):
        return block
    block = block.replace("'ru-RU'", "'en-US'")
    return _JS_STRING.sub(lambda m: "'" + EN[m.group(1)].replace("'", "\\'") + "'" if m.group(1) in EN else m.group(0), block)


def translate_html(page: str) -> str:
    page = page.replace('<html lang="ru">', '<html lang="en">', 1)
    pieces = _SCRIPT.split(page)
    for i, piece in enumerate(pieces):
        if i % 2:
            pieces[i] = _script(piece)
        else:
            piece = _TEXT.sub(_text_node, piece)
            pieces[i] = _ATTR.sub(_attr, piece)
    return "".join(pieces)


def untranslated(page: str):
    """Оставшиеся русские фрагменты — для проверки полноты словаря в тестах."""
    found = []
    for i, piece in enumerate(_SCRIPT.split(page)):
        if i % 2:
            continue
        for m in _TEXT.finditer(piece):
            text = html.unescape(m.group(1)).strip()
            if _CYRILLIC.search(text):
                found.append(text)
        for m in _ATTR.finditer(piece):
            text = html.unescape(m.group(2)).strip()
            if _CYRILLIC.search(text):
                found.append(text)
    return found
