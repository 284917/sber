"""Вторая волна отзывов: надёжный пароль и «Мой аккаунт», понятный отказ KYC, комиссия 3%, минимальная доля
предложения, договор загружают только стороны, оператор видит PDF у себя, лимит размера файла на клиенте."""
import pytest

from conftest import PDF, Browser, login
from liquidity import config, models
from liquidity.database import SessionLocal
from liquidity.services.accounts import password_problems
from test_features import make_company

STRONG = "Str0ng!Pass1"


@pytest.mark.parametrize("password, ok", [
    ("Str0ng!Pass1", True),
    ("Сильный#Пароль9", True),
    ("short!A1", False),            # короче 10
    ("alllowercase1!", False),      # нет заглавной
    ("ALLUPPERCASE1!", False),      # нет строчной
    ("NoDigitsHere!!", False),      # нет цифры
    ("NoSpecials123", False),       # нет спецсимвола
    ("ПарольБезЗнаков1", False),    # кириллица не считается спецсимволом
])
def test_password_policy(password, ok):
    assert (password_problems(password) == []) is ok


def test_register_rejects_weak_password_and_account_page():
    weak = Browser().post("/register", data={"email": "weak@test.ru", "password": "password1234", "role": "borrower",
                                             "company_name": "ООО «Слабый»", "registration_number": "7707083893"})
    assert weak.status_code == 400 and "Пароль слишком простой" in weak.text and "заглавная буква" in weak.text
    ok = Browser().post("/register", data={"email": "acc@test.ru", "password": STRONG, "role": "borrower",
                                           "company_name": "ООО «Аккаунт»", "registration_number": "7707083893"})
    assert ok.status_code == 303

    user = login("acc@test.ru", STRONG)
    page = user.get("/account").text
    assert "Мой аккаунт" in page and "acc@test.ru" in page and "7707083893" in page and "Смена пароля" in page
    assert 'href="/account"' in user.get("/dashboard").text

    def change(current, new, repeat=None):
        return user.post("/account/password", data={"current_password": current, "new_password": new,
                                                     "repeat_password": new if repeat is None else repeat}).headers["location"]
    assert change("wrong", "N3w!Password").endswith("error=current#password")
    assert change(STRONG, "N3w!Password", "other").endswith("error=repeat#password")
    assert change(STRONG, "weakpassword").endswith("error=password#password")
    assert change(STRONG, STRONG).endswith("error=same#password")
    assert change(STRONG, "N3w!Password") == "/account?password_changed=1"
    assert Browser().post("/login", data={"email": "acc@test.ru", "password": STRONG}).status_code == 400
    login("acc@test.ru", "N3w!Password")

    admin = login("admin@test.local", "admin-password")
    assert admin.get("/account").status_code == 200
    assert admin.post("/admin/create_user", data={"email": "weak-op@test.local", "password": "operator123",
                                                  "role": "operator"}).headers["location"].endswith("error=password")


def test_kyc_rejection_message_and_textarea():
    make_company("rej-c@test.cy", "creditor", "Reject Capital", "CY", "HE 71")
    with SessionLocal() as db:
        user = db.query(models.User).filter_by(email="rej-c@test.cy").one()
        for doc in db.query(models.Document).filter_by(user_id=user.id):
            doc.status = "pending"
        db.commit()
        uid = user.id
    admin = login("admin@test.local", "admin-password")
    queue = admin.get("/admin?tab=kyc").text
    assert '<textarea name="reason"' in queue
    reason = "Сертификат резидентства просрочен.\nЗагрузите документ за текущий год."
    admin.post(f"/admin/kyc/reject_user/{uid}", data={"reason": reason})
    page = login("rej-c@test.cy").get("/kyc").text
    assert page.index("Верификация отклонена.") < page.index("Загрузите документы заново") < page.index("Причина отказа")
    assert "Сертификат резидентства просрочен." in page
    with SessionLocal() as db:
        text = db.query(models.Notification).filter_by(user_id=uid).order_by(models.Notification.id.desc()).first().text
    assert text.startswith("Верификация отклонена. Загрузите документы заново. Причина:")


def test_fee_min_offer_and_contract_rights():
    assert config.PLATFORM_COMMISSION == 3.0
    make_company("min-b@test.ru", "borrower", "ООО «Минимум»", "RU", "7701542036")
    make_company("min-c@test.cn", "creditor", "Min Lender", "CN", "CN 81")
    borrower, creditor = login("min-b@test.ru"), login("min-c@test.cn")
    admin = login("admin@test.local", "admin-password")
    borrower.post("/create_application", data={"amount": "500000", "currency": "CNY", "country": "CN", "purpose": "Минимум",
                                               "term_months": "12", "transfer_method": "cips"})
    with SessionLocal() as db:
        app_id = db.query(models.Application).filter_by(purpose="Минимум").one().id
    admin.post(f"/admin/application/{app_id}/moderate", data={"decision": "approve"})

    # 40 000 из 500 000 — меньше 10%: платформа останавливает отправку
    small = creditor.post(f"/create_offer/{app_id}", data={"amount": "40000", "rate": "8", "lender_comm": "1"})
    assert small.headers["location"].endswith("error=offer_min")
    assert "минимум 10%" in creditor.get("/dashboard?error=offer_min").text
    assert 'min="50000' in creditor.get("/dashboard").text
    creditor.post(f"/create_offer/{app_id}", data={"amount": "500000", "rate": "8", "lender_comm": "1"})
    with SessionLocal() as db:
        offer = db.query(models.Offer).filter_by(application_id=app_id).one()
        assert float(offer.platform_commission) == 3.0 and float(offer.total_commission) == 4.0
        offer_id = offer.id
    assert "Заёмщик увидит его после проверки оператором" in creditor.get("/dashboard").text
    admin.post(f"/operator/offer/{offer_id}/show")
    borrower.post(f"/accept_offer/{offer_id}")

    # Сотрудник не загружает договор, стороны — да
    assert admin.post(f"/deal/{app_id}/upload_contract", files={"file": PDF}).status_code == 403
    assert "Загрузить договор" not in admin.get(f"/deal/{app_id}").text
    assert borrower.post(f"/deal/{app_id}/upload_contract", files={"file": PDF}).status_code == 303
    deal = admin.get(f"/operator/deal/{app_id}").text
    assert 'class="pdf-frame"' in deal and 'type="application/pdf"' in deal and "Открыть в новой вкладке" in deal and "SHA-256" in deal
    assert "Открыть PDF" in admin.get("/operator/deals").text
    pdf = admin.get(f"/deal/{app_id}/contract-draft.pdf")
    assert pdf.status_code == 200

    # Клиентская проверка размера файла есть в каждом кабинете
    assert "const MAX_UPLOAD = 4194304" in borrower.get(f"/deal/{app_id}").text
