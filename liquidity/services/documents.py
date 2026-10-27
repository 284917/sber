"""PDF-сводка согласованной сделки: стороны, условия, полная стоимость, график платежей, подтверждение."""
import os
from datetime import datetime

from fpdf import FPDF
from fpdf.fonts import FontFace

from liquidity import config
from liquidity.services.costs import offer_cost, payment_schedule
from liquidity.services.fx import to_rub
from liquidity.web import format_money, format_percent

FONT_DIR = os.path.join(config.STATIC_DIR, "fonts")
INK, MUTED, LINE, BRAND = (11, 11, 12), (110, 115, 122), (230, 232, 235), (33, 160, 56)


def _plain(text: str) -> str:
    return (text or "").replace(" ", " ")


class DealPDF(FPDF):
    def header(self):
        self.set_fill_color(*BRAND)
        self.rect(15, 12, 8, 8, style="F", round_corners=True, corner_radius=2)
        # стрелка роста, как в логотипе сайта (координаты из SVG 24x24, масштаб 1/3 мм)
        self.set_draw_color(255, 255, 255)
        self.set_line_width(0.6)
        to_mm = lambda pts: [(15 + x / 3, 12 + y / 3) for x, y in pts]  # noqa: E731
        self.polyline(to_mm([(5, 16), (9.5, 11.5), (13, 15), (19, 8.5)]))
        self.polyline(to_mm([(15, 8.5), (19, 8.5), (19, 12.5)]))
        self.set_line_width(0.2)
        self.set_xy(26, 12)
        self.set_font("Manrope", "B", 11)
        self.set_text_color(*INK)
        self.cell(0, 4, "Платформа ликвидности", new_x="LMARGIN", new_y="NEXT")
        self.set_x(26)
        self.set_font("Manrope", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 4, "Трансграничное финансирование между юрлицами", new_x="LMARGIN", new_y="NEXT")
        self.ln(8)

    def footer(self):
        self.set_y(-14)
        self.set_font("Manrope", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 4, f"Сводка сформирована {datetime.utcnow():%d.%m.%Y %H:%M} UTC. Не заменяет договор займа, "
                        f"условия которого стороны загрузили в комнату сделки.", align="L")
        self.cell(0, 4, f"{self.page_no()}/{{nb}}", align="R")

    def section(self, title: str, keep: float = 30):
        """Заголовок раздела; если до конца страницы меньше keep мм, раздел начинается с новой страницы."""
        if self.get_y() + keep > self.h - self.b_margin:
            self.add_page()
        self.ln(4)
        self.set_font("Manrope", "B", 11)
        self.set_text_color(*INK)
        self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def kv(self, rows):
        for key, value in rows:
            self.set_font("Manrope", "", 9)
            self.set_text_color(*MUTED)
            self.cell(60, 6, key)
            self.set_font("Manrope", "B", 9)
            self.set_text_color(*INK)
            self.multi_cell(0, 6, _plain(value), new_x="LMARGIN", new_y="NEXT")


def deal_summary_pdf(application, offer, events, confirmed_by, fx) -> bytes:
    cur = application.currency_code
    cost = offer_cost(offer, application)
    schedule = payment_schedule(offer, application)
    borrower, creditor = application.borrower, offer.sender

    pdf = DealPDF(format="A4")
    pdf.add_font("Manrope", "", os.path.join(FONT_DIR, "manrope-400.ttf"))
    pdf.add_font("Manrope", "B", os.path.join(FONT_DIR, "manrope-700.ttf"))
    pdf.set_margins(15, 12, 15)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.set_title(f"Сводка сделки №{application.id}")
    pdf.set_author("Платформа ликвидности")
    pdf.add_page()

    pdf.set_font("Manrope", "B", 20)
    pdf.set_text_color(*INK)
    pdf.cell(0, 10, f"Сводка сделки №{application.id}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Manrope", "", 10)
    pdf.set_text_color(*BRAND)
    confirmed = application.confirmed_at.strftime("%d.%m.%Y %H:%M") if application.confirmed_at else "—"
    pdf.cell(0, 6, f"Статус: {config.APPLICATION_STATUSES.get(application.status, application.status)} · "
                   f"подтверждена {confirmed}", new_x="LMARGIN", new_y="NEXT")

    pdf.section("Стороны")
    pdf.kv([
        ("Заёмщик", f"{borrower.display_name} · ИНН {borrower.registration_number or '—'} · Россия"),
        ("Кредитор", f"{creditor.display_name} · {config.COUNTRY_NAMES.get(creditor.country_code, '—')} · "
                     f"рег. номер {creditor.registration_number or '—'}"),
    ])

    pdf.section("Условия")
    pdf.kv([
        ("Сумма к выдаче", f"{format_money(offer.funding_amount)} {cur}"),
        ("Ставка", f"{format_percent(offer.annual_interest_rate)} годовых"),
        ("Комиссия за организацию", f"{format_percent(offer.total_commission)} (кредитора "
                                    f"{format_percent(offer.lender_commission)} + платформы "
                                    f"{format_percent(config.PLATFORM_COMMISSION)})"),
        ("Срок", f"{application.term_months} мес." if application.term_months else "—"),
        ("Страна получения", config.COUNTRY_NAMES.get(application.target_jurisdiction, "—")),
        ("Способ передачи", config.TRANSFER_METHODS.get(application.transfer_method, "—")),
        ("Назначение", application.purpose),
        ("Дополнительные условия", offer.extra_terms or "—"),
    ])

    pdf.section("Полная стоимость")
    rows = [("Полная стоимость", f"{format_percent(round(cost['effective_rate'], 2))} годовых"
             if cost["effective_rate"] is not None else "—"),
            ("Проценты за срок", f"{format_money(cost['interest'])} {cur}" if cost["interest"] is not None else "—"),
            ("Комиссия", f"{format_money(cost['commission'])} {cur}"),
            ("Переплата", f"{format_money(cost['overpayment'])} {cur}" if cost["overpayment"] is not None else "—")]
    rub = to_rub(cost["overpayment"], cur, fx)
    if rub is not None:
        rate = fx["rates"][cur]
        rows.append(("Переплата в рублях", f"{format_money(round(rub))} ₽ по курсу ЦБ РФ "
                                           f"{format_money(rate)} ₽ за 1 {cur} на {fx['date']:%d.%m.%Y}"))
    pdf.kv(rows)

    if schedule:
        pdf.section("График платежей")
        pdf.set_font("Manrope", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 4, "Комиссия при выдаче, проценты ежемесячно на всю сумму, основной долг в последний месяц.",
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_text_color(*INK)
        heading = FontFace(emphasis="BOLD", color=MUTED, fill_color=(250, 250, 251))
        with pdf.table(col_widths=(22, 34, 34, 34, 36), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"),
                       headings_style=heading, line_height=5.5, borders_layout="HORIZONTAL_LINES") as table:
            table.row(["Платёж", f"Проценты, {cur}", f"Основной долг, {cur}", f"Комиссия, {cur}", f"Итого, {cur}"])
            for r in schedule["rows"]:
                table.row([r["label"], format_money(r["interest"]), format_money(r["principal"]),
                           format_money(r["commission"]), format_money(r["payment"])])
            t = schedule["totals"]
            table.row(["Всего", format_money(t["interest"]), format_money(t["principal"]),
                       format_money(t["commission"]), format_money(t["payment"])],
                      style=FontFace(emphasis="BOLD"))

    pdf.section("Подтверждение", keep=36)
    pdf.kv([("Подтвердил оператор", confirmed_by.email if confirmed_by else "—"), ("Дата", confirmed)])

    if events:
        pdf.section("История сделки", keep=24)
        for event in events:
            pdf.set_font("Manrope", "", 8)
            pdf.set_text_color(*MUTED)
            pdf.cell(30, 5, event.created_at.strftime("%d.%m.%Y %H:%M") if event.created_at else "")
            pdf.set_text_color(*INK)
            pdf.multi_cell(0, 5, event.text, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
