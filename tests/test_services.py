"""Модульные тесты бизнес-логики (без HTTP): расчёт стоимости, этапы, аналитика, валидация."""
from types import SimpleNamespace as NS

import pytest

import conftest  # noqa: F401  настраивает окружение и БД
from liquidity import models
from liquidity.database import SessionLocal
from liquidity.services import Invalid, accounts, analytics, applications, demo
from liquidity.services.costs import compare_offers, offer_cost
from liquidity.web import format_money, format_percent

APP = NS(funding_amount=500000, term_months=6)


def offer(id, amount, rate, commission, status="shown"):
    return NS(id=id, funding_amount=amount, annual_interest_rate=rate, total_commission=commission, status=status)


def test_offer_cost():
    beta = offer_cost(offer(1, 500000, 8, 1.8), APP)
    assert beta["interest"] == pytest.approx(20000)        # 500 000 × 8% × 6/12
    assert beta["commission"] == pytest.approx(9000)       # 500 000 × 1,8%
    assert beta["overpayment"] == pytest.approx(29000)
    assert beta["effective_rate"] == pytest.approx(11.6)   # 8 + 1,8 × 12/6
    assert offer_cost(offer(2, 450000, 7.5, 2.3), APP)["coverage"] == 90
    no_term = offer_cost(offer(3, 1, 5, 1), NS(funding_amount=1, term_months=None))
    assert no_term["effective_rate"] is None and no_term["overpayment"] is None


def test_compare_offers_sorts_and_picks_best():
    offers = [offer(1, 450000, 7.5, 2.3), offer(2, 500000, 8, 1.8), offer(3, 500000, 6, 1.0, status="rejected")]
    rows, best_id, savings = compare_offers(APP, offers)
    assert [r["offer"].id for r in rows] == [3, 2, 1]  # по полной стоимости: 8%, 11,6%, 12,1%
    assert best_id == 2                                  # отклонённое не участвует в выборе лучшего
    # экономия на сумму заявки: (12,1% − 11,6%) × 500 000 × 6/12
    assert savings == pytest.approx(1250)
    assert compare_offers(APP, [offer(1, 1, 5, 1)])[1:] == (None, None)  # одно предложение — сравнивать не с чем


def test_formatting():
    assert format_money(612500.5) == "612 500,50"
    assert format_money(500000) == "500 000"
    assert format_money(None) == "—"
    assert format_percent(1.80) == "1,8%"
    assert format_percent(8) == "8%"


def test_stages():
    with SessionLocal() as db:
        states = lambda status: [s["state"] for s in applications.stages(  # noqa: E731
            db, NS(id=0, status=status, moderation_comment="причина", review_reason=None))]
        assert states("moderation") == ["current"] + ["todo"] * 5
        assert states("rejected") == ["failed"] + ["todo"] * 5
        assert states("published")[:3] == ["done", "done", "current"]
        assert states("in_work")[:4] == ["done", "done", "done", "current"]  # договора ещё нет
        assert states("agreed") == ["done"] * 6
        notes = applications.stages(db, NS(id=0, status="published", moderation_comment=None, review_reason=None), shown_offers=2)
        assert notes[2]["note"] == "Получено предложений: 2"


def test_analytics_by_currency_and_supply():
    apps = [NS(currency_code="EUR", funding_amount=100, target_jurisdiction="DE", status="agreed"),
            NS(currency_code="EUR", funding_amount=300, target_jurisdiction="DE", status="published"),
            NS(currency_code="CNY", funding_amount=1000, target_jurisdiction="CN", status="in_work")]
    offers = [NS(sender=NS(country_code="CY")), NS(sender=NS(country_code="CY")), NS(sender=NS(country_code="AE"))]
    stats = analytics.build(apps, offers)
    by_currency = {row["currency"]: row["avg"] for row in stats["avg_by_currency"]}
    assert by_currency == {"EUR": 200, "CNY": 1000}  # валюты не смешиваются
    assert stats["top_demand"][0] == ("DE", 2)
    assert dict(stats["top_supply"]) == {"CY": 2, "AE": 1}
    assert stats["deal_share"] == 67


def test_parse_number():
    assert applications.parse_number("100 000,5") == 100000.5
    assert applications.parse_number("") is None
    assert applications.parse_number("abc") is None


@pytest.mark.parametrize("data, code", [
    ({"role": "admin"}, "role"),
    ({"company_name": " "}, "company"),
    ({"password": "short"}, "password"),
    ({"registration_number": "123"}, "inn"),
    ({"role": "creditor", "country_code": "RU"}, "country"),
])
def test_register_validation(data, code):
    form = {"email": "v@x.ru", "password": "Str0ng!Pass1", "role": "borrower", "company_name": "ООО «Тест»",
            "registration_number": "7700000009", "country_code": "", **data}
    with SessionLocal() as db, pytest.raises(Invalid) as err:
        accounts.register_company(db, **form)
    assert err.value.code == code


def test_demo_seed_and_reset_keep_real_data():
    with SessionLocal() as db:
        real = models.User(email="real@company.ru", password_hash="x", role="borrower", company_name="Реальная")
        db.add(real)
        db.commit()
        demo.reset(db)
        demo_users = db.query(models.User).filter(models.User.email.like("%@demo.local")).count()
        statuses = {a.status for a in db.query(models.Application).filter(
            models.Application.user_id.in_(demo.demo_user_ids(db)))}
        assert demo_users == len(demo.COMPANIES)
        assert statuses == {"moderation", "rejected", "published", "in_work", "agreed"}
        demo.reset(db)  # повторный сброс не плодит дубликаты
        assert db.query(models.User).filter(models.User.email.like("%@demo.local")).count() == demo_users
        demo.clear(db)
        assert db.query(models.User).filter(models.User.email.like("%@demo.local")).count() == 0
        assert db.query(models.User).filter(models.User.email == "real@company.ru").count() == 1
