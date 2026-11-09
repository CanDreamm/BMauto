"""Google OAuth and read-only monitoring behind an HTTPS reverse proxy."""
import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

from .core import Store
from .oauth import GoogleConnections
from .monitor import ASSETS, MonitorNotFound, MonitorService, MonitorUnavailable, render_error, render_monitor

MONITOR_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def connections_from_env(store):
    admins = {int(x.strip()) for x in os.getenv("VK_ALLOWED_USERS", "").split(",") if x.strip()}
    return GoogleConnections(store, admins, os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials-web.json"),
                             os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "https://bot.itiscat.ru/oauth/google/callback"),
                             os.getenv("GOOGLE_CREDENTIALS_FILE", "token.json"))


def make_handler(factory, monitor_service=None):
    monitor_service = monitor_service or MonitorService(factory)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # URLs can contain OAuth codes or secret monitoring tokens.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if len(self.path) > 16384:
                self.reply(400, "Некорректный запрос")
                return
            if parsed.path.startswith("/monitor-assets/"):
                name = parsed.path.removeprefix("/monitor-assets/")
                types = {"monitor.css": "text/css; charset=utf-8", "monitor.js": "text/javascript; charset=utf-8"}
                if name not in types:
                    self.reply(404, "Страница не найдена")
                    return
                self.respond(200, (ASSETS / name).read_bytes(), types[name], cache="public, max-age=3600")
                return
            if parsed.path.startswith("/monitor/"):
                token = parsed.path.removeprefix("/monitor/")
                params = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    month = params.get("month", [None])
                    if len(month) != 1 or (month[0] is not None and (not month[0].isascii() or not month[0].isdigit() or len(month[0]) > 10)):
                        raise MonitorNotFound()
                    if params.get("refresh", ["0"]) not in (["0"], ["1"]):
                        raise MonitorNotFound()
                    data = monitor_service.load(token, int(month[0]) if month[0] is not None else None, force=params.get("refresh") == ["1"])
                    self.respond(200, render_monitor(data, parsed.path).encode(), "text/html; charset=utf-8")
                except MonitorNotFound:
                    self.respond(404, render_error("Страница не найдена или ссылка больше не действует").encode(), "text/html; charset=utf-8")
                except MonitorUnavailable as exc:
                    self.respond(503, render_error(str(exc)).encode(), "text/html; charset=utf-8")
                return
            if parsed.path == "/healthz":
                self.reply(200, "OAuth callback service is running")
                return
            if parsed.path != "/oauth/google/callback":
                self.reply(404, "Страница не найдена")
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
            self.respond(status, content, "text/html; charset=utf-8", csp="default-src 'none'; frame-ancestors 'none'")

        def do_HEAD(self):
            if urlsplit(self.path).path == "/oauth/google/callback":
                self.respond(405, b"Method not allowed", "text/plain; charset=utf-8", allow="GET")
            else:
                self.do_GET()

        def read_only(self):
            self.respond(405, b"Method not allowed", "text/plain; charset=utf-8", allow="GET, HEAD")

        do_POST = do_PUT = do_PATCH = do_DELETE = read_only

        def respond(self, status, content, content_type, cache="no-store", csp=MONITOR_CSP, allow=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            if allow:
                self.send_header("Allow", allow)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if getattr(self, "command", "GET") != "HEAD":
                self.wfile.write(content)
    return Handler


def main():
    load_dotenv()
    factory = lambda: connections_from_env(Store(os.getenv("APP_DATA", "data")))
    server = ThreadingHTTPServer(("127.0.0.1", int(os.getenv("GOOGLE_OAUTH_PORT", "8787"))), make_handler(factory))
    print("Google OAuth callback: 127.0.0.1:" + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
