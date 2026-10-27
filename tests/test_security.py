"""Безопасность: CSRF, сессия, роли, XSS-экранирование, заголовки."""
from datetime import datetime, timedelta, timezone

from jose import jwt

from conftest import Browser, login
from liquidity import config, models
from liquidity.database import SessionLocal
from liquidity.security import hash_password


def make_user(email, role="borrower", **extra):
    with SessionLocal() as db:
        if not db.query(models.User).filter(models.User.email == email).first():
            db.add(models.User(email=email, password_hash=hash_password("password123"), role=role, **extra))
            db.commit()


def test_csrf_required_for_post():
    make_user("csrf@test.ru")
    browser = Browser()
    # без токена и с чужим токеном — 403, вход не выполнен
    assert browser.post("/login", data={"email": "csrf@test.ru", "password": "password123"}, csrf=False).status_code == 403
    assert browser.post("/login", data={"email": "csrf@test.ru", "password": "password123",
                                        "csrf_token": "forged"}).status_code == 403
    assert "access_token" not in browser.cookies
    # с токеном из cookie — успешно
    assert browser.post("/login", data={"email": "csrf@test.ru", "password": "password123"}).status_code == 303


def test_csrf_token_rendered_in_forms():
    browser = Browser()
    page = browser.get("/register").text
    assert 'name="csrf_token"' in page and browser.cookies.get("csrf_token") in page


def test_session_cookie_flags_and_expiry():
    make_user("cookie@test.ru")
    browser = Browser()
    response = browser.post("/login", data={"email": "cookie@test.ru", "password": "password123"})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and f"max-age={config.SESSION_HOURS * 3600}" in cookie
    payload = jwt.decode(browser.cookies.get("access_token"), config.SECRET_KEY, algorithms=["HS256"])
    assert "exp" in payload


def test_expired_or_forged_session_redirects_to_login():
    make_user("expired@test.ru")
    with SessionLocal() as db:
        user_id = db.query(models.User).filter(models.User.email == "expired@test.ru").one().id
    expired = jwt.encode({"user_id": user_id, "role": "borrower", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                         config.SECRET_KEY, algorithm="HS256")
    forged = jwt.encode({"user_id": user_id, "role": "admin"}, "wrong-key", algorithm="HS256")
    for token in (expired, forged):
        browser = Browser()
        browser.cookies.set("access_token", token)
        response = browser.get("/dashboard")
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_role_model():
    make_user("role-borrower@test.ru", company_name="Заёмщик")
    make_user("role-creditor@test.ru", role="creditor", company_name="Кредитор")
    borrower, creditor = login("role-borrower@test.ru"), login("role-creditor@test.ru")
    assert borrower.get("/admin").status_code == 403
    assert borrower.get("/operator/offers").status_code == 403
    assert creditor.post("/accept_offer/1").status_code == 403
    assert borrower.post("/create_offer/1", data={"amount": "1", "rate": "1", "lender_comm": "1"}).status_code == 403
    assert borrower.post("/admin/create_user", data={"email": "x@x.ru", "password": "password123",
                                                    "role": "admin"}).status_code == 403
    # самостоятельно стать администратором нельзя
    anon = Browser()
    assert anon.post("/register", data={"email": "evil@x.ru", "password": "password123", "role": "admin",
                                        "company_name": "X", "registration_number": "1"}).status_code == 400


def test_html_is_escaped():
    make_user("xss@test.ru", company_name="<script>alert(1)</script>")
    page = login("xss@test.ru").get("/dashboard").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_security_headers_and_demo_disabled():
    response = Browser().get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert Browser().post("/demo/login/admin").status_code == 404  # демо-вход выключен без DEMO_MODE=1


def test_demo_mode_login_and_reset(monkeypatch):
    from liquidity import config as cfg
    monkeypatch.setattr(cfg, "DEMO_MODE", True)
    browser = Browser()
    assert browser.post("/demo/login/borrower").headers["location"] == "/dashboard"
    assert "ООО «Альфа Технологии»" in browser.get("/dashboard").text
    admin = Browser()
    admin.post("/demo/login/admin")
    with SessionLocal() as db:  # имитируем PostgreSQL: после сброса у демо-админа будет другой id
        db.add(models.User(email="filler@test.ru", password_hash="x", role="borrower"))
        db.commit()
    assert admin.post("/demo/reset").headers["location"] == "/admin?demo_reset=1"
    assert admin.get("/admin").status_code == 200  # сессия выдана заново, на вход не выкинуло
    assert Browser().post("/demo/login/borrower").status_code == 303
    assert login("role-borrower@test.ru").post("/demo/reset").status_code == 403  # сброс — только админ


def test_public_pages():
    browser = Browser()
    landing = browser.get("/").text
    assert "Зарегистрировать компанию" in landing and 'href="/privacy"' in landing and "og:title" in landing
    assert browser.get("/login").status_code == 200
    assert "Кредитор (зарубежье)" in browser.get("/register?role=creditor").text
    assert browser.get("/privacy").status_code == 200 and browser.get("/terms").status_code == 200
    missing = browser.get("/no-such-page", headers={"accept": "text/html"})
    assert missing.status_code == 404 and "Страница не найдена" in missing.text
    assert browser.get("/static/icons/phosphor.svg").status_code == 200
