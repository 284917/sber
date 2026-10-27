"""Общая настройка тестов: отдельная SQLite-база и тестовый клиент с CSRF-токеном, как у браузера."""
import os
import sys
import tempfile

DB_FILE = os.path.join(tempfile.mkdtemp(), "test.db")
# TEST_DATABASE_URL позволяет оставить базу после прогона (например, чтобы посмотреть экраны)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{DB_FILE}")
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "admin-password"
os.environ["SECRET_KEY"] = "test-secret"
os.environ.pop("DEMO_MODE", None)
os.environ.pop("VERCEL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from liquidity.app import app  # noqa: E402
from liquidity.services import fx  # noqa: E402

# Тесты не ходят в сеть: курсы ЦБ фиксированные
TEST_RATES = (date(2026, 10, 27), {"EUR": Decimal("90"), "USD": Decimal("80"), "CNY": Decimal("12")})
fx.fetch_from_cbr = lambda: TEST_RATES

PDF = ("doc.pdf", b"%PDF-1.4 test", "application/pdf")


class Browser(TestClient):
    """Клиент, который ведёт себя как браузер: получает CSRF-cookie и кладёт токен в каждую форму."""

    def __init__(self):
        super().__init__(app, follow_redirects=False)
        self.get("/")

    def post(self, url, data=None, csrf=True, **kwargs):
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf_token", self.cookies.get("csrf_token"))
        return super().post(url, data=data, **kwargs)


def login(email, password="password123") -> Browser:
    browser = Browser()
    response = browser.post("/login", data={"email": email, "password": password})
    assert response.status_code == 303 and response.headers["location"] == "/dashboard", response.text
    return browser
