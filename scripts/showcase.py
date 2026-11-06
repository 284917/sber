"""Показательные данные для защиты. Запуск из папки проекта на сервере:

    python scripts/showcase.py seed      создать компании, заявки, сделки и вывести логины с паролями
    python scripts/showcase.py remove    удалить всё созданное этой командой

Пароли выводятся один раз и нигде не сохраняются. Настоящие компании и их данные не затрагиваются.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liquidity.database import SessionLocal, create_schema  # noqa: E402
from liquidity.services import showcase  # noqa: E402


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("seed", "remove"):
        sys.exit(__doc__)
    create_schema()
    with SessionLocal() as db:
        if command == "remove":
            showcase.remove(db)
            print("Показательные данные удалены.")
            return
        if showcase.exists(db):
            sys.exit("Показательные данные уже есть. Сначала выполните: python scripts/showcase.py remove")
        logins = showcase.seed(db)
    print(f"Создано учётных записей: {len(logins)}. Сохраните пароли: повторно они не показываются.\n")
    for role, name, email, password in logins:
        print(f"{role:<12} {email:<34} {password}   {name}")


if __name__ == "__main__":
    main()
