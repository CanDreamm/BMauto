"""Loopback-only callback service; expose its path through an HTTPS reverse proxy."""
import html
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

from .core import Store
from .oauth import GoogleConnections


def connections_from_env(store):
    admins = {int(x.strip()) for x in os.getenv("VK_ALLOWED_USERS", "").split(",") if x.strip()}
    return GoogleConnections(store, admins, os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials-web.json"),
                             os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "https://bot.itiscat.ru/oauth/google/callback"),
                             os.getenv("GOOGLE_CREDENTIALS_FILE", "token.json"))


def make_handler(factory):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes; never log them.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self.reply(200, "OAuth callback service is running")
                return
            if parsed.path != "/oauth/google/callback":
                self.reply(404, "Страница не найдена")
                return
            if len(self.path) > 16384:
                self.reply(400, "Некорректный запрос")
                return
            params = parse_qs(parsed.query)
            if any(len(params.get(k, [])) > 1 for k in ("state", "code", "error")):
                self.reply(400, "Некорректный запрос")
                return
            connection = None
            try:
                connection = factory()
                name = connection.finish(params.get("state", [""])[0], params.get("code", [None])[0], params.get("error", [None])[0])
                self.reply(200, f"Google подключён к проекту «{name}». Вернитесь в бот ВК и откройте даты проекта")
            except ValueError as exc:
                self.reply(400, str(exc))
            except Exception:
                self.reply(502, "Не удалось завершить подключение Google. Вернитесь в бот и запросите новую ссылку")
            finally:
                if connection is not None:
                    connection.db.close()
                    connection.store.db.close()

        def reply(self, status, message):
            content = ("<!doctype html><html lang='ru'><meta charset='utf-8'><title>BMauto — Google</title>"
                       "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                       f"<body><h1>Подключение Google</h1><p>{html.escape(message)}</p></body></html>").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
    return Handler


def main():
    load_dotenv()
    factory = lambda: connections_from_env(Store(os.getenv("APP_DATA", "data")))
    server = HTTPServer(("127.0.0.1", int(os.getenv("GOOGLE_OAUTH_PORT", "8787"))), make_handler(factory))
    print("Google OAuth callback: 127.0.0.1:" + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
