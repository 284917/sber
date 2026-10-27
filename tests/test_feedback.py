"""Правки по отзывам: KYC без перезагрузки, чат без перезагрузки, удаление договора, вкладки админки,
главный администратор, модерация оператором, снятие заявки, подсказки к терминам и переключатель на лендинге."""
from conftest import PDF, Browser, login
from liquidity import config, models
from liquidity.database import SessionLocal
from liquidity.security import hash_password
from liquidity.web import with_terms
from test_features import make_company

JSON = {"Accept": "application/json"}


def make_user(email, role, **extra):
    with SessionLocal() as db:
        user = models.User(email=email, password_hash=hash_password("password123"), role=role,
                           company_name=extra.pop("company_name", email), **extra)
        db.add(user)
        db.commit()
        return user.id


def test_kyc_upload_is_json_and_keeps_other_files():
    make_user("kyc-json@test.ru", "borrower", country_code="RU", registration_number="7707083893")
    browser = login("kyc-json@test.ru")
    types = [t for t, _ in config.KYC_DOCS["borrower"]]

    first = browser.post("/kyc/upload", data={"doc_type": types[0]}, files={"file": PDF}, headers=JSON)
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] and body["done"] == 1 and body["total"] == len(types) and body["filename"] == "doc.pdf"

    # Не PDF и неизвестный тип документа — понятная ошибка, без перезагрузки
    bad = browser.post("/kyc/upload", data={"doc_type": types[1]}, files={"file": ("a.pdf", b"not pdf", "application/pdf")},
                       headers=JSON)
    assert bad.status_code == 400 and not bad.json()["ok"] and bad.json()["error"]
    assert browser.post("/kyc/upload", data={"doc_type": "bogus"}, files={"file": PDF}, headers=JSON).status_code == 400

    # Отправить неполный пакет нельзя
    assert browser.post("/kyc/submit").headers["location"] == "/kyc?error=incomplete"

    # Замена файла не трогает остальные загруженные документы
    browser.post("/kyc/upload", data={"doc_type": types[1]}, files={"file": PDF}, headers=JSON)
    replaced = browser.post("/kyc/upload", data={"doc_type": types[0]},
                            files={"file": ("ochen-dlinnoe-nazvanie-dokumenta-bez-probelov-2026.pdf", b"%PDF-1.7", "application/pdf")},
                            headers=JSON).json()
    assert replaced["done"] == 2
    with SessionLocal() as db:
        uid = db.query(models.User).filter_by(email="kyc-json@test.ru").one().id
        docs = db.query(models.Document).filter_by(user_id=uid).all()
        assert len(docs) == 2 and {d.doc_type for d in docs} == set(types[:2])

    for t in types[2:]:
        browser.post("/kyc/upload", data={"doc_type": t}, files={"file": PDF}, headers=JSON)
    assert browser.post("/kyc/submit").headers["location"].startswith("/kyc?success")
    # После отправки менять файлы нельзя, пока админ не решит
    assert browser.post("/kyc/upload", data={"doc_type": types[0]}, files={"file": PDF}, headers=JSON).status_code == 400

    admin = login("admin@test.local", "admin-password")
    queue = admin.get("/admin?tab=kyc").text
    assert "kyc-json@test.ru" in queue and dict(config.KYC_DOCS["borrower"])[types[0]].split()[0] in queue


def test_superadmin_manages_staff_and_operator_moderates():
    admin = login("admin@test.local", "admin-password")
    assert "Сотрудники" in admin.get("/admin?tab=staff").text
    admin.post("/admin/create_user", data={"email": "second-admin@test.local", "password": "password123", "role": "admin"})
    admin.post("/admin/create_user", data={"email": "op2@test.local", "password": "password123", "role": "operator"})

    # Обычный администратор не видит вкладку и не управляет сотрудниками
    second = login("second-admin@test.local")
    assert 'href="/admin?tab=staff"' not in second.get("/admin").text
    assert "Добавить сотрудника" not in second.get("/admin?tab=staff").text
    assert second.post("/admin/create_user", data={"email": "x@test.local", "password": "password123",
                                                   "role": "admin"}).status_code == 403

    # Оператор модерирует и снимает заявки в своём кабинете
    make_company("mod-borrower@test.ru", "borrower", "ООО «Модерация»", "RU", "7736050003")
    make_company("mod-creditor@test.cy", "creditor", "Mod Capital", "CY", "HE 7")
    borrower, creditor, operator = login("mod-borrower@test.ru"), login("mod-creditor@test.cy"), login("op2@test.local")
    form = {"amount": "100000", "currency": "EUR", "country": "DE", "purpose": "Модерация оператором",
            "term_months": "4", "transfer_method": "swift"}
    borrower.post("/create_application", data=form)
    with SessionLocal() as db:
        app_id = db.query(models.Application).filter_by(purpose="Модерация оператором").one().id
    page = operator.get("/operator/moderation")
    assert page.status_code == 200 and "Модерация оператором" in page.text
    assert operator.post(f"/admin/application/{app_id}/moderate",
                         data={"decision": "approve"}).headers["location"] == "/operator/moderation?moderated=1"
    creditor.post(f"/create_offer/{app_id}", data={"amount": "100000", "rate": "9", "lender_comm": "1"})

    # Снять без причины нельзя; с причиной — заявка закрыта, предложения тоже
    assert operator.post(f"/admin/application/{app_id}/unpublish", data={"reason": " "}).headers["location"].endswith("error=reason")
    operator.post(f"/admin/application/{app_id}/unpublish", data={"reason": "Дубль заявки"})
    with SessionLocal() as db:
        application = db.get(models.Application, app_id)
        assert application.status == "rejected" and application.moderation_comment == "Дубль заявки"
        assert {o.status for o in db.query(models.Offer).filter_by(application_id=app_id)} == {"rejected"}
    assert "Дубль заявки" in borrower.get(f"/applications/{app_id}/offers").text
    assert operator.post(f"/admin/application/{app_id}/unpublish", data={"reason": "ещё раз"}).status_code == 404

    # Главный админ отключает сотрудника; себя отключить нельзя
    with SessionLocal() as db:
        op_id = db.query(models.User).filter_by(email="op2@test.local").one().id
        root_id = db.query(models.User).filter_by(email="admin@test.local").one().id
    assert admin.post(f"/admin/staff/{root_id}/remove").headers["location"].endswith("error=self")
    assert admin.post(f"/admin/staff/{op_id}/remove").headers["location"].endswith("removed=1")
    assert operator.get("/operator/moderation").status_code in (303, 401, 403)  # сессия больше не действует
    assert Browser().post("/login", data={"email": "op2@test.local", "password": "password123"}).status_code != 303


def test_chat_json_and_contract_delete():
    make_company("chat-borrower@test.ru", "borrower", "ООО «Чат»", "RU", "7702070139")
    make_company("chat-creditor@test.cy", "creditor", "Chat Capital", "CY", "HE 9")
    borrower, creditor = login("chat-borrower@test.ru"), login("chat-creditor@test.cy")
    admin = login("admin@test.local", "admin-password")
    borrower.post("/create_application", data={"amount": "50000", "currency": "USD", "country": "DE", "purpose": "Чат",
                                               "term_months": "2", "transfer_method": "swift"})
    with SessionLocal() as db:
        app_id = db.query(models.Application).filter_by(purpose="Чат").one().id
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})
    creditor.post(f"/create_offer/{app_id}", data={"amount": "50000", "rate": "7", "lender_comm": "1"})
    with SessionLocal() as db:
        offer_id = db.query(models.Offer).filter_by(application_id=app_id).one().id
    admin.post(f"/operator/offer/{offer_id}/show")
    borrower.post(f"/accept_offer/{offer_id}")

    sent = borrower.post(f"/deal/{app_id}/send", data={"text": "Добрый день"}, headers=JSON)
    assert sent.status_code == 200 and sent.json()["ok"] and "Добрый день" in sent.json()["html"]
    photo = creditor.post(f"/deal/{app_id}/send", data={"text": ""},
                          files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n0000", "image/png")}, headers=JSON).json()
    assert "msg-image" in photo["html"]
    polled = borrower.get(f"/deal/{app_id}/messages?after={sent.json()['id']}").json()
    assert "msg-image" in polled["html"] and polled["last_id"] == photo["id"]

    # Договор: второй нельзя, удалить и загрузить заново — можно
    assert creditor.post(f"/deal/{app_id}/upload_contract", files={"file": PDF}).status_code == 303
    assert creditor.post(f"/deal/{app_id}/upload_contract", files={"file": PDF}).headers["location"].endswith("error=contract_exists")
    assert borrower.post(f"/deal/{app_id}/contract/delete").status_code == 403  # удаляет только загрузивший или сотрудник
    assert creditor.post(f"/deal/{app_id}/contract/delete").status_code == 303
    assert "Загрузить договор" in creditor.get(f"/deal/{app_id}").text
    assert creditor.post(f"/deal/{app_id}/upload_contract", files={"file": PDF}).status_code == 303


def test_terms_tooltips_and_landing_switch():
    html = str(with_terms("Выписка из ЕГРЮЛ и SWIFT"))
    assert html.count('class="term"') == 2 and 'data-tip="' in html
    assert "&lt;b&gt;" in str(with_terms("<b>ИНН</b>"))  # обычная строка экранируется
    page = Browser().get("/?for=creditor").text
    assert 'data-audience="creditor"' in page and "Для кредиторов" in page
    assert 'data-audience="borrower"' in Browser().get("/").text
