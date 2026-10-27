# Точка входа для Vercel: все запросы перенаправляются сюда (см. vercel.json).
# После rewrite функция видит путь /api/index, а исходный путь приходит в параметре __path —
# middleware возвращает его на место, чтобы маршруты FastAPI работали как обычно.
import os
import sys
from urllib.parse import parse_qsl, urlencode, quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app  # noqa: E402


class VercelPathMiddleware:
    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            params = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
            path, rest = None, []
            for key, value in params:
                if key == "__path" and path is None:
                    path = value
                else:
                    rest.append((key, value))
            if path is not None:
                path = "/" + path.lstrip("/")
                scope = dict(scope, path=path, raw_path=quote(path).encode(),
                             query_string=urlencode(rest).encode())
        await self.app(scope, receive, send)


app.add_middleware(VercelPathMiddleware)
