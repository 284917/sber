"""Сквозной сценарий из задания: регистрация -> KYC -> заявка -> модерация -> оффер -> оператор ->
принятие/отклонение -> договор -> «Согласовано» -> архив и аналитика. Плюс проверки ролевой модели.

Запуск: pytest -q  (используется отдельная SQLite-база, PostgreSQL не нужен)
"""
import pytest

from conftest import PDF, Browser, login
from liquidity import models
from liquidity.database import SessionLocal
from liquidity.services import analytics

BORROWER_DOCS = ["charter", "egrul", "passport", "beneficiaries", "director_auth"]
CREDITOR_DOCS = ["cert_inc", "memorandum", "directors_reg", "ubo", "tax_residency", "source_funds"]


def register(c, **data):
    form = {"password": "password123", **data}
    return c.post("/register", data=form)


def pass_kyc(c, admin, user_email, doc_types):
    for t in doc_types:
        assert c.post("/kyc/upload", data={"doc_type": t}, files={"file": PDF}).status_code == 303
    assert c.post("/kyc/submit").status_code == 303
    db = SessionLocal()
    uid = db.query(models.User).filter(models.User.email == user_email).one().id
    db.close()
    assert admin.post(f"/admin/kyc/approve_user/{uid}").status_code == 303
    return uid


def get(model, **filters):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**filters).one()
    finally:
        db.close()


@pytest.fixture(scope="module")
def world():
    anon = Browser()
    # Регистрация юрлиц и ролевая модель
    assert register(anon, email="hacker@x.ru", role="admin", company_name="X", registration_number="1").status_code == 400
    assert register(anon, email="bad@alfa.ru", role="borrower", company_name="Альфа",
                    registration_number="123").status_code == 400  # ИНН не из 10 цифр
    assert register(anon, email="alfa@alfa.ru", role="borrower", company_name="ООО «Альфа»",
                    registration_number="7700000000").status_code == 303
    assert register(anon, email="beta@beta.cy", role="creditor", company_name="Beta Capital Ltd",
                    registration_number="HE123456", country_code="CY").status_code == 303
    assert register(anon, email="gamma@gamma.ae", role="creditor", company_name="Gamma Finance",
                    registration_number="AE-777", country_code="AE").status_code == 303
    assert get(models.User, email="alfa@alfa.ru").country_code == "RU"

    admin = login("admin@test.local", "admin-password")
    assert admin.post("/admin/create_user", data={"email": "op@test.local", "password": "password123",
                                                  "role": "operator"}).status_code == 303
    return {"admin": admin, "operator": login("op@test.local"), "alfa": login("alfa@alfa.ru"),
            "beta": login("beta@beta.cy"), "gamma": login("gamma@gamma.ae")}


def test_full_deal_flow(world):
    admin, operator, alfa, beta, gamma = (world[k] for k in ("admin", "operator", "alfa", "beta", "gamma"))

    # Без KYC заявку создать нельзя
    app_form = {"amount": "500000", "currency": "EUR", "country": "DE", "purpose": "Закупка оборудования",
                "term_months": "6", "transfer_method": "swift"}
    assert alfa.post("/create_application", data=app_form).headers["location"] == "/dashboard?error=kyc"

    # KYC: отказ с причиной, повторная загрузка, одобрение
    pass_kyc(alfa, admin, "alfa@alfa.ru", BORROWER_DOCS)
    for t in CREDITOR_DOCS:
        beta.post("/kyc/upload", data={"doc_type": t}, files={"file": PDF})
    beta.post("/kyc/submit")
    beta_id = get(models.User, email="beta@beta.cy").id
    assert admin.post(f"/admin/kyc/reject_user/{beta_id}", data={"reason": ""}).headers["location"].endswith("error=reason#kyc")
    admin.post(f"/admin/kyc/reject_user/{beta_id}", data={"reason": "Нет сертификата резидентства"})
    assert get(models.User, id=beta_id).kyc_reject_reason == "Нет сертификата резидентства"
    assert "Нет сертификата резидентства" in beta.get("/kyc").text
    pass_kyc(beta, admin, "beta@beta.cy", CREDITOR_DOCS)
    assert get(models.User, id=beta_id).kyc_reject_reason is None
    pass_kyc(gamma, admin, "gamma@gamma.ae", CREDITOR_DOCS)

    # Заявка: проверка полей, затем модерация
    assert alfa.post("/create_application", data={**app_form, "term_months": "0"}).headers["location"] == "/dashboard?error=form"
    assert alfa.post("/create_application", data={**app_form, "transfer_method": "cash"}).headers["location"] == "/dashboard?error=form"
    assert alfa.post("/create_application", data=app_form).headers["location"] == "/dashboard?created=1"
    alfa.post("/create_application", data={**app_form, "amount": "1000", "purpose": "Тест"})
    application = get(models.Application, purpose="Закупка оборудования")
    spam = get(models.Application, purpose="Тест")
    assert application.status == "moderation" and application.term_months == 6
    assert "Закупка оборудования" not in beta.get("/dashboard").text  # до модерации кредиторы не видят

    assert admin.post(f"/admin/application/{spam.id}/moderate",
                      data={"decision": "reject", "comment": ""}).headers["location"].endswith("error=reason#moderation")
    admin.post(f"/admin/application/{spam.id}/moderate", data={"decision": "reject", "comment": "Неполное назначение"})
    admin.post(f"/admin/application/{application.id}/moderate", data={"decision": "approve"})
    assert get(models.Application, id=spam.id).status == "rejected"
    assert get(models.Application, id=application.id).status == "published"
    assert "Неполное назначение" in alfa.get("/dashboard").text

    # Фильтры кредитора по стране, сумме и сроку
    assert "Закупка оборудования" in beta.get("/dashboard?country=DE").text
    assert "Закупка оборудования" not in beta.get("/dashboard?country=TR").text
    assert "Закупка оборудования" not in beta.get("/dashboard?term_max=3").text
    assert "Закупка оборудования" not in beta.get("/dashboard?amount_min=600000").text
    assert "Закупка оборудования" in beta.get("/dashboard?amount_min=100000&amount_max=500000&term_max=12&currency=EUR").text

    # Офферы: проверки суммы, дополнительные условия, повтор
    offer_url = f"/create_offer/{application.id}"
    assert beta.post(offer_url, data={"amount": "600000", "rate": "8", "lender_comm": "1"}).headers["location"] == "/dashboard?error=offer"
    assert beta.post(offer_url, data={"amount": "500000", "rate": "8", "lender_comm": "1",
                                      "extra_terms": "Льготный период 1 месяц"}).headers["location"] == "/dashboard?offer_sent=1"
    assert beta.post(offer_url, data={"amount": "500000", "rate": "7", "lender_comm": "1"}).headers["location"] == "/dashboard?error=duplicate"
    gamma.post(offer_url, data={"amount": "450000", "rate": "7.5", "lender_comm": "1.5"})
    beta_offer = get(models.Offer, user_id=beta_id)
    gamma_offer = get(models.Offer, user_id=get(models.User, email="gamma@gamma.ae").id)
    assert beta_offer.extra_terms == "Льготный период 1 месяц" and float(beta_offer.total_commission) == pytest.approx(1.8)
    assert "Beta Capital" not in alfa.get(f"/applications/{application.id}/offers").text  # до оператора не видно

    # Оператор показывает офферы; заёмщик не может решать по чужой заявке и по непоказанным
    assert alfa.post(f"/accept_offer/{beta_offer.id}").status_code == 400
    assert operator.get("/operator/offers").status_code == 200
    operator.post(f"/operator/offer/{beta_offer.id}/show")
    operator.post(f"/operator/offer/{gamma_offer.id}/show")
    page = alfa.get(f"/applications/{application.id}/offers").text
    assert "Beta Capital Ltd" in page and "Gamma Finance" in page and "Льготный период" in page
    # Сравнение: Beta дешевле по полной стоимости (11,6% против 12,1%), экономия на сумму заявки за 6 мес.
    assert "Лучшее предложение" in page and "11,6%" in page and "12,1%" in page
    assert page.index("Beta Capital Ltd") < page.index("Gamma Finance")
    assert "1 250" in page

    # Отклонение одного предложения и принятие другого
    assert alfa.post(f"/reject_offer/{gamma_offer.id}").status_code == 303
    assert get(models.Offer, id=gamma_offer.id).status == "rejected"
    assert alfa.post(f"/accept_offer/{beta_offer.id}").headers["location"] == f"/deal/{application.id}"
    assert get(models.Application, id=application.id).status == "in_work"

    # Чужие участники не попадают в сделку
    assert gamma.get(f"/deal/{application.id}").status_code == 403
    assert gamma.post(f"/deal/{application.id}/send", data={"text": "spam"}).status_code == 403
    assert beta.post(f"/deal/{application.id}/send", data={"text": "Готовы подписать"}).status_code == 303

    # Все экраны сделки открываются у участников и сотрудников
    for c, url in ((alfa, f"/deal/{application.id}"), (beta, f"/deal/{application.id}"), (beta, "/dashboard"),
                   (operator, "/operator/deals"), (operator, f"/operator/deal/{application.id}"),
                   (operator, f"/deal/{application.id}"), (admin, f"/operator/deal/{application.id}")):
        assert c.get(url).status_code == 200, url
    assert "Станет активной, когда все проверки пройдены" in operator.get(f"/operator/deal/{application.id}").text

    # Подтверждение только после договора
    assert operator.post(f"/operator/deal/{application.id}/confirm").headers["location"].endswith("error=checks")
    beta.post(f"/deal/{application.id}/upload_contract", files={"file": ("dogovor.pdf", b"%PDF", "application/pdf")})
    assert operator.post(f"/operator/deal/{application.id}/confirm").headers["location"] == "/operator/deals"
    assert get(models.Application, id=application.id).status == "agreed"

    # История, архив и аналитика
    history = alfa.get(f"/applications/{application.id}/offers").text
    for text in ("отправлена на модерацию", "прошла модерацию", "Оператор показал", "Отклонено предложение",
                 "Принято предложение", "загрузил основной договор", "Сделка подтверждена"):
        assert text in history, text
    admin_page = admin.get("/admin").text
    assert "Архив согласованных сделок" in admin_page and "Beta Capital Ltd" in admin_page
    stats = analytics.build(*_all_apps_and_offers())
    assert {row["currency"]: row["count"] for row in stats["avg_by_currency"]} == {"EUR": 2}
    assert dict(stats["top_supply"]) == {"CY": 1, "AE": 1}
    assert dict(stats["top_demand"]) == {"DE": 2}


def test_roles_and_files(world):
    alfa, beta, gamma, operator = world["alfa"], world["beta"], world["gamma"], world["operator"]
    assert alfa.get("/admin").status_code == 403
    assert operator.post("/admin/kyc/approve_user/1").status_code == 403  # KYC решает только админ
    assert beta.post("/create_application", data={"amount": "1", "currency": "EUR", "country": "DE",
                                                  "purpose": "x", "term_months": "1",
                                                  "transfer_method": "swift"}).status_code == 403
    # Документы KYC видит владелец и сотрудники, но не другие компании
    doc = get(models.Document, user_id=get(models.User, email="alfa@alfa.ru").id, doc_type="egrul")
    assert alfa.get("/" + doc.file_path).status_code == 200
    assert operator.get("/" + doc.file_path).status_code == 200
    assert gamma.get("/" + doc.file_path).status_code == 403
    assert Browser().get("/" + doc.file_path).headers["location"] == "/"  # без входа — на страницу входа


def _all_apps_and_offers():
    db = SessionLocal()
    apps = db.query(models.Application).all()
    offers = db.query(models.Offer).all()
    for o in offers:
        _ = o.sender  # подгрузить до закрытия сессии
    return apps, offers

