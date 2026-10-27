"""Бизнес-логика платформы. Сервисы не знают про HTTP: принимают сессию БД и данные,
возвращают результат или бросают доменное исключение, которое HTTP-слой превращает в ответ."""


class NotFound(Exception):
    """Объект не найден или недоступен в текущем статусе (HTTP 404)."""


class Forbidden(Exception):
    """Действие запрещено этому пользователю (HTTP 403)."""


class Invalid(Exception):
    """Данные не прошли проверку. code уходит в ?error=..., message показывается пользователю."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
