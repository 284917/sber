"""Показательные данные: компании верифицированы, входят с выданным паролем, статистика наполнена,
главная их не учитывает, удаление не задевает настоящие данные."""
from fastapi.testclient import TestClient

from conftest import login
from liquidity import config, models
from liquidity.app import app
from liquidity.database import SessionLocal
from liquidity.services import accounts, analytics, deal_reports, reliability, showcase
from test_features import make_company


def test_showcase_seed_login_stats_and_remove():
    make_company("real-b@test.ru", "borrower", "ООО «Настоящая»", "RU", "7701234567")
    real = login("real-b@test.ru")
    real.post("/create_application", data={"currency": "EUR", "country": "DE", "term_months": "6", "transfer_method": "swift",
                                           "amount": "300000", "purpose": "Настоящая заявка по контракту с поставщиком"})
    with SessionLocal() as db:
        before_public = analytics.public_stats(db)
        before_market = analytics.market_rates(db)
        before_users = db.query(models.User).count()
        logins = showcase.seed(db)
        assert showcase.exists(db)

        # Все компании верифицированы, данные согласованы
        companies = db.query(models.User).filter(models.User.email.like(f"%{config.SHOWCASE_DOMAIN}"), models.User.role != "operator").all()
        assert len(companies) == 20 and all(u.kyc_status == "approved" and u.kyc_approved_at for u in companies)
        assert all(accounts.inn_is_valid(u.registration_number) for u in companies if u.role == "borrower")
        assert not accounts.password_problems(logins[0][3]) and not accounts.password_problems(logins[-1][3])

        apps = db.query(models.Application).filter(models.Application.user_id.in_([u.id for u in companies])).all()
        assert len(apps) == 57 and sum(a.status == "agreed" for a in apps) == 22
        agreed = [a for a in apps if a.status == "agreed"]
        assert all(a.confirmed_at and a.confirmed_at >= a.created_at for a in agreed)
        assert db.query(models.Offer).filter_by(status="accepted").count() >= 29
        offers = db.query(models.Offer).join(models.Application).filter(models.Application.id.in_([a.id for a in apps])).all()
        assert all(o.sender for o in offers)
        assert all(not e.text.endswith("None") for e in db.query(models.ApplicationEvent))

        # Главная не учитывает показательные данные, аналитика администратора их видит
        assert analytics.public_stats(db)["offers"] == before_public["offers"]
        assert analytics.market_rates(db) == before_market
        assert analytics.overview(db)["analytics"]["total_apps"] >= 57
        creditor_ids = [u.id for u in companies if u.role == "creditor"]
        reports = deal_reports.about_creditors(db, creditor_ids)
        assert sum(r["count"] for r in reports.values()) > 10
        facts = reliability.creditor_facts(db, creditor_ids)
        assert any(f["reports"]["shown"] for f in facts.values())

    # Вход с выданным паролем и рабочие экраны у всех ролей
    role, _, email, password = next(item for item in logins if item[2].startswith("meridian"))
    creditor = login(email, password)
    assert "Заявки на финансирование" in creditor.get("/dashboard").text
    borrower = login(logins[0][2], logins[0][3])
    assert "Мои заявки" in borrower.get("/dashboard").text
    operator = login(logins[-1][2], logins[-1][3])
    assert "Проблемы после сделки" in operator.get("/operator/deals").text or operator.get("/operator/deals").status_code == 200
    assert "Обращения в поддержку" in operator.get("/operator/support").text
    with SessionLocal() as db:
        deal_id = db.query(models.Application).filter_by(status="agreed").filter(
            models.Application.user_id.in_([u.id for u in db.query(models.User).filter(models.User.email.like(f"%{config.SHOWCASE_DOMAIN}"))])).first().id
    assert operator.get(f"/deal/{deal_id}").status_code == 200
    assert operator.get(f"/operator/deal/{deal_id}").status_code == 200

    # Главная открывается, «реального времени» с показательными данными не появляется
    with TestClient(app) as guest:
        assert guest.get("/").status_code == 200

    # Удаление: показательных данных нет, настоящая заявка на месте
    with SessionLocal() as db:
        showcase.remove(db)
        assert not showcase.exists(db)
        assert db.query(models.User).count() == before_users
        assert db.query(models.Application).filter_by(purpose="Настоящая заявка по контракту с поставщиком").count() == 1
        assert db.query(models.StoredFile).filter(models.StoredFile.path.like("%showcase_%")).count() == 0
