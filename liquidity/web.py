"""Шаблоны Jinja2: фильтры форматирования и общие переменные. Autoescape включён — защита от XSS."""
import re
from decimal import Decimal

from jinja2 import pass_context
from markupsafe import Markup, escape

from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from liquidity import config, i18n
from liquidity.security import csrf_field
from liquidity.services import sla
from liquidity.services.files import MAX_FILE_BYTES, display_name


def format_money(value):
    """500000 -> «500 000», 612500.5 -> «612 500,50» (неразрывные пробелы между разрядами)."""
    if value is None:
        return "—"
    value = Decimal(str(value)).quantize(Decimal("0.01"))
    whole, frac = f"{value:.2f}".split(".")
    if i18n.is_en():  # английская версия: 500,000.50
        whole = f"{int(whole):,}"
        return whole if frac == "00" else f"{whole}.{frac}"
    whole = f"{int(whole):,}".replace(",", " ")
    return whole if frac == "00" else f"{whole},{frac}"


def format_percent(value):
    if value is None:
        return "—"
    text = f"{Decimal(str(value)).normalize():f}"
    return (text if i18n.is_en() else text.replace(".", ",")) + "%"


def format_rub_short(value):
    """2 912 345 -> «≈ 2,9 млн ₽», 87 500 -> «≈ 87,5 тыс. ₽» — для подсказок рядом с валютными суммами."""
    if value is None:
        return ""
    if i18n.is_en():
        for limit, unit in ((1e9, "bn"), (1e6, "M"), (1e3, "K")):
            if abs(value) >= limit:
                return f"≈ ₽{value / limit:.1f}{unit}".replace(".0" + unit, unit)
        return f"≈ ₽{format_money(round(value))}"
    for limit, unit in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if abs(value) >= limit:
            short = f"{value / limit:.1f}".replace(".", ",").replace(",0", "")
            return f"≈ {short} {unit} ₽"
    return f"≈ {format_money(round(value))} ₽"


_TERM_RE = re.compile(r"(?<![\w])(" + "|".join(map(re.escape, config.GLOSSARY)) + r")(?![\w])")


def with_terms(value):
    """Оборачивает сокращения из словаря в подсказки. Текст экранируется; в готовом HTML заменяется только
    текст между тегами, атрибуты не трогаются."""
    html = str(value) if isinstance(value, Markup) else str(escape(value))
    if i18n.is_en():  # подсказки написаны по-русски: в английской версии текст без них
        return Markup(html)
    parts = re.split(r"(<[^>]+>)", html)
    inside_abbr = False
    for i, part in enumerate(parts):
        if part.startswith("<"):
            inside_abbr = part.startswith("<abbr") or (inside_abbr and not part.startswith("</abbr"))
            continue
        if not inside_abbr:
            parts[i] = _TERM_RE.sub(lambda m: (f'<abbr class="term" tabindex="0" data-tip="{escape(config.GLOSSARY[m.group(1)])}">'
                                              f'{m.group(1)}</abbr>'), part)
    return Markup("".join(parts))


templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
templates.env.filters["money"] = format_money
templates.env.filters["percent"] = format_percent
templates.env.filters["rub"] = format_rub_short
# pass_context: иначе Jinja вычислит фильтр над строковой константой один раз при компиляции шаблона,
# и подсказки «застрянут» в языке первого показа
templates.env.filters["terms"] = pass_context(lambda _ctx, value: with_terms(value))
templates.env.filters["filename"] = display_name
templates.env.globals.update(
    APPLICATION_STATUSES=config.APPLICATION_STATUSES, OFFER_STATUSES=config.OFFER_STATUSES,
    COUNTRY_NAMES=config.COUNTRY_NAMES, TRANSFER_METHODS=config.TRANSFER_METHODS, ROLE_NAMES=config.ROLE_NAMES,
    KYC_STATUSES=config.KYC_STATUSES, PLATFORM_COMMISSION=config.PLATFORM_COMMISSION, DEMO_MODE=config.DEMO_MODE, SITE_URL=config.SITE_URL,
    csrf_field=csrf_field, sla_status=sla.status, SLA_HOURS=config.SLA_HOURS, MAX_UPLOAD_BYTES=MAX_FILE_BYTES,
    APP_MIN_AMOUNT=config.APP_MIN_AMOUNT, APP_MAX_AMOUNT=config.APP_MAX_AMOUNT, PURPOSE_MIN_LENGTH=config.PURPOSE_MIN_LENGTH,
    OFFER_RATE_MAX=config.OFFER_RATE_MAX, OFFER_LENDER_COMM_MAX=config.OFFER_LENDER_COMM_MAX,
)


def render(request, name: str, status_code: int = 200, **context):
    lang = i18n.pick(request, context.get("user"))
    token = i18n.LANG.set(lang)
    try:
        response = templates.TemplateResponse(request=request, name=name, context={**context, "lang": lang},
                                              status_code=status_code)
        if lang == "en":
            return HTMLResponse(i18n.translate_html(response.body.decode("utf-8")), status_code=status_code)
        return response
    finally:
        i18n.LANG.reset(token)


def redirect(url: str):
    return RedirectResponse(url=url, status_code=303)
