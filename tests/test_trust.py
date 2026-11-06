"""Надёжность из фактов, опыт в похожих сделках, отчёт после сделки, карточка заявки, сравнение и поддержка."""
from conftest import PDF, Browser, login
from liquidity import models
from liquidity.database import SessionLocal
from liquidity.services import deal_reports, reliability
from test_features import make_company

FORM = {"currency": "EUR", "country": "DE", "term_months": "6", "transfer_method": "swift"}
INNS = iter(["7704211201", "7701234567", "7702070139", "7707329152", "7714617793", "7736050003"])
REPORT_OK = {"funds": "received", "on_time": "yes", "terms_match": "yes", "new_terms": "no", "contact_ok": "yes"}


def deal(borrower_client, creditor_client, admin, purpose, amount="300000", rate="8"):
    """Заявка, предложение, принятие и подтверждение: согласованная сделка."""
    borrower_client.post("/create_application", data={**FORM, "amount": amount, "purpose": purpose})
    with SessionLocal() as db:
        app = db.query(models.Application).filter_by(purpose=purpose).one()
        app_id, status = app.id, app.status
    if status == "moderation":
        admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})
    creditor_client.post(f"/create_offer/{app_id}", data={"amount": amount, "rate": rate, "lender_comm": "1"})
    with SessionLocal() as db:
        offer_id = db.query(models.Offer).filter_by(application_id=app_id).order_by(models.Offer.id.desc()).first().id
    borrower_client.post(f"/accept_offer/{offer_id}")
    creditor_client.post(f"/deal/{app_id}/upload_contract", files={"file": PDF})
    admin.post(f"/operator/deal/{app_id}/confirm")
    return app_id


def test_report_visibility_and_reliability_facts():
    make_company("tr-b@test.ru", "borrower", "ООО «Отчёт»", "RU", next(INNS))
    make_company("tr-c@test.cy", "creditor", "Report Capital", "CY", "HE 71")
    make_company("tr-x@test.ae", "creditor", "Outside Capital", "AE", "AE 72")
    borrower, creditor, outsider = login("tr-b@test.ru"), login("tr-c@test.cy"), login("tr-x@test.ae")
    admin = login("admin@test.local", "admin-password")

    # До согласования отчёта нет, а чужой участник отчитаться не может
    first = deal(borrower, creditor, admin, "Закупка станков по контракту номер один")
    page = borrower.get(f"/deal/{first}").text
    assert "Отчёт после сделки" in page and "Деньги получены?" in page
    assert "Заёмщик платит по графику?" in creditor.get(f"/deal/{first}").text
    assert outsider.post(f"/deal/{first}/report", data=REPORT_OK).status_code == 403
    assert "error=report" in borrower.post(f"/deal/{first}/report", data={**REPORT_OK, "funds": "x"}).headers["location"]

    # Проблема в отчёте уходит оператору, его ответы и комментарий видит оператор, но не вторая сторона
    assert borrower.post(f"/deal/{first}/report", data={**REPORT_OK, "terms_match": "no", "comment": "Подняли комиссию"}
                         ).headers["location"].endswith("report=1#report")
    operator_page = admin.get(f"/operator/deal/{first}").text
    assert "Отчёты после сделки" in operator_page and "Подняли комиссию" in operator_page
    assert "Подняли комиссию" not in creditor.get(f"/deal/{first}").text
    assert "Проблемы после сделки" in admin.get("/operator/deals").text

    # Отчёт можно обновить, запись одна
    borrower.post(f"/deal/{first}/report", data=REPORT_OK)
    with SessionLocal() as db:
        assert db.query(models.DealReport).filter_by(application_id=first).count() == 1
        creditor_id = db.query(models.User).filter_by(email="tr-c@test.cy").one().id

    # Меньше трёх отчётов: цифр нет, только честная подпись
    profile = borrower.get(f"/creditors/{creditor_id}").text
    assert "Надёжность" in profile and "отчётов пока мало" in profile and "из 5" not in profile

    # От трёх отчётов появляется сводка
    for i in (2, 3):
        app_id = deal(borrower, creditor, admin, f"Закупка оборудования по контракту номер {i}")
        borrower.post(f"/deal/{app_id}/report", data=REPORT_OK if i == 2 else {**REPORT_OK, "on_time": "not_due", "funds": "partial"})
    with SessionLocal() as db:
        fact = reliability.creditor_facts(db, [creditor_id])[creditor_id]
    assert fact["deals"] == 3 and fact["ubo"] and fact["source_funds"] and fact["verified_on"]
    assert fact["terms_text"] == "3 из 3" and fact["funds_text"] == "2 из 3" and fact["on_time_text"] == "2 из 2"
    assert "3 из 3" in borrower.get(f"/creditors/{creditor_id}").text


def test_similar_deals_and_compare_on_offers_page():
    make_company("sm-b@test.ru", "borrower", "ООО «Похожие»", "RU", next(INNS))
    make_company("sm-c@test.cy", "creditor", "Route Capital", "CY", "HE 73")
    make_company("sm-d@test.ae", "creditor", "Fresh Capital", "AE", "AE 74")
    borrower, route, fresh = login("sm-b@test.ru"), login("sm-c@test.cy"), login("sm-d@test.ae")
    admin = login("admin@test.local", "admin-password")
    past = deal(borrower, route, admin, "Прошлая закупка по контракту с поставщиком")
    borrower.post(f"/deal/{past}/report", data=REPORT_OK)

    borrower.post("/create_application", data={**FORM, "amount": "500000", "purpose": "Новая закупка по контракту с поставщиком"})
    with SessionLocal() as db:
        app = db.query(models.Application).filter_by(purpose="Новая закупка по контракту с поставщиком").one()
        app_id = app.id
    route.post(f"/create_offer/{app_id}", data={"amount": "500000", "rate": "8", "lender_comm": "1"})
    fresh.post(f"/create_offer/{app_id}", data={"amount": "500000", "rate": "9", "lender_comm": "1"})

    page = borrower.get(f"/applications/{app_id}/offers").text
    assert "Такой же маршрут уже был" in page and "EUR → Германия" in page and "получение денег подтверждено: 1" in page
    assert "Похожих сделок у этого кредитора пока нет" in page and "Новый кредитор" in page
    # Компания из прошлой сделки не раскрывается
    assert "Прошлая закупка" not in page
    # Сравнение: выбор и таблица с обоими кредиторами
    assert 'class="compare-check"' in page and 'id="compare-table"' in page
    assert page.count('data-col="') > 20


def test_borrower_card_and_contract_access():
    make_company("cd-b@test.ru", "borrower", "ООО «Карточка»", "RU", next(INNS))
    make_company("cd-c@test.cy", "creditor", "Card Capital", "CY", "HE 75")
    borrower, creditor = login("cd-b@test.ru"), login("cd-c@test.cy")
    admin = login("admin@test.local", "admin-password")
    purpose = "Оплата поставки станков по контракту 15/26 с поставщиком из Германии"
    assert "error=contract_pdf" in borrower.post("/create_application", data={**FORM, "amount": "300000", "purpose": purpose},
                                                 files={"contract": ("a.txt", b"x", "text/plain")}).headers["location"]
    borrower.post("/create_application", data={**FORM, "amount": "300000", "purpose": purpose},
                  files={"contract": ("kontrakt.pdf", b"%PDF-1.4 test", "application/pdf")})
    with SessionLocal() as db:
        app = db.query(models.Application).filter_by(purpose=purpose).one()
        app_id, path = app.id, app.contract_path
    assert path
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})

    page = creditor.get("/dashboard").text
    assert "Контракт приложен" in page and "Полнота данных:" in page and "Верификация" in page
    # Файл виден заёмщику и сотрудникам; кредитор, пока сделки нет, видит только отметку
    assert borrower.get(f"/{path}").status_code == 200 and admin.get(f"/{path}").status_code == 200
    assert creditor.get(f"/{path}").status_code == 403

    with SessionLocal() as db:
        app = db.get(models.Application, app_id)
        card = reliability.borrower_cards(db, [app])[app_id]
    assert card["total"] == 6 and dict(card["checks"])["Контракт приложен"] and dict(card["checks"])["Назначение описано подробно"]
    assert not dict(card["checks"])["Статус в ЕГРЮЛ подтверждён"]


def test_support_for_users_guests_and_operator():
    make_company("sp-b@test.ru", "borrower", "ООО «Поддержка»", "RU", next(INNS))
    borrower = login("sp-b@test.ru")
    admin = login("admin@test.local", "admin-password")

    assert "Написать в поддержку" in borrower.get("/dashboard").text
    assert "error=text" in borrower.post("/support", data={"topic": "kyc", "text": "мало"}).headers["location"]
    assert "error=topic" in borrower.post("/support", data={"topic": "x", "text": "Достаточно длинный вопрос"}).headers["location"]
    assert borrower.post("/support", data={"topic": "kyc", "text": "Сколько ждать проверку документов?"}).headers["location"] == "/support?sent=1"
    assert "Ждёт ответа" in borrower.get("/support").text

    # Гость без входа: нужна почта, есть ограничение частоты
    guest = Browser()
    assert "Почта для ответа" in guest.get("/support").text
    assert "error=email" in guest.post("/support", data={"topic": "access", "text": "Не могу войти в аккаунт"}).headers["location"]
    assert guest.post("/support", data={"topic": "access", "text": "Не могу войти в аккаунт", "contact_email": "guest@test.ru"}
                      ).headers["location"] == "/support?sent=1"
    for _ in range(4):
        guest.post("/support", data={"topic": "access", "text": "Не могу войти в аккаунт", "contact_email": "guest@test.ru"})
    assert "error=limit" in guest.post("/support", data={"topic": "access", "text": "Не могу войти в аккаунт", "contact_email": "guest@test.ru"}
                                       ).headers["location"]

    # Оператор видит очередь и отвечает, пользователь получает ответ
    queue = admin.get("/operator/support").text
    assert "Сколько ждать проверку" in queue and "guest@test.ru" in queue
    assert admin.get("/support").headers["location"] == "/operator/support"
    assert borrower.get("/operator/support").status_code == 403
    with SessionLocal() as db:
        ticket_id = db.query(models.SupportTicket).filter_by(topic="kyc").order_by(models.SupportTicket.id.desc()).first().id
    assert "error=reply" in admin.post(f"/operator/support/{ticket_id}/answer", data={"reply": " "}).headers["location"]
    admin.post(f"/operator/support/{ticket_id}/answer", data={"reply": "Обычно до 48 часов"})
    assert "Обычно до 48 часов" in borrower.get("/support").text
    assert "error=closed" in admin.post(f"/operator/support/{ticket_id}/answer", data={"reply": "Ещё раз"}).headers["location"]


def test_summary_hides_small_samples():
    class R:
        def __init__(self, funds="received", on_time="yes", terms=True, new=False, contact=True):
            self.funds, self.on_time, self.terms_match, self.new_terms, self.contact_ok = funds, on_time, terms, new, contact
    assert deal_reports.summary([R(), R()])["shown"] is False
    full = deal_reports.summary([R(), R(on_time="not_due"), R(terms=False)])
    assert full["shown"] and full["terms"] == {"ok": 2, "total": 3} and full["on_time"] == {"ok": 2, "total": 2}
    assert deal_reports.is_problem(R(funds="none")) and not deal_reports.is_problem(R())
