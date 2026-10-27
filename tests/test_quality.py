"""ИНН по контрольной сумме, защита от перебора пароля, уведомления, тёмная тема и превью для соцсетей."""
from datetime import datetime, timedelta

import pytest

from conftest import PDF, Browser, login
from liquidity import config, models
from liquidity.database import SessionLocal
from liquidity.security import hash_password
from liquidity.services import throttle
from liquidity.services.accounts import inn_is_valid


@pytest.mark.parametrize("inn, ok", [
    ("7707083893", True),   # ПАО Сбербанк
    ("7736050003", True),   # ПАО Газпром
    ("7707083894", False),  # опечатка в последней цифре
    ("7707038893", False),  # переставлены цифры
    ("770708389", False),   # 9 цифр
    ("77070838931", False),
    ("77070a3893", False),
])
def test_inn_checksum(inn, ok):
    assert inn_is_valid(inn) is ok


def test_register_shows_error_next_to_field():
    page = Browser().post("/register", data={"email": "inn@test.ru", "password": "password123", "role": "borrower",
                                             "company_name": "ООО «Опечатка»", "registration_number": "7707083894"})
    assert page.status_code == 400
    assert 'id="registration_number-error" >Контрольная цифра' in page.text or "контрольной цифры" in page.text
    assert 'aria-invalid="true"' in page.text and 'value="7707083894"' in page.text  # введённое не теряется
    ok = Browser().post("/register", data={"email": "inn-ok@test.ru", "password": "password123", "role": "borrower",
                                           "company_name": "ООО «Сбер»", "registration_number": "7707083893"})
    assert ok.status_code == 303


def test_login_lockout_after_five_failures():
    with SessionLocal() as db:
        db.add(models.User(email="brute@test.ru", password_hash=hash_password("password123"), role="borrower"))
        db.commit()
    browser = Browser()
    for attempt in range(1, 6):
        r = browser.post("/login", data={"email": "brute@test.ru", "password": f"wrong-{attempt}"})
        assert r.status_code == 400
    assert "Вход заблокирован на 15 минут" in r.text
    # даже верный пароль не пускает, пока действует блокировка
    blocked = browser.post("/login", data={"email": "Brute@Test.ru", "password": "password123"})
    assert blocked.status_code == 429 and "повторите через 15 мин" in blocked.text

    # через 15 минут блокировка снимается; успешный вход сбрасывает счётчик
    with SessionLocal() as db:
        row = db.get(models.LoginAttempt, "email:brute@test.ru")
        row.locked_until = datetime.utcnow() - timedelta(seconds=1)
        row.window_start = datetime.utcnow() - timedelta(minutes=16)
        db.commit()
    assert browser.post("/login", data={"email": "brute@test.ru", "password": "password123"}).status_code == 303
    with SessionLocal() as db:
        assert db.get(models.LoginAttempt, "email:brute@test.ru") is None


def test_ip_limit_and_window():
    now = datetime(2026, 10, 27, 12, 0)
    with SessionLocal() as db:
        for i in range(throttle.LIMITS["ip"]):
            throttle.register_failure(db, f"user{i}@spray.ru", "10.0.0.9", now=now)
        assert throttle.locked_for(db, "fresh@spray.ru", "10.0.0.9", now=now + timedelta(minutes=1))
        assert throttle.locked_for(db, "fresh@spray.ru", "10.0.0.10", now=now) is None  # другой IP не затронут
        assert throttle.locked_for(db, "fresh@spray.ru", "10.0.0.9", now=now + timedelta(minutes=16)) is None


def company(email, role, name, regnum, country):
    with SessionLocal() as db:
        user = models.User(email=email, password_hash=hash_password("password123"), role=role, company_name=name,
                           registration_number=regnum, country_code=country)
        db.add(user)
        db.flush()
        for doc_type, _ in config.KYC_DOCS[role]:
            db.add(models.Document(user_id=user.id, doc_type=doc_type, file_path=f"uploads/n_{email}_{doc_type}",
                                   status="pending" if email.startswith("kyc") else "approved"))
        db.commit()
        return user.id


def unread_texts(user_id):
    with SessionLocal() as db:
        return [n.text for n in db.query(models.Notification).filter_by(user_id=user_id, read_at=None)]


def test_notifications_flow():
    b_id = company("n-borrower@test.ru", "borrower", "ООО «Уведомления»", "7707083893", "RU")
    c_id = company("n-creditor@test.cy", "creditor", "Notify Capital", "HE 9", "CY")
    k_id = company("kyc-wait@test.ru", "borrower", "ООО «Проверка»", "7736050003", "RU")
    borrower, creditor = login("n-borrower@test.ru"), login("n-creditor@test.cy")
    admin = login("admin@test.local", "admin-password")

    # KYC одобрен -> уведомление компании
    admin.post(f"/admin/kyc/approve_user/{k_id}")
    assert any("Верификация пройдена" in t for t in unread_texts(k_id))

    # заявка опубликована -> заёмщику; предложение показано -> заёмщику и кредитору
    borrower.post("/create_application", data={"amount": "100000", "currency": "EUR", "country": "DE",
                                               "purpose": "Уведомления", "term_months": "6", "transfer_method": "swift"})
    with SessionLocal() as db:
        app_id = db.query(models.Application).filter_by(purpose="Уведомления").one().id
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})
    assert f"Заявка №{app_id} опубликована, кредиторы её видят" in unread_texts(b_id)
    creditor.post(f"/create_offer/{app_id}", data={"amount": "100000", "rate": "9", "lender_comm": "1"})
    with SessionLocal() as db:
        offer_id = db.query(models.Offer).filter_by(application_id=app_id).one().id
    admin.post(f"/operator/offer/{offer_id}/show")
    assert f"Новое предложение от Notify Capital по заявке №{app_id}" in unread_texts(b_id)

    # в кабинете видно счётчик и блок новых событий
    dashboard = borrower.get("/dashboard").text
    assert "Новые события" in dashboard and "Новое предложение от Notify Capital" in dashboard
    assert 'aria-label="Новых: 2"' in dashboard

    # принято -> кредитору; оператор написал -> обоим
    borrower.post(f"/accept_offer/{offer_id}")
    assert any("принял ваше предложение" in t for t in unread_texts(c_id))
    admin.post(f"/deal/{app_id}/send", data={"text": "Проверяю документы"})
    assert any("Оператор подключился к сделке" in t for t in unread_texts(b_id))
    assert any("Оператор подключился к сделке" in t for t in unread_texts(c_id))

    # переход по уведомлению отмечает его прочитанным; «прочитать все» обнуляет счётчик
    with SessionLocal() as db:
        first = db.query(models.Notification).filter_by(user_id=b_id, read_at=None).first()
        link, nid = first.link, first.id
    assert borrower.get(f"/notifications/{nid}").headers["location"] == link
    borrower.post("/notifications/read-all")
    assert unread_texts(b_id) == []
    assert "Новые события" not in borrower.get("/dashboard").text
    # чужое уведомление открыть нельзя: просто возврат к списку
    assert creditor.get(f"/notifications/{nid}").headers["location"] == "/notifications"


def test_theme_and_social_preview():
    page = Browser().get("/").text
    assert "toggleTheme()" in page and "localStorage.getItem('theme')" in page
    assert '<meta property="og:image" content="https://sber-delta.vercel.app/static/og.png">' in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    css = Browser().get("/static/css/app.css").text
    assert ':root[data-theme="dark"]' in css and "prefers-color-scheme: dark" in css
    og = Browser().get("/static/og.png")
    assert og.status_code == 200 and og.content[:8] == b"\x89PNG\r\n\x1a\n"
