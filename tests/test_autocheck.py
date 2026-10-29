"""Автоматическая проверка вместо ручной модерации: заявки и предложения публикуются сразу,
оператор видит только то, на что сработало правило, и жалобы кредиторов на опубликованные заявки."""
from conftest import login
from liquidity import models
from liquidity.database import SessionLocal
from liquidity.services import screening
from test_features import make_company

FORM = {"currency": "EUR", "country": "DE", "term_months": "6", "transfer_method": "swift"}


def app_by(purpose):
    with SessionLocal() as db:
        app = db.query(models.Application).filter_by(purpose=purpose).one()
        db.expunge(app)
        return app


def test_stop_words_match_word_starts():
    assert screening.stop_word("Пополнение оборотки для казино") == "казино"
    assert screening.stop_word("Закупка ОРУЖИЯ") == "оружи"
    assert screening.stop_word("Закупка вооружённых сил") is None  # не начало слова
    assert screening.stop_word("Закупка оборудования") is None


def test_applications_publish_right_away_except_flagged():
    make_company("auto-b@test.ru", "borrower", "ООО «Автопроверка»", "RU", "7704211201")
    borrower, admin = login("auto-b@test.ru"), login("admin@test.local", "admin-password")

    # Первая заявка компании — на ручную проверку, оператор публикует
    assert borrower.post("/create_application", data={**FORM, "amount": "300000", "purpose": "Первая заявка на станки"}
                         ).headers["location"] == "/dashboard?created=review"
    first = app_by("Первая заявка на станки")
    assert first.status == "moderation" and first.review_reason == "первая заявка компании"
    queue = admin.get("/operator/moderation").text
    assert "На ручной проверке" in queue and "первая заявка компании" in queue
    admin.post(f"/admin/application/{first.id}/moderate", data={"decision": "approve"})
    assert app_by("Первая заявка на станки").status == "published"

    # Следующая обычная заявка публикуется сразу, без оператора
    assert borrower.post("/create_application", data={**FORM, "amount": "200000", "purpose": "Вторая заявка на сырьё"}
                         ).headers["location"] == "/dashboard?created=1"
    second = app_by("Вторая заявка на сырьё")
    assert second.status == "published" and second.valid_until is not None and second.review_reason is None
    page = borrower.get(f"/applications/{second.id}/offers").text
    assert "прошла автоматическую проверку" in page

    # Крупная сумма — снова к оператору
    borrower.post("/create_application", data={**FORM, "amount": "1500000", "purpose": "Крупная заявка на краны"})
    big = app_by("Крупная заявка на краны")
    assert big.status == "moderation" and big.review_reason == "сумма выше 1 000 000 EUR"
    assert "Заявку проверяет оператор: сумма выше 1 000 000 EUR" in borrower.get(f"/applications/{big.id}/offers").text


def test_offer_far_from_market_goes_to_operator():
    make_company("rate-b@test.ru", "borrower", "ООО «Ставка»", "RU", "7701234567")
    make_company("rate-c@test.cy", "creditor", "Rate Capital", "CY", "HE 91")
    borrower, creditor = login("rate-b@test.ru"), login("rate-c@test.cy")
    admin = login("admin@test.local", "admin-password")
    borrower.post("/create_application", data={**FORM, "amount": "400000", "purpose": "Заявка для проверки ставки"})
    app_id = app_by("Заявка для проверки ставки").id
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})

    # Жёсткие правила: ставка выше максимума и стоп-слово в условиях
    assert creditor.post(f"/create_offer/{app_id}", data={"amount": "400000", "rate": "60", "lender_comm": "1"}
                         ).headers["location"] == "/dashboard?error=offer"
    assert creditor.post(f"/create_offer/{app_id}", data={"amount": "400000", "rate": "8", "lender_comm": "1",
                                                          "extra_terms": "Оплата через казино"}
                         ).headers["location"] == "/dashboard?error=stop_word"

    # Ставка далеко от рынка (ориентир EUR 8%) — предложение ждёт оператора, заёмщик его пока не видит
    assert creditor.post(f"/create_offer/{app_id}", data={"amount": "400000", "rate": "19", "lender_comm": "1"}
                         ).headers["location"] == "/dashboard?offer_sent=review"
    with SessionLocal() as db:
        offer = db.query(models.Offer).filter_by(application_id=app_id).one()
        assert offer.status == "pending" and "заметно выше рынка" in offer.review_reason
        offer_id = offer.id
    assert "Rate Capital" not in borrower.get(f"/applications/{app_id}/offers").text
    assert "заметно выше рынка" in admin.get("/operator/offers").text
    assert "Автопроверка: ставка 19%" in creditor.get("/dashboard").text
    admin.post(f"/operator/offer/{offer_id}/show")
    assert "Rate Capital" in borrower.get(f"/applications/{app_id}/offers").text


def test_creditor_complaint_and_operator_decision():
    make_company("cmp-b@test.ru", "borrower", "ООО «Жалоба»", "RU", "7702070139")
    make_company("cmp-c@test.cy", "creditor", "Complaint Capital", "CY", "HE 92")
    make_company("cmp-c2@test.ae", "creditor", "Second Complaint", "AE", "AE 93")
    borrower, creditor, creditor2 = login("cmp-b@test.ru"), login("cmp-c@test.cy"), login("cmp-c2@test.ae")
    admin = login("admin@test.local", "admin-password")
    for purpose in ("Первая заявка с жалобой", "Вторая заявка с жалобой"):
        borrower.post("/create_application", data={**FORM, "amount": "200000", "purpose": purpose})
        app = app_by(purpose)
        if app.status == "moderation":
            admin.post(f"/admin/application/{app.id}/moderate", data={"decision": "approve"})
    first, second = app_by("Первая заявка с жалобой").id, app_by("Вторая заявка с жалобой").id

    assert "Пожаловаться на заявку" in creditor.get("/dashboard").text
    assert creditor.post(f"/applications/{first}/complain", data={"reason": ""}).headers["location"].startswith(
        "/dashboard?error=complaint_reason")
    assert creditor.post(f"/applications/{first}/complain", data={"reason": "Странная цель займа"}
                         ).headers["location"] == "/dashboard?complained=1"
    assert creditor.post(f"/applications/{first}/complain", data={"reason": "Ещё раз"}
                         ).headers["location"].startswith("/dashboard?error=complaint_duplicate")
    assert "Вы пожаловались на заявку" in creditor.get("/dashboard").text
    creditor2.post(f"/applications/{second}/complain", data={"reason": "Данные не сходятся с ЕГРЮЛ"})

    queue = admin.get("/operator/moderation").text
    assert "Жалобы кредиторов" in queue and "Странная цель займа" in queue and "Данные не сходятся" in queue
    with SessionLocal() as db:
        complaints = {c.application_id: c.id for c in db.query(models.Complaint)}

    # Заявка в порядке: жалоба закрыта, кредитор получил ответ, заявка опубликована
    admin.post(f"/admin/complaint/{complaints[first]}/dismiss")
    # Жалоба подтвердилась: заявку снимают, жалоба принята
    admin.post(f"/admin/application/{second}/unpublish", data={"reason": "Данные не сходятся с ЕГРЮЛ", "back": "moderation"})
    with SessionLocal() as db:
        assert db.get(models.Complaint, complaints[first]).status == "dismissed"
        assert db.get(models.Complaint, complaints[second]).status == "accepted"
        assert db.get(models.Application, first).status == "published"
        assert db.get(models.Application, second).status == "rejected"
        texts = [n.text for n in db.query(models.Notification).join(models.User).filter(
            models.User.email.in_(["cmp-c@test.cy", "cmp-c2@test.ae"]))]
    assert any("оставил её опубликованной" in t for t in texts)
    assert any("снята с публикации по вашей жалобе" in t for t in texts)
    assert "Жалоб нет" in admin.get("/operator/moderation").text
