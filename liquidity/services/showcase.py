"""Показательный набор для защиты: верифицированные компании, заявки, сделки и отчёты.

Все записи привязаны к адресам @showcase.local и удаляются одной командой (scripts/showcase.py remove).
Публичная статистика на главной их не учитывает: там написано «данные в реальном времени».
"""
import random
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.security import hash_password
from liquidity.services import demo
from liquidity.services.accounts import INN_WEIGHTS

DOMAIN = config.SHOWCASE_DOMAIN
SEED = 20261107
BLANK_PDF = demo.DEMO_PDF

BORROWERS = [
    ("volga", "ООО «Волга Станкомаш»"), ("uralmet", "ООО «Уралметпром Сервис»"), ("neva", "АО «Невский приборный завод»"),
    ("kama", "ООО «Камские трубные системы»"), ("sibagro", "ООО «СибАгроТехника»"), ("baltlog", "ООО «Балтийская логистика»"),
    ("donhim", "ООО «Донхимпродукт»"), ("tula", "АО «Тульские приводы»"), ("akvilon", "ООО «Аквилон Медтех»"),
    ("severles", "ООО «Северлес Экспорт»"), ("orion", "ООО «Орион Электрокомплект»"), ("yuggrain", "ООО «ЮгЗерноТрейд»"),
]
CREDITORS = [
    ("meridian", "Meridian Trade Finance Ltd", "CY"), ("levant", "Levant Capital Partners Ltd", "CY"),
    ("emirate", "Emirate Bridge Finance FZE", "AE"), ("falcon", "Falcon Gulf Funding DMCC", "AE"),
    ("pearl", "Pearl River Credit Co", "CN"), ("harbour", "Harbour Asia Finance Ltd", "HK"),
    ("bosphorus", "Bosphorus Ticaret Finans AS", "TR"), ("altyn", "Altyn Steppe Capital TOO", "KZ"),
]
ROUTES = [("EUR", "DE", "swift"), ("EUR", "DE", "spfs"), ("EUR", "DE", "agent"), ("USD", "TR", "agent"), ("USD", "TR", "swift"),
          ("CNY", "CN", "cips"), ("CNY", "CN", "agent"), ("USD", "CN", "cips")]
AMOUNTS = {"EUR": (150000, 950000), "USD": (120000, 900000), "CNY": (900000, 6500000)}
BASE_RATE = {"EUR": 8.0, "USD": 9.0, "CNY": 7.0}
PURPOSES = [
    "Предоплата за обрабатывающие центры по контракту {n} с поставщиком", "Закупка комплектующих для производственной линии по контракту {n}",
    "Оплата промышленного оборудования по контракту {n}", "Пополнение оборотных средств под экспортный контракт {n}",
    "Закупка сырья для производства по договору поставки {n}", "Оплата логистического оборудования по контракту {n}",
    "Поставка медицинского оборудования по контракту {n}", "Закупка запасных частей для спецтехники по контракту {n}",
]
TERMS = [3, 6, 6, 9, 9, 12, 12, 18, 24]
EXTRA_TERMS = [None, None, "Проценты ежемесячно, основной долг в конце срока", "Обеспечение: залог оборудования",
               "Без досрочного погашения в первые 2 месяца", "Льготный период 1 месяц"]
TICKETS = [
    ("kyc", "Подскажите, в каком формате загрузить выписку из реестра?", "PDF до 4 МБ. Выписка должна быть не старше 30 дней."),
    ("application", "Можно ли изменить срок уже опубликованной заявки?", "Срок менять нельзя: отзовите заявку и подайте новую."),
    ("deal", "Кредитор просит изменить график, где зафиксировать новые условия?", "Обсудите их в чате сделки и отразите в договоре перед загрузкой."),
    ("access", "Сотрудник сменился, как поменять почту для входа?", "Напишите новую почту, поменяем после проверки полномочий."),
    ("bug", "Не открывается PDF договора в телефоне", None),
    ("other", "Есть ли ограничение на число одновременных заявок?", None),
    ("deal", "Когда оператор проверит загруженный договор?", None),
]


def _valid_inn(rng: random.Random) -> str:
    digits = [rng.randint(1, 9) for _ in range(9)]
    return "".join(map(str, digits)) + str(sum(w * d for w, d in zip(INN_WEIGHTS, digits)) % 11 % 10)


def password() -> str:
    """Надёжный пароль по правилам платформы: длина, регистры, цифра и спецсимвол."""
    return "Aa1!" + secrets.token_urlsafe(14)


def exists(db: Session) -> bool:
    return bool(demo.demo_user_ids(db, DOMAIN))


def remove(db: Session):
    demo.clear(db, DOMAIN)


def seed(db: Session, company_password: str = None, operator_password: str = None):
    """Создаёт набор и возвращает учётные записи: (роль, название, почта, пароль). Пароли нигде не сохраняются."""
    rng = random.Random(SEED)
    company_password, operator_password = company_password or password(), operator_password or password()
    now = datetime.utcnow().replace(microsecond=0)
    ago = lambda days, hours=0: now - timedelta(days=days, hours=hours)  # noqa: E731
    company_hash = hash_password(company_password)
    logins = []

    def company(key, name, role, country, regnum, since, **extra):
        user = models.User(email=f"{key}{DOMAIN}", password_hash=company_hash, role=role, company_name=name, country_code=country,
                           registration_number=regnum, created_at=since, kyc_approved_at=since + timedelta(days=2), **extra)
        db.add(user)
        db.flush()
        for doc_type, _ in config.KYC_DOCS[role]:
            path = f"{config.UPLOAD_DIR}/showcase_{key}_{doc_type}.pdf"
            db.add(models.StoredFile(path=path, content=BLANK_PDF))
            db.add(models.Document(user_id=user.id, doc_type=doc_type, file_path=path, status="approved", created_at=since + timedelta(days=1)))
        logins.append((config.ROLE_NAMES.get(role, role), name, user.email, company_password))
        return user

    borrowers = [company(key, name, "borrower", "RU", _valid_inn(rng), ago(rng.randint(130, 170)), egrul_status="ACTIVE",
                         registered_on=date(rng.randint(2005, 2020), rng.randint(1, 12), rng.randint(1, 28)))
                 for key, name in BORROWERS]
    creditors = [company(key, name, "creditor", country, f"{country}-{rng.randint(10000, 99999)}", ago(rng.randint(130, 170)))
                 for key, name, country in CREDITORS]
    operator = models.User(email=f"operator{DOMAIN}", password_hash=hash_password(operator_password), role="operator",
                           company_name="Оператор платформы", created_at=ago(170))
    db.add(operator)
    db.flush()
    logins.append((config.ROLE_NAMES.get("operator", "operator"), operator.company_name, operator.email, operator_password))

    def event(app, actor, text, when):
        db.add(models.ApplicationEvent(application_id=app.id, actor_id=actor.id if actor else None, text=text, created_at=when))

    def make_offer(app, creditor, when, status, amount=None, share_rate=None):
        rate = share_rate or round(BASE_RATE[app.currency_code] + rng.uniform(-1.8, 2.2), 1)
        comm = round(rng.uniform(0.6, 2.4), 1)
        days = rng.choice(config.OFFER_VALID_CHOICES)
        # Открытые предложения должны быть действующими, иначе автозакрытие снимет их при первом же запросе
        until = now + timedelta(days=rng.randint(10, 30)) if status == "shown" else when + timedelta(days=days)
        offer = models.Offer(application_id=app.id, user_id=creditor.id, funding_amount=amount if amount is not None else app.funding_amount,
                             annual_interest_rate=rate, platform_commission=config.PLATFORM_COMMISSION, lender_commission=comm,
                             total_commission=comm + config.PLATFORM_COMMISSION, extra_terms=rng.choice(EXTRA_TERMS), status=status,
                             created_at=when, valid_until=until.date(),
                             operator_decision_at=when if status != "pending" else None)
        db.add(offer)
        return offer

    # Распределение статусов: (статус, возраст заявки в днях от и до, сколько)
    plan = [("agreed", 28, 118, 22), ("in_work", 12, 30, 7), ("published", 1, 24, 15), ("moderation", 0, 2, 3),
            ("rejected", 5, 90, 3), ("withdrawn", 6, 80, 3), ("expired", 36, 100, 4)]
    number = 100
    agreed_apps, published_apps = [], []
    for status, low, high, count in plan:
        for _ in range(count):
            number += 1
            owner = rng.choice(borrowers)
            currency, country, method = rng.choice(ROUTES)
            low_amount, high_amount = AMOUNTS[currency]
            amount = Decimal(rng.randrange(low_amount, high_amount, 10000))
            created = ago(rng.randint(low, high), rng.randint(0, 20))
            published_at = created + timedelta(minutes=rng.randint(1, 40))
            app = models.Application(user_id=owner.id, funding_amount=amount, currency_code=currency, target_jurisdiction=country,
                                     term_months=rng.choice(TERMS), transfer_method=method, status=status, created_at=created,
                                     purpose=rng.choice(PURPOSES).format(n=f"{number}/{created.strftime('%y')}"))
            db.add(app)
            db.flush()
            if rng.random() < 0.45:
                app.contract_path = f"{config.UPLOAD_DIR}/showcase_{app.id}_kontrakt.pdf"
                db.add(models.StoredFile(path=app.contract_path, content=BLANK_PDF))
            event(app, owner, "Заявка создана", created)

            if status == "moderation":
                app.review_reason = "сумма выше порога" if amount > 800000 else "первая заявка компании"
                continue
            if status == "rejected":
                app.moderation_comment = "Не раскрыто назначение средств: укажите контракт и контрагента"
                event(app, operator, f"Заявка отклонена оператором: {app.moderation_comment}", created + timedelta(hours=5))
                continue

            app.valid_until = (published_at + timedelta(days=config.APP_VALID_DAYS)).date()
            event(app, None, "Заявка прошла автоматическую проверку и опубликована для кредиторов", published_at)
            lenders = rng.sample(creditors, rng.randint(2, 4))
            first_at = published_at + timedelta(hours=rng.randint(4, 60))
            offers = []
            for i, creditor in enumerate(lenders):
                when = min(first_at + timedelta(hours=i * rng.randint(3, 30)), now)
                state = "shown" if status == "published" else "rejected"
                offers.append(make_offer(app, creditor, when, state))
                event(app, creditor, f"Предложение {creditor.company_name} прошло автоматическую проверку и показано заёмщику", when)

            if status == "published":
                published_apps.append(app)
            elif status in ("agreed", "in_work"):
                db.flush()
                offers.sort(key=lambda o: float(o.annual_interest_rate) + float(o.total_commission) * 12 / app.term_months)
                offers[0].status = "accepted"
                syndicated = status == "agreed" and rng.random() < 0.12 and len(offers) > 1
                if syndicated:  # синдикат: сумму закрывают два кредитора
                    offers[0].funding_amount = (amount * Decimal("0.6")).quantize(Decimal("1"))
                    offers[1].funding_amount = amount - offers[0].funding_amount
                    offers[1].status = "accepted"
                winner = offers[0]
                accepted_at = min(first_at + timedelta(hours=rng.randint(30, 110)), now)
                event(app, owner, f"Принято предложение {winner.sender.company_name if winner.sender else ''}, сделка в работе".replace(
                    "None", ""), accepted_at)
                creditor = next(c for c in creditors if c.id == winner.user_id)
                db.add(models.Message(application_id=app.id, sender_id=owner.id, text="Добрый день! Готовы обсудить график платежей.",
                                      created_at=accepted_at + timedelta(hours=3)))
                db.add(models.Message(application_id=app.id, sender_id=creditor.id, text="Добрый день! Подтверждаем условия, готовим проект договора.",
                                      created_at=accepted_at + timedelta(hours=7)))
                if status == "agreed":
                    signed_at = accepted_at + timedelta(days=rng.randint(2, 6))
                    confirmed_at = signed_at + timedelta(hours=rng.randint(4, 40))
                    path = f"{config.UPLOAD_DIR}/showcase_{app.id}_dogovor_zayma_signed.pdf"
                    db.add(models.StoredFile(path=path, content=BLANK_PDF))
                    db.add(models.Message(application_id=app.id, sender_id=creditor.id, text="Загружен основной договор займа",
                                          file_path=path, created_at=signed_at))
                    event(app, creditor, f"{creditor.company_name} загрузил основной договор займа", signed_at)
                    event(app, operator, "Сделка подтверждена оператором: статус «Согласовано», сделка в архиве", confirmed_at)
                    app.confirmed_by, app.confirmed_at = operator.id, confirmed_at
                    agreed_apps.append((app, owner, [o for o in offers if o.status == "accepted"]))
            elif status == "withdrawn":
                event(app, owner, "Заявка отозвана заёмщиком", published_at + timedelta(days=4))
            elif status == "expired":
                event(app, None, "Срок публикации заявки истёк, открытые предложения закрыты", published_at + timedelta(days=config.APP_VALID_DAYS))
    db.flush()

    # Отчёты после сделки: большинство без замечаний, у части есть проблемы
    for app, owner, accepted in agreed_apps:
        recent = (now - app.confirmed_at).days < 45
        if rng.random() < 0.9:
            problem = rng.random() < 0.13
            db.add(models.DealReport(application_id=app.id, user_id=owner.id, role="borrower",
                                     funds="partial" if problem else "received", on_time="no" if problem else "yes",
                                     terms_match=not problem, new_terms=problem, contact_ok=True,
                                     comment="Вторая часть суммы задержана" if problem else None, updated_at=app.confirmed_at))
        for offer in accepted:
            if rng.random() < 0.8:
                db.add(models.DealReport(application_id=app.id, user_id=offer.user_id, role="creditor", funds="received",
                                         on_time="not_due" if recent else "yes", terms_match=True, new_terms=False, contact_ok=True,
                                         updated_at=app.confirmed_at))

    # Жалобы и обращения: оператору есть что показать
    for app in rng.sample(published_apps, 2):
        author = rng.choice(creditors)
        db.add(models.Complaint(application_id=app.id, user_id=author.id, created_at=ago(1), status="open",
                                reason="В назначении мало подробностей о контрагенте, просим проверить"))
    for topic, text, reply in TICKETS:
        author = rng.choice(borrowers + creditors)
        created = ago(rng.randint(0, 12), rng.randint(1, 20))
        db.add(models.SupportTicket(user_id=author.id, topic=topic, text=text, created_at=created, reply=reply,
                                    status="answered" if reply else "open", answered_by=operator.id if reply else None,
                                    answered_at=created + timedelta(hours=4) if reply else None))
    db.commit()
    return logins
