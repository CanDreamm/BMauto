"""Read-only project dashboard. Bearer URLs never enumerate projects."""
import html
import hashlib
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from string import Template
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from .calendar import MONTHS
from .core import assignment_cell, parse_date, table_layout
from .google import Google
from .statuses import APPROVED, NO_PUBLICATION, NOT_PLANNED, PLANNED, PUBLISHED

ASSETS = Path(__file__).with_name("web")
ASSET_VERSION = hashlib.sha256((ASSETS / "monitor.css").read_bytes() + (ASSETS / "monitor.js").read_bytes()).hexdigest()[:12]
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
STATUS_CLASS = {APPROVED: "approved", PLANNED: "planned", PUBLISHED: "published",
                NO_PUBLICATION: "empty", NOT_PLANNED: "missed"}


class MonitorNotFound(Exception):
    pass


class MonitorUnavailable(Exception):
    pass


def monitor_url(store, project, connections=None):
    redirect = urlsplit(getattr(connections, "redirect_uri", "https://bot.itiscat.ru/oauth/google/callback"))
    origin = f"{redirect.scheme}://{redirect.netloc}"
    return origin + "/monitor/" + store.monitor_token(project["id"])


def month_sheets(sheets):
    order = {name.casefold(): i for i, name in enumerate(MONTHS)}
    return sorted((s for s in sheets if s["title"].strip().casefold() in order),
                  key=lambda s: (order[s["title"].strip().casefold()], s["sheetId"]))


def monitor_rows(values, project):
    if not values:
        return []
    header, _ = table_layout(values, project)
    rows = []
    for number, raw in enumerate(values[header:], header + 1):
        cells = [str(value) if value is not None else "" for value in raw] + [""] * 4
        if not cells[1].strip():
            continue
        try:
            day = parse_date(cells[1].strip())
        except ValueError:
            continue
        assignment, link = assignment_cell(cells[3])
        status = cells[0].strip()
        hint = ""
        if assignment.strip():
            if not status:
                hint = "Подготовить материалы"
            elif status == PLANNED:
                hint = "Ожидает согласования"
            elif status == APPROVED:
                hint = "Готово к публикации"
            elif status == NOT_PLANNED:
                hint = "Материалы не были запланированы"
        rows.append({"row": number, "date": day.isoformat(), "date_label": day.strftime("%d.%m.%Y"),
                     "weekday": cells[2], "assignment": assignment, "materials": link,
                     "status": status, "tone": STATUS_CLASS.get(status, "neutral"), "hint": hint})
    return sorted(rows, key=lambda item: (item["date"], item["row"]))


class MonitorService:
    def __init__(self, factory, google_factory=Google, ttl=30):
        self.factory, self.google_factory, self.ttl = factory, google_factory, ttl
        self.cache, self.lock = {}, threading.Lock()

    def load(self, token, selected=None, force=False):
        if not TOKEN_RE.fullmatch(token):
            raise MonitorNotFound()
        connection = google = None
        try:
            connection = self.factory()
            project = connection.store.monitor_project(token)
            if project is None:
                raise MonitorNotFound()
            key = (project["id"], project.get("revision"), project.get("google_connection_id"), project.get("spreadsheet_id"), selected)
            with self.lock:
                cached = self.cache.get(key)
            if not force and cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1]
            if not project.get("spreadsheet_id"):
                raise MonitorUnavailable("Для проекта ещё не указана Google Таблица. Добавьте её в настройках бота")
            google = self.google_factory(connection.path_for(project))
            sheets = month_sheets(google.worksheets(project))
            if not sheets:
                raise MonitorUnavailable("В таблице не найдены вкладки с названиями месяцев")
            if selected is None:
                month = MONTHS[datetime.now(ZoneInfo("Europe/Moscow")).month - 1].casefold()
                sheet = next((s for s in sheets if s["title"].strip().casefold() == month), sheets[-1])
            else:
                sheet = next((s for s in sheets if s["sheetId"] == selected), None)
                if sheet is None:
                    raise MonitorNotFound()
            source = {**project, "_sheet_id": sheet["sheetId"], "worksheet": sheet["title"]}
            rows = monitor_rows(google.rows(source), project)
            data = {"name": project["name"], "sheets": sheets, "sheet_id": sheet["sheetId"],
                    "month": sheet["title"].strip(), "rows": rows,
                    "updated": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")}
            # Rotation/deletion must also revoke requests that were waiting on Google.
            if connection.store.monitor_project(token) is None:
                raise MonitorNotFound()
            with self.lock:
                now = time.monotonic()
                self.cache = {k: v for k, v in self.cache.items() if now - v[0] < self.ttl}
                self.cache[key] = (now, data)
            return data
        except (MonitorNotFound, MonitorUnavailable):
            raise
        except Exception:
            raise MonitorUnavailable("Не удалось загрузить данные. Проверьте подключение Google и доступ к таблице в боте") from None
        finally:
            if google is not None and hasattr(google, "http"):
                google.http.close()
            if connection is not None:
                connection.db.close()
                connection.store.db.close()


def render_monitor(data, path):
    esc = html.escape
    rows = data["rows"]
    counters = (("Всего заданий", sum(bool(r["assignment"].strip()) for r in rows), "all"),
                ("Нужно подготовить", sum(bool(r["assignment"].strip()) and not r["status"] for r in rows), "neutral"),
                ("Ждут согласования", sum(r["status"] == PLANNED for r in rows), "planned"),
                ("Вышло", sum(r["status"] == PUBLISHED for r in rows), "published"))
    stats = "".join(f'<div class="stat {tone}"><span>{label}</span><strong>{count:02d}</strong></div>' for label, count, tone in counters)
    body = []
    for row in rows:
        status = (f'<span class="badge {row["tone"]}"><i></i>{esc(row["status"])}</span>' if row["status"]
                  else '<span class="blank-status" aria-label="Статус не задан">—</span>')
        assignment = esc(row["assignment"]) if row["assignment"].strip() else '<span class="no-task">Задание не задано</span>'
        hint = f'<div class="task-hint">{esc(row["hint"])}</div>' if row["hint"] else ""
        link = (f'<a class="materials" href="{esc(row["materials"], quote=True)}" target="_blank" rel="noreferrer noopener">Материалы '
                '<svg aria-hidden="true" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></a>'
                if row["materials"] else "")
        body.append(f'<tr class="row-{row["tone"]}"><td>{status}</td><td class="date"><time datetime="{row["date"]}">{row["date_label"]}</time></td>'
                    f'<td class="weekday">{esc(row["weekday"])}</td><td class="task"><div class="task-text">{assignment}</div>{hint}{link}</td></tr>')
    if not body:
        body.append('<tr><td colspan="4" class="empty-table">В этом месяце пока нет строк с датами</td></tr>')
    tabs = []
    for sheet in data["sheets"]:
        active = sheet["sheetId"] == data["sheet_id"]
        url = path + "?" + urlencode({"month": sheet["sheetId"]})
        tabs.append(f'<a class="month-tab{" active" if active else ""}" href="{esc(url, quote=True)}"'
                    f'{" aria-current=page" if active else ""}>{esc(sheet["title"].strip())}</a>')
    template = Template((ASSETS / "monitor.html").read_text())
    return template.substitute(title=esc(data["name"]), month=esc(data["month"]), updated=esc(data["updated"]),
                               assets=ASSET_VERSION, stats=stats, rows="".join(body), tabs="".join(tabs), refresh=esc(path + "?" + urlencode({"month": data["sheet_id"], "refresh": 1}), quote=True))


def render_error(message):
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>BMauto · Мониторинг</title><link rel="stylesheet" href="/monitor-assets/monitor.css?v={ASSET_VERSION}">'
            '<body><main class="error-screen"><div class="brand">BM<span>auto</span></div><h1>Мониторинг недоступен</h1>'
            f'<p>{html.escape(message)}</p><a class="refresh" href="">Попробовать снова ↻</a></main></body></html>')
