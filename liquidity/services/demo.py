"""Демо-режим для презентации (DEMO_MODE=1): наполненная база и вход в любую роль одной кнопкой.

Все демо-компании имеют email в домене @demo.local; сброс удаляет только их данные и создаёт заново.
Пароли демо-аккаунтов случайные: войти в них можно только кнопками демо-входа.
"""
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.security import hash_password

DEMO_DOMAIN = "@demo.local"
DEMO_LOGINS = {"borrower": "alfa@demo.local", "creditor": "beta@demo.local",
               "operator": "operator@demo.local", "admin": "admin@demo.local"}
# Минимальный корректный PDF, чтобы демо-документы открывались в браузере
DEMO_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")

COMPANIES = [
    # ключ, email, роль, название, страна, рег. номер, KYC
    ("alfa", "alfa@demo.local", "borrower", "ООО «Альфа Технологии»", "RU", "7701234560", "approved"),
    ("sever", "sever@demo.local", "borrower", "ООО «Северный терминал»", "RU", "7809876549", "approved"),
    ("vostok", "vostok@demo.local", "borrower", "ООО «Восток Агро»", "RU", "2536011223", "pending"),
    ("beta", "beta@demo.local", "creditor", "Beta Capital Ltd", "CY", "HE 412873", "approved"),
    ("gulf", "gulf@demo.local", "creditor", "Gulf Trade Finance FZE", "AE", "DMCC-77120", "approved"),
    ("rongtong", "rongtong@demo.local", "creditor", "Shenzhen Rongtong Capital", "CN", "91440300MA5F", "approved"),
    ("operator", "operator@demo.local", "operator", "Администрация платформы", None, None, None),
    ("admin", "admin@demo.local", "admin", "Администрация платформы", None, None, None),
]


def is_demo_email(email: str) -> bool:
    return (email or "").endswith(DEMO_DOMAIN)


def demo_user_ids(db: Session):
    return [u.id for u in db.query(models.User).filter(models.User.email.like(f"%{DEMO_DOMAIN}"))]


def reset(db: Session):
    """Удаляет только демо-данные и создаёт их заново."""
    clear(db)
    seed(db)


def clear(db: Session):
    ids = demo_user_ids(db)
    if not ids:
        return
    app_ids = [a.id for a in db.query(models.Application).filter(models.Application.user_id.in_(ids))]
    files = [m.file_path for m in db.query(models.Message).filter(
        (models.Message.application_id.in_(app_ids or [0])) | (models.Message.sender_id.in_(ids)))]
    files += [d.file_path for d in db.query(models.Document).filter(models.Document.user_id.in_(ids))]
    db.query(models.StoredFile).filter(models.StoredFile.path.in_([f for f in files if f] or [""])).delete(
        synchronize_session=False)
    db.query(models.ApplicationEvent).filter(models.ApplicationEvent.application_id.in_(app_ids or [0])).delete(
        synchronize_session=False)
    db.query(models.ApplicationEvent).filter(models.ApplicationEvent.actor_id.in_(ids)).update(
        {"actor_id": None}, synchronize_session=False)
    db.query(models.Message).filter((models.Message.application_id.in_(app_ids or [0]))
                                    | (models.Message.sender_id.in_(ids))).delete(synchronize_session=False)
    db.query(models.Offer).filter((models.Offer.application_id.in_(app_ids or [0]))
                                  | (models.Offer.user_id.in_(ids))).delete(synchronize_session=False)
    db.query(models.Document).filter(models.Document.user_id.in_(ids)).delete(synchronize_session=False)
    db.query(models.Application).filter(models.Application.id.in_(app_ids or [0])).delete(synchronize_session=False)
    db.query(models.Application).filter(models.Application.confirmed_by.in_(ids)).update(
        {"confirmed_by": None}, synchronize_session=False)
    db.query(models.User).filter(models.User.id.in_(ids)).delete(synchronize_session=False)
    db.commit()


def ensure_seeded(db: Session):
    if not db.query(models.User).filter(models.User.email == DEMO_LOGINS["admin"]).first():
        seed(db)


def seed(db: Session):
    now = datetime.utcnow().replace(microsecond=0)
    at = lambda days, hours=0: now - timedelta(days=days, hours=hours)  # noqa: E731

    users = {}
    for key, email, role, name, country, regnum, kyc_state in COMPANIES:
        user = models.User(email=email, password_hash=hash_password(secrets.token_urlsafe(16)), role=role,
                           company_name=name, country_code=country, registration_number=regnum,
                           created_at=at(14), is_superadmin=(key == "admin"))
        db.add(user)
        db.flush()
        users[key] = user
        if kyc_state:
            for doc_type, _ in config.KYC_DOCS[role]:
                path = f"{config.UPLOAD_DIR}/demo_{key}_{doc_type}.pdf"
                db.add(models.StoredFile(path=path, content=DEMO_PDF))
                db.add(models.Document(user_id=user.id, doc_type=doc_type, file_path=path, status=kyc_state,
                                       created_at=at(13)))

    def application(owner, amount, currency, country, term, method, purpose, status, days, comment=None):
        app = models.Application(user_id=users[owner].id, funding_amount=amount, currency_code=currency,
                                 target_jurisdiction=country, term_months=term, transfer_method=method,
                                 purpose=purpose, status=status, moderation_comment=comment, created_at=at(days))
        if status == "published":  # опубликованные демо-заявки открыты ещё на срок публикации
            app.valid_until = (at(days) + timedelta(days=config.APP_VALID_DAYS)).date()
        db.add(app)
        db.flush()
        return app

    def event(app, actor, text, days, hours=0):
        db.add(models.ApplicationEvent(application_id=app.id, actor_id=users[actor].id if actor else None,
                                       text=text, created_at=at(days, hours)))

    def offer(app, creditor, amount, rate, comm, status, days, extra=None, hide_reason=None):
        o = models.Offer(application_id=app.id, user_id=users[creditor].id, funding_amount=amount,
                         annual_interest_rate=rate, lender_commission=comm,
                         total_commission=comm + config.PLATFORM_COMMISSION, extra_terms=extra, status=status,
                         hide_reason=hide_reason, created_at=at(days),
                         operator_decision_at=at(days, -2) if status != "pending" else None)
        db.add(o)
        return o

    def message(app, sender, text, days, hours=0, file=None):
        path = None
        if file:
            path = f"{config.UPLOAD_DIR}/demo_{app.id}_{file}"
            db.add(models.StoredFile(path=path, content=DEMO_PDF))
        db.add(models.Message(application_id=app.id, sender_id=users[sender].id, text=text, file_path=path,
                              created_at=at(days, hours)))

    def moderated(app, days):
        event(app, "alfa" if app.user_id == users["alfa"].id else "sever", "Заявка создана и отправлена на модерацию", days)
        event(app, "admin", "Заявка прошла модерацию и опубликована для кредиторов", days, -3)

    # 1. Опубликована, три предложения — показать сравнение по полной стоимости
    a1 = application("alfa", 500000, "EUR", "DE", 6, "swift", "Закупка станков с ЧПУ у поставщика в Германии",
                     "published", 6)
    moderated(a1, 6)
    offer(a1, "beta", 500000, 8.0, 1.0, "shown", 5, "Проценты ежемесячно, основной долг в конце срока. Льготный период 1 месяц")
    offer(a1, "gulf", 500000, 7.2, 2.2, "shown", 4, "Погашение равными долями ежемесячно")
    offer(a1, "rongtong", 450000, 7.6, 1.4, "shown", 3, "Обеспечение: залог оборудования")
    for creditor, days in (("beta", 5), ("gulf", 4), ("rongtong", 3)):
        event(a1, creditor, "Получено новое предложение, ждёт проверки оператором", days)
        event(a1, "operator", f"Оператор показал заёмщику предложение {users[creditor].company_name}", days, -2)

    # 2. В работе: принято предложение, идёт обсуждение, договора ещё нет
    a2 = application("alfa", 1200000, "CNY", "CN", 12, "cips", "Предоплата по контракту на поставку комплектующих",
                     "in_work", 10)
    moderated(a2, 10)
    offer(a2, "rongtong", 1200000, 6.9, 1.5, "accepted", 9, "Погашение равными долями ежеквартально")
    offer(a2, "beta", 1000000, 6.4, 2.4, "rejected", 9)
    event(a2, "operator", "Оператор показал заёмщику предложение Shenzhen Rongtong Capital", 9, -2)
    event(a2, "operator", "Оператор показал заёмщику предложение Beta Capital Ltd", 9, -3)
    event(a2, "alfa", "Принято предложение Shenzhen Rongtong Capital, сделка в работе", 8)
    message(a2, "alfa", "Добрый день! Можно ли перенести первый платёж на конец квартала?", 7, 5)
    message(a2, "rongtong", "Да, первый платёж — в конце квартала. Готовим проект договора.", 7, 2)
    message(a2, "rongtong", "Проект договора для согласования", 6, 4, file="proekt_dogovora.pdf")

    # 3. Согласована: договор загружен, оператор подтвердил — в архиве и аналитике
    a3 = application("sever", 250000, "USD", "TR", 9, "agent", "Пополнение оборотного капитала под экспортный контракт",
                     "agreed", 20)
    a3.confirmed_by, a3.confirmed_at = users["operator"].id, at(12)
    moderated(a3, 20)
    offer(a3, "gulf", 250000, 9.5, 1.2, "accepted", 18, "Оплата через платёжного агента в Стамбуле")
    event(a3, "operator", "Оператор показал заёмщику предложение Gulf Trade Finance FZE", 18, -2)
    event(a3, "sever", "Принято предложение Gulf Trade Finance FZE, сделка в работе", 17)
    message(a3, "gulf", "Направляем подписанный договор займа", 13, 3)
    message(a3, "gulf", "Загружен основной договор займа", 13, 2, file="dogovor_zayma_signed.pdf")
    event(a3, "gulf", "Gulf Trade Finance FZE загрузил основной договор займа", 13, 2)
    event(a3, "operator", "Сделка подтверждена оператором: статус «Согласовано», сделка в архиве", 12)

    # 4. На модерации — показать работу администратора
    a4 = application("sever", 800000, "EUR", "DE", 18, "spfs", "Финансирование поставки портовых кранов",
                     "moderation", 1)
    event(a4, "sever", "Заявка создана и отправлена на модерацию", 1)

    # 5. Отклонена модератором
    a5 = application("alfa", 90000, "USD", "TR", 3, "swift", "Прочие нужды", "rejected", 15,
                     comment="Не раскрыто назначение средств: укажите контракт и контрагента")
    event(a5, "alfa", "Заявка создана и отправлена на модерацию", 15)
    event(a5, "admin", "Заявка отклонена модератором: Не раскрыто назначение средств: укажите контракт и контрагента", 15, -4)

    # 6. Опубликована, предложение ждёт оператора — показать проверку оператором
    a6 = application("sever", 300000, "USD", "CN", 6, "cips", "Закупка оборудования для склада", "published", 3)
    moderated(a6, 3)
    offer(a6, "rongtong", 300000, 8.4, 1.1, "pending", 1, "Без досрочного погашения в первые 2 месяца")
    offer(a6, "beta", 350000, 7.9, 1.0, "pending", 1)  # сумма больше запрошенной — оператор увидит ошибку
    event(a6, "rongtong", "Получено новое предложение, ждёт проверки оператором", 1)
    event(a6, "beta", "Получено новое предложение, ждёт проверки оператором", 1)

    # Договоры в демо — настоящие PDF с условиями сделки: их можно открыть и просмотреть у оператора
    from liquidity.services.deals import accepted_offer
    from liquidity.services.documents import contract_draft_pdf

    db.flush()
    for app in (a2, a3):
        deal_offer = accepted_offer(db, app.id)
        if deal_offer:
            content = contract_draft_pdf(app, deal_offer, None)
            for stored in db.query(models.StoredFile).filter(
                    models.StoredFile.path.like(f"{config.UPLOAD_DIR}/demo_{app.id}_%dogovor%")):
                stored.content = content

    db.commit()
