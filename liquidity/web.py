"""Шаблоны Jinja2: фильтры форматирования и общие переменные. Autoescape включён — защита от XSS."""
from decimal import Decimal

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from liquidity import config
from liquidity.security import csrf_field


def format_money(value):
    """500000 -> «500 000», 612500.5 -> «612 500,50» (неразрывные пробелы между разрядами)."""
    if value is None:
        return "—"
    value = Decimal(str(value)).quantize(Decimal("0.01"))
    whole, frac = f"{value:.2f}".split(".")
    whole = f"{int(whole):,}".replace(",", " ")
    return whole if frac == "00" else f"{whole},{frac}"


def format_percent(value):
    if value is None:
        return "—"
    return f"{Decimal(str(value)).normalize():f}".replace(".", ",") + "%"


templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
templates.env.filters["money"] = format_money
templates.env.filters["percent"] = format_percent
templates.env.globals.update(
    APPLICATION_STATUSES=config.APPLICATION_STATUSES, OFFER_STATUSES=config.OFFER_STATUSES,
    COUNTRY_NAMES=config.COUNTRY_NAMES, TRANSFER_METHODS=config.TRANSFER_METHODS, ROLE_NAMES=config.ROLE_NAMES,
    KYC_STATUSES=config.KYC_STATUSES, PLATFORM_COMMISSION=config.PLATFORM_COMMISSION, DEMO_MODE=config.DEMO_MODE,
    csrf_field=csrf_field,
)


def render(request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)


def redirect(url: str):
    return RedirectResponse(url=url, status_code=303)
