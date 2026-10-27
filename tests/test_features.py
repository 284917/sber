"""Отзыв заявки и предложения, график платежей, курсы ЦБ, PDF-сводка, графики аналитики, подтверждения."""
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

from conftest import PDF, Browser, login
from liquidity import config, models
from liquidity.database import SessionLocal
from liquidity.security import hash_password
from liquidity.services import analytics, demo, fx
from liquidity.services.costs import offer_cost, payment_schedule

CBR_XML = ('<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="27.10.2026" name="Foreign Currency Market">'
           '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Dollar</Name>'
           '<Value>80,4909</Value></Valute><Valute ID="R01375"><NumCode>156</NumCode><CharCode>CNY</CharCode>'
           '<Nominal>10</Nominal><Name>Yuan</Name><Value>120,6800</Value></Valute>'
           '<Valute ID="R01820"><NumCode>392</NumCode><CharCode>JPY</CharCode><Nominal>100</Nominal><Name>Yen</Name>'
           '<Value>52,1</Value></Valute></ValCurs>').encode("cp1251")


def test_payment_schedule_matches_overpayment():
    app = NS(funding_amount=500000, term_months=6)
    offer = NS(funding_amount=500000, annual_interest_rate=8, total_commission=1.8)
    schedule = payment_schedule(offer, app)
    assert len(schedule["rows"]) == 7  # выдача + 6 месяцев
    assert schedule["rows"][1]["interest"] == pytest.approx(500000 * 0.08 / 12)
    assert schedule["rows"][-1]["principal"] == 500000 and schedule["rows"][-1]["balance"] == 0
    t = schedule["totals"]
    # проценты + комиссия по графику = переплата из расчёта полной стоимости
    assert t["interest"] + t["commission"] == pytest.approx(offer_cost(offer, app)["overpayment"])
    assert payment_schedule(offer, NS(funding_amount=1, term_months=None)) is None


def test_cbr_parsing_and_fallback(monkeypatch):
    rate_date, rates = fx.parse_cbr_xml(CBR_XML)
    assert rate_date == date(2026, 10, 27)
    assert float(rates["USD"]) == pytest.approx(80.4909) and float(rates["CNY"]) == pytest.approx(12.068)  # номинал 10 учтён
    assert "JPY" not in rates
    assert fx.to_rub(1000, "USD", {"date": rate_date, "rates": rates}) == pytest.approx(80490.9)
    assert fx.to_rub(1000, "USD", None) is None

    with SessionLocal() as db:
        fx.reset_cache()
        fresh = fx.current_rates(db)  # подменённый ЦБ: курс сохраняется в БД
        assert fresh["rates"]["EUR"] == 90

        def offline():
            raise OSError("ЦБ недоступен")
        monkeypatch.setattr(fx, "fetch_from_cbr", offline)
        fx.reset_cache()
        cached = fx.current_rates(db)  # ЦБ недоступен: берём последний сохранённый курс
        assert cached["date"] == fresh["date"] and cached["rates"]["EUR"] == 90
    fx.reset_cache()


def test_funnel_and_weekly():
    apps = [NS(id=i, status=s, created_at=datetime(2026, 10, d)) for i, (s, d) in enumerate(
        [("moderation", 27), ("published", 26), ("in_work", 20), ("agreed", 13), ("rejected", 13), ("withdrawn", 5)])]
    offers = [NS(application_id=1, operator_decision_at=datetime.now(), status="shown"),
              NS(application_id=2, operator_decision_at=datetime.now(), status="accepted"),
              NS(application_id=3, operator_decision_at=datetime.now(), status="accepted"),
              NS(application_id=5, operator_decision_at=None, status="pending")]
    stages = {s["label"]: s["count"] for s in analytics.funnel(apps, offers, published_ids={1, 2, 3, 5})}
    assert stages == {"Подано заявок": 6, "Прошли модерацию": 4, "Получили предложения": 3,
                      "Дошли до сделки": 2, "Согласованы": 1}
    weeks = analytics.weekly(apps, weeks=4, today=date(2026, 10, 27))  # вторник; неделя с 26.10
    assert [w["start"] for w in weeks] == [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)]
    assert [w["count"] for w in weeks] == [1, 2, 1, 2]


def make_company(email, role, name, country, regnum):
    with SessionLocal() as db:
        user = models.User(email=email, password_hash=hash_password("password123"), role=role, company_name=name,
                           country_code=country, registration_number=regnum)
        db.add(user)
        db.flush()
        for doc_type, _ in config.KYC_DOCS[role]:
            db.add(models.Document(user_id=user.id, doc_type=doc_type, file_path=f"uploads/t_{email}_{doc_type}",
                                   status="approved"))
        db.commit()


def test_withdraw_confirm_and_pdf():
    make_company("w-borrower@test.ru", "borrower", "ООО «Отзыв»", "RU", "7712345671")
    make_company("w-creditor@test.cy", "creditor", "Withdraw Capital", "CY", "HE 1")
    make_company("w-creditor2@test.ae", "creditor", "Second Capital", "AE", "AE 2")
    borrower, creditor, creditor2 = login("w-borrower@test.ru"), login("w-creditor@test.cy"), login("w-creditor2@test.ae")
    admin, = (login("admin@test.local", "admin-password"),)
    form = {"amount": "300000", "currency": "USD", "country": "TR", "purpose": "Отзыв", "term_months": "3",
            "transfer_method": "agent"}

    # Заявку на модерации можно отозвать
    borrower.post("/create_application", data=form)
    with SessionLocal() as db:
        first = db.query(models.Application).filter_by(purpose="Отзыв").one().id
    assert borrower.post(f"/applications/{first}/withdraw").headers["location"].endswith("withdrawn=1")
    with SessionLocal() as db:
        assert db.get(models.Application, first).status == "withdrawn"
    assert borrower.post(f"/applications/{first}/withdraw").status_code == 400  # повторно нельзя
    assert creditor.post(f"/applications/{first}/withdraw").status_code == 403  # чужим нельзя

    # Предложение можно отозвать до решения заёмщика, потом отправить новое
    borrower.post("/create_application", data={**form, "purpose": "Сделка для PDF"})
    with SessionLocal() as db:
        app_id = db.query(models.Application).filter_by(purpose="Сделка для PDF").one().id
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})
    creditor.post(f"/create_offer/{app_id}", data={"amount": "300000", "rate": "9", "lender_comm": "1"})
    with SessionLocal() as db:
        offer_id = db.query(models.Offer).filter_by(application_id=app_id).one().id
    assert creditor2.post(f"/offers/{offer_id}/withdraw").status_code == 404  # чужое предложение
    assert creditor.post(f"/offers/{offer_id}/withdraw").headers["location"].endswith("#my-offers")
    creditor.post(f"/create_offer/{app_id}", data={"amount": "300000", "rate": "9", "lender_comm": "1"})
    creditor2.post(f"/create_offer/{app_id}", data={"amount": "250000", "rate": "8", "lender_comm": "2"})
    operator = admin
    with SessionLocal() as db:
        live = [o.id for o in db.query(models.Offer).filter_by(application_id=app_id, status="pending")]
    for oid in live:
        operator.post(f"/operator/offer/{oid}/show")

    # Карточка заявки: подтверждения, график платежей, рубли по курсу ЦБ
    page = borrower.get(f"/applications/{app_id}/offers").text
    assert 'data-confirm="Принять предложение Withdraw Capital?"' in page
    assert "Остальное предложение будет отклонено" in page
    assert "График платежей" in page and "Отозвать заявку" in page
    assert "по курсу ЦБ РФ на 27.10.2026" in page and "₽" in page

    # Принять -> договор -> подтвердить -> PDF
    accepted = live[0]
    borrower.post(f"/accept_offer/{accepted}")
    assert borrower.get(f"/deal/{app_id}/summary.pdf").status_code == 404  # до согласования сводки нет
    creditor.post(f"/deal/{app_id}/upload_contract", files={"file": PDF})
    assert "data-confirm=\"Подтвердить сделку" in operator.get(f"/operator/deal/{app_id}").text
    operator.post(f"/operator/deal/{app_id}/confirm")
    pdf = borrower.get(f"/deal/{app_id}/summary.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and len(pdf.content) > 5000
    assert creditor.get(f"/deal/{app_id}/summary.pdf").status_code == 200
    assert creditor2.get(f"/deal/{app_id}/summary.pdf").status_code == 403  # отклонённому кредитору нельзя
    assert "Скачать сводку PDF" in borrower.get(f"/deal/{app_id}").text

    # Админка: воронка и динамика
    admin_page = admin.get("/admin").text
    assert "Воронка заявок" in admin_page and "Новые заявки по неделям" in admin_page


def test_demo_pdf_renders():
    with SessionLocal() as db:
        demo.reset(db)
        agreed = db.query(models.Application).filter(models.Application.status == "agreed",
                                                     models.Application.user_id.in_(demo.demo_user_ids(db))).first()
        offer = db.query(models.Offer).filter_by(application_id=agreed.id, status="accepted").one()
        from liquidity.services.documents import deal_summary_pdf
        from liquidity.services.events import get_events
        pdf = deal_summary_pdf(agreed, offer, get_events(db, agreed.id), db.get(models.User, agreed.confirmed_by),
                               {"date": date(2026, 10, 27), "rates": {"USD": 80}})
        assert pdf.startswith(b"%PDF")
        demo.clear(db)
