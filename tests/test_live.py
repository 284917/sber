"""Объяснение выбора, обновление предложений без перезагрузки, условия платформы на главной."""
from types import SimpleNamespace

from conftest import login
from liquidity import config, models
from liquidity.database import SessionLocal
from liquidity.services.costs import compare_offers, explain_choice
from test_features import make_company

FORM = {"currency": "EUR", "country": "DE", "term_months": "6", "transfer_method": "swift"}


def offer(oid, rate, commission, amount=500000, status="shown"):
    return SimpleNamespace(id=oid, annual_interest_rate=rate, total_commission=commission, funding_amount=amount, status=status)


def choice_for(*offers):
    application = SimpleNamespace(funding_amount=500000, term_months=6)
    rows, best_id, _ = compare_offers(application, list(offers))
    return explain_choice(rows, best_id)


def test_explain_choice_finds_low_rate_trap():
    # Низкая ставка при высокой комиссии — самое дорогое предложение
    choice = choice_for(offer(1, 8, 4), offer(2, 7.6, 4.4, 450000), offer(3, 7.2, 5.2))
    assert choice["best"]["offer"].id == 1 and choice["lowest"]["offer"].id == 3
    assert choice["lowest_is_worst"] and not choice["same"]
    assert round(choice["gap"], 2) == 1.6
    assert choice["lowest"]["cost"]["commission_annual"] == 10.4

    # Низкая ставка, но не самая дорогая
    choice = choice_for(offer(1, 8, 4), offer(2, 7.6, 4.4), offer(3, 9, 5))
    assert choice["lowest"]["offer"].id == 2 and not choice["lowest_is_worst"]

    # Лучшее сразу и по ставке, и по полной стоимости
    assert choice_for(offer(1, 7, 3), offer(2, 8, 4))["same"]

    # Сравнивать не с чем: объяснения нет, закрытые предложения не считаются
    assert choice_for(offer(1, 8, 4)) is None
    assert choice_for(offer(1, 8, 4), offer(2, 7, 9, status="rejected")) is None


def test_offers_version_changes_only_with_offers_and_is_private():
    make_company("live-b@test.ru", "borrower", "ООО «Живая»", "RU", "7704211201")
    make_company("live-b2@test.ru", "borrower", "ООО «Чужая»", "RU", "7701234567")
    make_company("live-c@test.cy", "creditor", "Live Capital", "CY", "HE 95")
    borrower, stranger, creditor = login("live-b@test.ru"), login("live-b2@test.ru"), login("live-c@test.cy")
    admin = login("admin@test.local", "admin-password")
    borrower.post("/create_application", data={**FORM, "amount": "400000", "purpose": "Заявка для живого обновления"})
    with SessionLocal() as db:
        app_id = db.query(models.Application).filter_by(purpose="Заявка для живого обновления").one().id
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})

    url = f"/applications/{app_id}/offers/version"
    first = borrower.get(url).json()
    assert first["status"] == "published" and borrower.get(url).json() == first
    assert f'data-version="{first["version"]}"' in borrower.get(f"/applications/{app_id}/offers").text

    # Ответ не раскрывает ничего, кроме отпечатка и статуса; чужую заявку не видно
    assert set(first) == {"version", "status"}
    assert stranger.get(url).status_code == 404
    assert creditor.get(url).status_code == 403

    creditor.post(f"/create_offer/{app_id}", data={"amount": "400000", "rate": "8", "lender_comm": "1"})
    second = borrower.get(url).json()
    assert second["version"] != first["version"]
    page = borrower.get(f"/applications/{app_id}/offers").text
    assert f'data-version="{second["version"]}"' in page and "Live Capital" in page
    assert "Комиссия в пересчёте на год" in page and 'data-live="offers"' in page


def test_landing_shows_terms_until_there_is_enough_data(monkeypatch):
    from fastapi.testclient import TestClient
    from liquidity.app import app

    guest = TestClient(app)
    page = guest.get("/").text
    assert "на проверку компании" in page and "заявок опубликовано" not in page
    assert 'id="scene"' in page and "showcase-bar\"><i>" not in page

    monkeypatch.setattr(config, "LIVE_STATS_MIN_OFFERS", 0)
    page = guest.get("/").text
    assert "заявок опубликовано" in page and "на проверку компании" not in page


def test_operator_page_keeps_reason_fields_folded():
    admin = login("admin@test.local", "admin-password")
    page = admin.get("/operator/moderation").text
    assert 'data-tabs="hash"' in page and 'id="complaints"' in page and 'id="published"' in page
