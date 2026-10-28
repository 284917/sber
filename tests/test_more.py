"""Частичное финансирование и синдикат, встречная ставка, сроки действия, журнал сотрудников, отпечаток договора,
валютный риск, автозаполнение по ИНН, графики, живые цифры, английская версия и PWA."""
from datetime import date, timedelta

from conftest import PDF, Browser, login
from liquidity import config, i18n, models
from liquidity.database import SessionLocal
from liquidity.services import analytics, company_lookup, expiry, fx
from test_features import make_company

FORM = {"currency": "EUR", "country": "DE", "term_months": "12", "transfer_method": "swift"}


def app_id_by(purpose):
    with SessionLocal() as db:
        return db.query(models.Application).filter_by(purpose=purpose).one().id


def offer_of(app_id, email):
    with SessionLocal() as db:
        uid = db.query(models.User).filter_by(email=email).one().id
        return db.query(models.Offer).filter_by(application_id=app_id, user_id=uid).one().id


def publish(borrower, admin, purpose, amount):
    borrower.post("/create_application", data={**FORM, "amount": str(amount), "purpose": purpose})
    app_id = app_id_by(purpose)
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})
    return app_id


def test_partial_financing_syndicate_and_counter_offer():
    make_company("syn-b@test.ru", "borrower", "ООО «Синдикат»", "RU", "7707049388")
    for email, name, country, reg in (("syn-c1@test.cy", "First Lender", "CY", "HE 31"),
                                      ("syn-c2@test.ae", "Second Lender", "AE", "AE 32"),
                                      ("syn-c3@test.cn", "Third Lender", "CN", "CN 33")):
        make_company(email, "creditor", name, country, reg)
    borrower = login("syn-b@test.ru")
    c1, c2, c3 = login("syn-c1@test.cy"), login("syn-c2@test.ae"), login("syn-c3@test.cn")
    admin = login("admin@test.local", "admin-password")
    app_id = publish(borrower, admin, "Синдицированный займ", 1000000)
    with SessionLocal() as db:
        assert db.get(models.Application, app_id).valid_until == date.today() + timedelta(days=config.APP_VALID_DAYS)

    c1.post(f"/create_offer/{app_id}", data={"amount": "600000", "rate": "10", "lender_comm": "1", "valid_days": "14"})
    c2.post(f"/create_offer/{app_id}", data={"amount": "400000", "rate": "8", "lender_comm": "1"})
    c3.post(f"/create_offer/{app_id}", data={"amount": "900000", "rate": "7", "lender_comm": "1"})
    o1, o2, o3 = (offer_of(app_id, e) for e in ("syn-c1@test.cy", "syn-c2@test.ae", "syn-c3@test.cn"))
    with SessionLocal() as db:
        assert db.get(models.Offer, o1).valid_until == date.today() + timedelta(days=14)
    for oid in (o1, o2, o3):
        admin.post(f"/operator/offer/{oid}/show")

    # Встречная ставка: заёмщик просит 9% вместо 10%, кредитор соглашается
    assert borrower.post(f"/offers/{o1}/counter", data={"rate": "11"}).headers["location"].endswith("error=counter_rate")
    assert borrower.post(f"/offers/{o1}/counter", data={"rate": "9", "comment": "Есть встречное от другого банка"}
                         ).headers["location"].endswith("countered=1")
    assert borrower.post(f"/accept_offer/{o1}").headers["location"].endswith("error=counter_pending")
    assert "Заёмщик предлагает 9%" in c1.get("/dashboard").text
    assert c2.post(f"/offers/{o1}/counter/agree").status_code == 404  # чужой оффер
    c1.post(f"/offers/{o1}/counter/agree")
    with SessionLocal() as db:
        offer = db.get(models.Offer, o1)
        assert float(offer.annual_interest_rate) == 9 and offer.counter_status == "accepted"
    assert borrower.post(f"/offers/{o1}/counter", data={"rate": "8"}).headers["location"].endswith("error=counter_used")

    # Частичное принятие: 600 000 из 1 000 000 — заявка остаётся открытой, 900 000 уже больше остатка
    assert borrower.post(f"/accept_offer/{o1}").headers["location"].endswith("partial=1")
    with SessionLocal() as db:
        assert db.get(models.Application, app_id).status == "published"
    page = borrower.get(f"/applications/{app_id}/offers").text
    assert "Собрано по заявке" in page and "Начать сделку с 60%" in page and "Больше остатка" in page
    assert borrower.post(f"/accept_offer/{o3}").headers["location"].endswith("error=exceeds")
    c4_form = {"amount": "500000", "rate": "7", "lender_comm": "1"}
    make_company("syn-c4@test.kz", "creditor", "Fourth Lender", "KZ", "KZ 34")
    assert login("syn-c4@test.kz").post(f"/create_offer/{app_id}", data=c4_form).headers["location"].endswith("error=offer")

    # Второе предложение закрывает остаток — сделка начинается автоматически, лишний оффер отклоняется
    assert borrower.post(f"/accept_offer/{o2}").headers["location"] == f"/deal/{app_id}"
    with SessionLocal() as db:
        assert db.get(models.Application, app_id).status == "in_work"
        assert db.get(models.Offer, o3).status == "rejected"
    for client in (borrower, c1, c2):
        assert client.get(f"/deal/{app_id}").status_code == 200
    assert c3.get(f"/deal/{app_id}").status_code == 403
    deal = borrower.get(f"/deal/{app_id}").text
    assert "Синдикат из 2 кредиторов" in deal and "First Lender" in deal and "Second Lender" in deal
    assert "Валютный риск" in deal
    # Синдикат: сумма складывается, ставка средневзвешенная (9% × 0,6 + 8% × 0,4 = 8,6%)
    assert "1 000 000 EUR" in deal.replace(" ", " ") and "8,6%" in deal
    draft = c2.get(f"/deal/{app_id}/contract-draft.pdf")
    assert draft.status_code == 200 and draft.content.startswith(b"%PDF")

    # Договор с отпечатком, подтверждение, PDF-сводка синдиката
    c1.post(f"/deal/{app_id}/upload_contract", files={"file": ("dogovor.pdf", b"%PDF-1.4 syndicate", "application/pdf")})
    import hashlib
    assert hashlib.sha256(b"%PDF-1.4 syndicate").hexdigest()[:16] in borrower.get(f"/deal/{app_id}").text
    assert admin.post(f"/operator/deal/{app_id}/confirm").headers["location"] == "/operator/deals"
    assert borrower.get(f"/deal/{app_id}/summary.pdf").status_code == 200
    assert "Синдикат" in admin.get("/admin?tab=archive").text


def test_start_deal_with_partial_amount_and_withdraw_closes_accepted():
    make_company("part-b@test.ru", "borrower", "ООО «Часть»", "RU", "7736207543")
    make_company("part-c@test.cy", "creditor", "Part Lender", "CY", "HE 41")
    borrower, creditor = login("part-b@test.ru"), login("part-c@test.cy")
    admin = login("admin@test.local", "admin-password")

    app_id = publish(borrower, admin, "Старт с частью", 500000)
    assert borrower.post(f"/applications/{app_id}/start_deal").headers["location"].endswith("error=nothing")
    creditor.post(f"/create_offer/{app_id}", data={"amount": "200000", "rate": "9", "lender_comm": "1"})
    oid = offer_of(app_id, "part-c@test.cy")
    admin.post(f"/operator/offer/{oid}/show")
    borrower.post(f"/accept_offer/{oid}")
    assert borrower.post(f"/applications/{app_id}/start_deal").headers["location"] == f"/deal/{app_id}"
    with SessionLocal() as db:
        assert db.get(models.Application, app_id).status == "in_work"

    other = publish(borrower, admin, "Отзыв с частью", 500000)
    creditor.post(f"/create_offer/{other}", data={"amount": "100000", "rate": "9", "lender_comm": "1"})
    oid = offer_of(other, "part-c@test.cy")
    admin.post(f"/operator/offer/{oid}/show")
    borrower.post(f"/accept_offer/{oid}")
    borrower.post(f"/applications/{other}/withdraw")
    with SessionLocal() as db:
        assert db.get(models.Offer, oid).status == "rejected"
    assert creditor.get(f"/deal/{other}").status_code == 403


def test_expiry_closes_offers_and_applications():
    make_company("exp-b@test.ru", "borrower", "ООО «Срок»", "RU", "7702235133")
    make_company("exp-c@test.cy", "creditor", "Expiry Lender", "CY", "HE 51")
    borrower, creditor = login("exp-b@test.ru"), login("exp-c@test.cy")
    admin = login("admin@test.local", "admin-password")
    app_id = publish(borrower, admin, "Истекает", 300000)
    creditor.post(f"/create_offer/{app_id}", data={"amount": "300000", "rate": "9", "lender_comm": "1", "valid_days": "7"})
    oid = offer_of(app_id, "exp-c@test.cy")
    admin.post(f"/operator/offer/{oid}/show")
    assert creditor.post(f"/create_offer/{app_id}", data={"amount": "1", "rate": "9", "lender_comm": "1",
                                                          "valid_days": "7"}).headers["location"].endswith("error=duplicate")

    with SessionLocal() as db:
        assert expiry.run(db, today=date.today() + timedelta(days=8), force=True) == {"offers": 1, "applications": 0}
        assert db.get(models.Offer, oid).status == "expired"
        assert expiry.run(db, today=date.today() + timedelta(days=config.APP_VALID_DAYS + 1), force=True)["applications"] >= 1
        assert db.get(models.Application, app_id).status == "expired"
        assert any("истёк" in n.text for n in db.query(models.Notification).filter_by(user_id=db.get(models.Application, app_id).user_id))
    page = borrower.get(f"/applications/{app_id}/offers").text
    assert "Срок публикации заявки истёк" in page and "Срок истёк" in page


def test_audit_journal_onboarding_and_charts():
    admin = login("admin@test.local", "admin-password")
    admin.post("/admin/create_user", data={"email": "audit-admin@test.local", "password": "password123", "role": "admin"})
    journal = admin.get("/admin?tab=journal").text
    assert "Журнал действий" in journal and "Создал сотрудника audit-admin@test.local" in journal
    plain = login("audit-admin@test.local")
    assert "Создал сотрудника" not in plain.get("/admin?tab=journal").text  # обычному админу журнал не виден

    overview = admin.get("/admin").text
    assert "Спрос: страна × валюта" in overview and "Средняя ставка по неделям" in overview and "Сколько длится каждый этап" in overview
    with SessionLocal() as db:
        charts = analytics.charts(db)
        assert set(charts["heat"]) == {(c, cur) for c in config.TARGET_COUNTRIES for cur in config.CURRENCIES}
        assert len(charts["stages"]) == 4
        stats = analytics.public_stats(db)
        assert stats["published"] >= 1 and len(stats["demand"]) == 3

    with SessionLocal() as db:
        db.add(models.User(email="new-b@test.ru", password_hash=__import__("liquidity.security", fromlist=["x"]).hash_password("password123"),
                           role="borrower", company_name="ООО «Новичок»", country_code="RU", registration_number="7710030411"))
        db.commit()
    page = login("new-b@test.ru").get("/dashboard").text
    assert "Первые шаги на платформе" in page and "Выполнено 1 из 5" in page


def test_fx_stress_and_landing_live_blocks():
    rows = fx.stress(1000, 100, "EUR", {"date": date.today(), "rates": {"EUR": 90}})
    assert [r["shock"] for r in rows] == [0, 10, 20, 30]
    assert rows[0]["rub"] == 99000 and round(rows[2]["extra"]) == 19800
    assert fx.stress(1000, 100, "EUR", None) is None
    page = Browser().get("/").text
    assert 'id="flows"' in page and "data-count" in page and "Если рубль ослабнет" in page


def test_inn_lookup(monkeypatch):
    assert Browser().get("/api/company?inn=7707083893").json() == {"enabled": False}
    monkeypatch.setattr(config, "DADATA_API_KEY", "test-key")
    calls = []

    def fake_request(inn):
        calls.append(inn)
        return {"suggestions": [{"value": "ПАО СБЕРБАНК", "data": {
            "name": {"short_with_opf": "ПАО Сбербанк"}, "ogrn": "1027700132195",
            "address": {"value": "г Москва, ул Вавилова, д 19"}, "state": {"status": "ACTIVE"}}}]}
    monkeypatch.setattr(company_lookup, "_request", fake_request)
    company_lookup._cache.clear()
    data = Browser().get("/api/company?inn=7707083893").json()
    assert data["found"] and data["name"] == "ПАО Сбербанк" and data["active"] and data["status_name"] == "действующая"
    Browser().get("/api/company?inn=7707083893")
    assert calls == ["7707083893"]  # второй раз из кэша
    assert Browser().get("/api/company?inn=7707083894").status_code == 400  # неверная контрольная цифра
    assert 'id="egrul"' in Browser().get("/register").text


def test_english_version_for_guests_and_lenders():
    guest = Browser()
    assert guest.get("/lang/en?next=//evil.example").headers["location"] == "/"
    switch = guest.get("/lang/en?next=/security")
    assert switch.headers["location"] == "/security" and "lang=en" in switch.headers["set-cookie"]
    guest.cookies.set("lang", "en")
    allowed = {"Русская версия"}
    for url in ("/", "/?for=creditor", "/security", "/login", "/register", "/privacy", "/terms"):
        page = guest.get(url).text
        assert '<html lang="en">' in page, url
        assert set(i18n.untranslated(page)) <= allowed, (url, set(i18n.untranslated(page)) - allowed)
    landing = guest.get("/").text
    assert "How much will the financing cost" in landing and "'en-US'" in landing and "11.6%" in landing

    make_company("en-c@test.cy", "creditor", "English Lender", "CY", "HE 61")
    lender = login("en-c@test.cy")
    lender.cookies.set("lang", "en")
    dashboard = lender.get("/dashboard").text
    assert "Financing applications" in dashboard and "My offers" in dashboard
    # заёмщик всегда видит русский интерфейс, даже с cookie
    make_company("en-b@test.ru", "borrower", "ООО «Язык»", "RU", "7714698320")
    borrower = login("en-b@test.ru")
    borrower.cookies.set("lang", "en")
    assert "Мои заявки" in borrower.get("/dashboard").text
    # русская версия не затронута
    assert "Сколько будет стоить финансирование" in Browser().get("/").text


def test_pwa_files():
    client = Browser()
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/" and "caches.open" in sw.text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.json()["display"] == "standalone"
    for icon in manifest.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/offline").status_code == 200
    assert 'rel="manifest"' in client.get("/").text
