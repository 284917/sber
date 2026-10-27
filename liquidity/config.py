"""Настройки из переменных окружения и справочники предметной области."""
import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# --- Окружение ---
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-secret-change-me")
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "8"))
ON_VERCEL = bool(os.getenv("VERCEL"))
# Cookie только по HTTPS: на Vercel всегда HTTPS, локально и в тестах — HTTP
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "1" if ON_VERCEL else "0") == "1"
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
# Демо-режим для презентации: вход в любую роль в один клик и наполненная база
DEMO_MODE = os.getenv("DEMO_MODE") == "1"
# Публичный адрес сайта: нужен для абсолютных ссылок в превью соцсетей (og:image)
SITE_URL = os.getenv("SITE_URL", "https://sber-delta.vercel.app").rstrip("/")

# --- Роли ---
PUBLIC_ROLES = {"borrower", "creditor"}  # можно выбрать при самостоятельной регистрации
STAFF_ROLES = {"admin", "operator"}      # создаёт только администратор
ROLE_NAMES = {"borrower": "Заёмщик", "creditor": "Кредитор", "admin": "Администратор", "operator": "Оператор"}

# --- Справочники заявки ---
CURRENCIES = ["EUR", "USD", "CNY"]
# Куда заёмщик получает средства (как на макете в Miro)
TARGET_COUNTRIES = {"DE": "Германия", "TR": "Турция", "CN": "Китай"}
# Откуда могут быть кредиторы
CREDITOR_COUNTRIES = {"CY": "Кипр", "AE": "ОАЭ", "CN": "Китай", "TR": "Турция", "DE": "Германия",
                      "KZ": "Казахстан", "HK": "Гонконг", "IN": "Индия"}
COUNTRY_NAMES = {"RU": "Россия", **CREDITOR_COUNTRIES, **TARGET_COUNTRIES}
TRANSFER_METHODS = {"swift": "SWIFT", "spfs": "СПФС", "cips": "CIPS", "agent": "Платёжный агент"}
MAX_TERM_MONTHS = 120
PLATFORM_COMMISSION = 0.8  # % от суммы, добавляется к комиссии кредитора

# --- Статусы ---
APPLICATION_STATUSES = {"moderation": "На модерации", "rejected": "Отклонена", "published": "Опубликована",
                        "in_work": "В работе", "agreed": "Согласовано", "withdrawn": "Отозвана"}
OFFER_STATUSES = {"pending": "На проверке у оператора", "shown": "Получено заёмщиком", "hidden": "Скрыто оператором",
                  "accepted": "Принято", "rejected": "Отклонено", "withdrawn": "Отозвано кредитором"}
KYC_STATUSES = {"approved": "Верифицирована", "pending": "На проверке", "rejected": "Отказ в верификации",
                "none": "Не верифицирована"}

# --- Пакеты документов KYC ---
KYC_DOCS = {
    "borrower": [("charter", "Устав компании"), ("egrul", "Выписка из ЕГРЮЛ"), ("passport", "Паспорт директора"),
                 ("beneficiaries", "Сведения о бенефициарах (UBO)"),
                 ("director_auth", "Документ о полномочиях директора")],
    "creditor": [("cert_inc", "Свидетельство о регистрации (Certificate of Incorporation)"),
                 ("memorandum", "Устав (Memorandum and Articles)"),
                 ("directors_reg", "Реестр директоров и участников"), ("ubo", "Сведения о бенефициарах (UBO)"),
                 ("tax_residency", "Сертификат налогового резидентства"),
                 ("source_funds", "Подтверждение источника средств (отчётность или справка)")],
}

# Новые файлы хранятся в БД (таблица stored_files), у Vercel нет постоянного диска.
# Папка uploads нужна только для файлов, загруженных до этого изменения.
UPLOAD_DIR = "uploads"

# Расшифровки сокращений: показываются подсказкой при наведении (фильтр terms в шаблонах)
GLOSSARY = {
    "ЕГРЮЛ": "Единый государственный реестр юридических лиц. Выписка подтверждает, что компания существует, кто ей руководит и где она зарегистрирована",
    "ИНН": "Идентификационный номер налогоплательщика. У юрлица 10 цифр, последняя контрольная",
    "KYC": "Know Your Customer: проверка, что компания реальна и действует законно. Проводит администратор по пакету документов",
    "AML": "Anti-Money Laundering: противодействие отмыванию денег. Проверяем источник средств и бенефициаров",
    "UBO": "Ultimate Beneficial Owner: конечный бенефициар, физическое лицо, которое владеет компанией или контролирует её",
    "SWIFT": "Международная система межбанковских платёжных сообщений",
    "СПФС": "Система передачи финансовых сообщений Банка России, российский аналог SWIFT",
    "CIPS": "Cross-Border Interbank Payment System: китайская система трансграничных платежей в юанях",
}
