"""Регистрация юрлиц, вход и создание сотрудников."""
import re
import secrets

from sqlalchemy.orm import Session

from liquidity import config, models
from liquidity.security import hash_password, verify_password
from liquidity.services import Forbidden, Invalid, NotFound
from liquidity.services.audit import record


INN_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)


def inn_is_valid(inn: str) -> bool:
    """ИНН юрлица: 10 цифр, последняя — контрольная: сумма цифр с весами 2,4,10,3,5,9,4,6,8 по модулю 11, затем 10."""
    if not re.fullmatch(r"\d{10}", inn or ""):
        return False
    digits = [int(c) for c in inn]
    return sum(w * d for w, d in zip(INN_WEIGHTS, digits)) % 11 % 10 == digits[9]


def register_company(db: Session, *, email: str, password: str, role: str, company_name: str,
                     registration_number: str, country_code: str) -> models.User:
    """Самостоятельная регистрация: только заёмщик (российское юрлицо) или кредитор (зарубежное)."""
    if role not in config.PUBLIC_ROLES:
        raise Invalid("role", "Недопустимая роль")
    company_name, registration_number = company_name.strip(), registration_number.strip()
    if not company_name or len(company_name) > 255:
        raise Invalid("company", "Укажите название компании")
    if len(password) < 8:
        raise Invalid("password", "Пароль должен быть не короче 8 символов")
    if role == "borrower":
        if not re.fullmatch(r"\d{10}", registration_number):
            raise Invalid("inn", "ИНН юрлица должен состоять из 10 цифр")
        if not inn_is_valid(registration_number):
            raise Invalid("inn", "ИНН не прошёл проверку контрольной цифры. Проверьте, нет ли опечатки")
        country_code = "RU"
    else:
        if country_code not in config.CREDITOR_COUNTRIES:
            raise Invalid("country", "Выберите страну регистрации кредитора")
        if not registration_number or len(registration_number) > 50:
            raise Invalid("regnum", "Укажите регистрационный номер компании")
    if db.query(models.User).filter(models.User.email == email).first():
        raise Invalid("exists", "Пользователь с таким email уже существует")

    user = models.User(email=email, password_hash=hash_password(password), role=role, company_name=company_name,
                       registration_number=registration_number, country_code=country_code)
    db.add(user)
    db.commit()
    return user


def authenticate(db: Session, email: str, password: str):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def staff_list(db: Session):
    return db.query(models.User).filter(models.User.role.in_(config.STAFF_ROLES)).order_by(
        models.User.is_superadmin.desc(), models.User.role, models.User.email).all()


def remove_staff(db: Session, actor: models.User, staff_id: int):
    """Отключает сотрудника: войти больше нельзя, но история его действий сохраняется.
    Email освобождается, чтобы при необходимости завести учётку заново."""
    if not actor.is_superadmin:
        raise Forbidden()
    target = db.get(models.User, staff_id)
    if not target or target.role not in config.STAFF_ROLES:
        raise NotFound()
    if target.id == actor.id or target.is_superadmin:
        raise Invalid("self", "Главного администратора отключить нельзя")
    record(db, actor, f"Отключил сотрудника {target.email}")
    target.role = "removed"
    target.email = f"removed-{target.id}-{target.email}"[:255]
    target.password_hash = hash_password(secrets.token_urlsafe(24))
    db.commit()
    return target


def create_staff(db: Session, *, email: str, password: str, role: str, actor=None) -> models.User:
    if role not in config.STAFF_ROLES:
        raise Invalid("role", "Недопустимая роль")
    if len(password) < 8:
        raise Invalid("password", "Пароль должен быть не короче 8 символов")
    if db.query(models.User).filter(models.User.email == email).first():
        raise Invalid("exists", "Пользователь с таким email уже существует")
    user = models.User(email=email, password_hash=hash_password(password), role=role,
                       company_name="Администрация платформы")
    db.add(user)
    record(db, actor, f"Создал сотрудника {email} ({config.ROLE_NAMES.get(role, role)})")
    db.commit()
    return user


def ensure_admin(db: Session):
    """Первый админ из ADMIN_EMAIL / ADMIN_PASSWORD, если его ещё нет (нужно для пустой БД)."""
    if not config.ADMIN_EMAIL or not config.ADMIN_PASSWORD:
        return
    if not db.query(models.User).filter(models.User.email == config.ADMIN_EMAIL).first():
        db.add(models.User(email=config.ADMIN_EMAIL, password_hash=hash_password(config.ADMIN_PASSWORD),
                           role="admin", company_name="Администрация платформы", is_superadmin=True))
        db.commit()
    else:
        owner = db.query(models.User).filter(models.User.email == config.ADMIN_EMAIL).first()
        if owner.role == "admin" and not owner.is_superadmin:
            owner.is_superadmin = True
            db.commit()
