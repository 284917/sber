"""PDF-сводка согласованной сделки: стороны, условия, полная стоимость, график платежей, подтверждение."""
import os
from decimal import Decimal
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
    footer_note = ("Сводка сформирована {now:%d.%m.%Y %H:%M} UTC. Не заменяет договор займа, "
                   "условия которого стороны загрузили в комнату сделки.")

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
        self.cell(0, 4, self.footer_note.format(now=datetime.utcnow()), align="L")
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


def _lender_parts(offer):
    """[(кредитор, сумма)]: один займодавец или участники синдиката."""
    return [(o.sender, o.funding_amount) for o in getattr(offer, "parts", [offer])]


def _lender_line(sender) -> str:
    return (f"{sender.display_name} ({config.COUNTRY_NAMES.get(sender.country_code, '—')}, "
            f"регистрационный номер {sender.registration_number or '—'})")


def new_pdf(cls, title: str):
    pdf = cls(format="A4")
    pdf.add_font("Manrope", "", os.path.join(FONT_DIR, "manrope-400.ttf"))
    pdf.add_font("Manrope", "B", os.path.join(FONT_DIR, "manrope-700.ttf"))
    pdf.set_margins(15, 12, 15)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.set_title(title)
    pdf.set_author("Платформа ликвидности")
    pdf.add_page()
    return pdf


def schedule_table(pdf, schedule, cur):
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


def deal_summary_pdf(application, offer, events, confirmed_by, fx) -> bytes:
    cur = application.currency_code
    cost = offer_cost(offer, application)
    schedule = payment_schedule(offer, application)
    borrower = application.borrower

    pdf = new_pdf(DealPDF, f"Сводка сделки №{application.id}")

    pdf.set_font("Manrope", "B", 20)
    pdf.set_text_color(*INK)
    pdf.cell(0, 10, f"Сводка сделки №{application.id}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Manrope", "", 10)
    pdf.set_text_color(*BRAND)
    confirmed = application.confirmed_at.strftime("%d.%m.%Y %H:%M") if application.confirmed_at else "—"
    pdf.cell(0, 6, f"Статус: {config.APPLICATION_STATUSES.get(application.status, application.status)} · "
                   f"подтверждена {confirmed}", new_x="LMARGIN", new_y="NEXT")

    pdf.section("Стороны")
    parts = _lender_parts(offer)
    pdf.kv([("Заёмщик", f"{borrower.display_name} · ИНН {borrower.registration_number or '—'} · Россия")] + [
        ("Кредитор" if len(parts) == 1 else f"Кредитор {i}",
         f"{lender.display_name} · {config.COUNTRY_NAMES.get(lender.country_code, '—')} · "
         f"рег. номер {lender.registration_number or '—'}"
         + ("" if len(parts) == 1 else f" · доля {format_money(amount)} {cur}"))
        for i, (lender, amount) in enumerate(parts, 1)])

    pdf.section("Условия")
    pdf.kv([
        ("Сумма к выдаче", f"{format_money(offer.funding_amount)} {cur}"),
        ("Ставка", f"{format_percent(offer.annual_interest_rate)} годовых"),
        ("Комиссия за организацию", f"{format_percent(offer.total_commission)} (кредитора "
                                    f"{format_percent(offer.lender_commission)} + платформы "
                                    f"{format_percent(Decimal(str(offer.total_commission)) - Decimal(str(offer.lender_commission)))})"),
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
        schedule_table(pdf, schedule, cur)

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


def _signature_row(pdf, signers):
    if pdf.get_y() + 44 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y() + 2
    for x, (role, company) in zip((15, 110), signers):
        pdf.set_text_color(*INK)
        pdf.set_xy(x, y)
        pdf.set_font("Manrope", "B", 9)
        pdf.cell(85, 5, role)
        pdf.set_xy(x, y + 6)
        pdf.set_font("Manrope", "", 9)
        pdf.multi_cell(85, 5, _plain(company))
        pdf.set_xy(x, y + 26)
        pdf.cell(85, 5, "__________________ / __________________")
        pdf.set_xy(x, y + 32)
        pdf.set_text_color(*MUTED)
        pdf.cell(85, 5, "подпись / ФИО, должность")
    pdf.set_xy(15, y + 42)


class ContractPDF(DealPDF):
    footer_note = ("Проект сформирован {now:%d.%m.%Y %H:%M} UTC по условиям сделки на платформе. "
                   "Перед подписанием проверьте текст с юристами сторон.")

    def clause(self, number: str, title: str, paragraphs):
        self.section(f"{number}. {title}", keep=24)
        self.set_font("Manrope", "", 9)
        self.set_text_color(*INK)
        for i, text in enumerate(paragraphs, 1):
            self.multi_cell(0, 5, _plain(f"{number}.{i}. {text}"), new_x="LMARGIN", new_y="NEXT")
            self.ln(1)


def contract_draft_pdf(application, offer, fx) -> bytes:
    """Проект договора займа: стороны, сумма, ставка, комиссия, срок и график подставлены из сделки."""
    cur = application.currency_code
    cost = offer_cost(offer, application)
    schedule = payment_schedule(offer, application)
    borrower = application.borrower
    term = application.term_months
    amount = f"{format_money(offer.funding_amount)} {cur}"

    pdf = new_pdf(ContractPDF, f"Проект договора займа по сделке №{application.id}")
    pdf.set_font("Manrope", "B", 18)
    pdf.set_text_color(*INK)
    pdf.cell(0, 9, "Договор займа", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Manrope", "", 10)
    pdf.set_text_color(*BRAND)
    pdf.cell(0, 6, f"Проект по сделке №{application.id} · дата и место подписания: ____________",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Manrope", "", 9)
    pdf.set_text_color(*INK)
    parts = _lender_parts(offer)
    if len(parts) == 1:
        lenders_text = f"{_lender_line(parts[0][0])}, далее «Займодавец»"
    else:
        lenders_text = "; ".join(_lender_line(lender) for lender, _ in parts) + \
                       ", совместно далее «Займодавец» (синдикат кредиторов)"
    pdf.multi_cell(0, 5, _plain(
        f"{lenders_text}, и {borrower.display_name} (Россия, ИНН {borrower.registration_number or '—'}), "
        f"далее «Заёмщик», совместно «Стороны», заключили настоящий договор о нижеследующем."),
        new_x="LMARGIN", new_y="NEXT")

    pdf.clause("1", "Предмет договора", [
        f"Займодавец передаёт Заёмщику денежные средства в размере {amount} (далее «Сумма займа»), "
        f"а Заёмщик обязуется вернуть Сумму займа и уплатить проценты в порядке и сроки, установленные договором.",
        f"Целевое назначение займа: {application.purpose}.",
    ] + ([("Сумма займа предоставляется синдикатом в долях: " + "; ".join(
        f"{lender.display_name} — {format_money(amount)} {cur} ({round(100 * amount / offer.funding_amount)}%)"
        for lender, amount in parts) + ". Проценты и возврат распределяются пропорционально долям, "
        "ставка и комиссия указаны средневзвешенными.")] if len(parts) > 1 else []))
    pdf.clause("2", "Предоставление займа", [
        f"Сумма займа перечисляется в валюте {cur} на счёт Заёмщика или указанного им получателя в стране: "
        f"{config.COUNTRY_NAMES.get(application.target_jurisdiction, '—')}.",
        f"Способ передачи средств: {config.TRANSFER_METHODS.get(application.transfer_method, '—')}.",
        "Займ считается предоставленным с даты зачисления средств на счёт получателя.",
    ])
    pdf.clause("3", "Проценты и комиссии", [
        f"За пользование займом Заёмщик уплачивает проценты по ставке {format_percent(offer.annual_interest_rate)} "
        f"годовых, начисляемые на Сумму займа и уплачиваемые ежемесячно.",
        f"Комиссия за организацию составляет {format_percent(offer.total_commission)} от Суммы займа "
        f"({format_money(cost['commission'])} {cur}), в том числе комиссия Займодавца "
        f"{format_percent(offer.lender_commission)} и комиссия платформы {format_percent(Decimal(str(offer.total_commission)) - Decimal(str(offer.lender_commission)))}. "
        f"Комиссия удерживается при выдаче.",
        (f"Полная стоимость займа: {format_percent(round(cost['effective_rate'], 2))} годовых, переплата за весь срок "
         f"{format_money(cost['overpayment'])} {cur}." if cost["effective_rate"] is not None else
         "Полная стоимость займа рассчитывается после согласования срока."),
    ])
    pdf.clause("4", "Срок и возврат", [
        f"Срок займа: {term} мес. с даты предоставления." if term else "Срок займа: ____ мес. с даты предоставления.",
        "Сумма займа возвращается одним платежом в последний месяц срока вместе с процентами за этот месяц "
        "согласно графику платежей (Приложение 1).",
        "Досрочный возврат возможен с письменного уведомления Займодавца не позднее чем за 10 рабочих дней.",
    ])
    number = 5
    if offer.extra_terms:
        pdf.clause(str(number), "Дополнительные условия", [offer.extra_terms])
        number += 1
    pdf.clause(str(number), "Ответственность и споры", [
        "За просрочку платежа Заёмщик уплачивает неустойку в размере ____% от просроченной суммы за каждый день просрочки.",
        "Применимое право: ____________. Споры рассматриваются в ____________.",
    ])
    pdf.clause(str(number + 1), "Заключительные положения", [
        "Договор вступает в силу с даты подписания и действует до полного исполнения обязательств.",
        "Договор составлен в двух экземплярах, по одному для каждой из Сторон. "
        "Подписанный экземпляр загружается в комнату сделки на платформе.",
    ])

    pdf.section("Подписи сторон", keep=50)
    signers = [("Займодавец" if len(parts) == 1 else f"Займодавец {i}", lender.display_name)
               for i, (lender, _) in enumerate(parts, 1)] + [("Заёмщик", borrower.display_name)]
    for row_start in range(0, len(signers), 2):
        _signature_row(pdf, signers[row_start:row_start + 2])

    if schedule:
        pdf.ln(8)
        pdf.section("Приложение 1. График платежей", keep=70)
        schedule_table(pdf, schedule, cur)
        rub = to_rub(cost["overpayment"], cur, fx) if cost["overpayment"] is not None else None
        if rub is not None:
            pdf.ln(2)
            pdf.set_font("Manrope", "", 8)
            pdf.set_text_color(*MUTED)
            pdf.multi_cell(0, 4, _plain(f"Справочно: переплата около {format_money(round(rub))} ₽ по курсу ЦБ РФ "
                                        f"{format_money(fx['rates'][cur])} ₽ за 1 {cur} на {fx['date']:%d.%m.%Y}."),
                           new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
