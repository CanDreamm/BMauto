"""Read-only personal dashboards scoped to current project memberships."""
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


def monitor_url(store, user, connections=None, admins=()):
    redirect = urlsplit(getattr(connections, "redirect_uri", "https://bot.itiscat.ru/oauth/google/callback"))
    origin = f"{redirect.scheme}://{redirect.netloc}"
    return origin + "/monitor/" + store.monitor_token(user, admins)


def month_sheets(sheets):
    order = {name.casefold(): i for i, name in enumerate(MONTHS)}
    return sorted((s for s in sheets if s["title"].strip().casefold() in order),
                  key=lambda s: (order[s["title"].strip().casefold()], s["sheetId"]))


def monitor_rows(values, project, today=None):
    if not values:
        return []
    header, _ = table_layout(values, project)
    today = today or datetime.now(ZoneInfo("Europe/Moscow")).date()
    rows = []
    for number, raw in enumerate(values[header:], header + 1):
        cells = [str(value) if value is not None else "" for value in raw] + [""] * 4
        if not cells[1].strip():
            continue
        try:
            day = parse_date(cells[1].strip())
        except ValueError:
            continue
        if day < today:
            continue
        assignment, link = assignment_cell(cells[3])
        status = cells[0].strip()
        hint = ""
        if assignment.strip():
            if not status:
                hint = "Подготовить материалы"
            elif status == PLANNED:
                hint = "Ожидает согласования"
        rows.append({"row": number, "date": day.isoformat(), "date_label": day.strftime("%d.%m.%Y"),
                     "weekday": cells[2], "assignment": assignment, "materials": link,
                     "status": status, "tone": STATUS_CLASS.get(status, "neutral"), "hint": hint})
    return sorted(rows, key=lambda item: (item["date"], item["row"]))


class MonitorService:
    def __init__(self, factory, google_factory=Google, ttl=30):
        self.factory, self.google_factory, self.ttl = factory, google_factory, ttl
        self.cache, self.lock = {}, threading.Lock()

    def load(self, token, selected=None, force=False, project_id=None):
        if not TOKEN_RE.fullmatch(token):
            raise MonitorNotFound()
        connection = google = None
        try:
            connection = self.factory()
            user = connection.store.monitor_user(token)
            if user is None:
                raise MonitorNotFound()
            admins = getattr(connection, "admins", ())
            projects = connection.store.visible_projects(user, admins)
            if project_id is not None and project_id not in projects:
                raise MonitorNotFound()
            project = projects.get(project_id) if project_id is not None else next(iter(projects.values()), None)
            tabs = [{"id": p["id"], "name": p["name"]} for p in projects.values()]
            if project is None:
                return self.empty_data("Нет доступных проектов", "Пока вам не назначены проекты", tabs)
            key = (project["id"], project.get("revision"), project.get("google_connection_id"), project.get("spreadsheet_id"), selected, datetime.now(ZoneInfo("Europe/Moscow")).date())
            with self.lock:
                cached = self.cache.get(key)
            if not force and cached and time.monotonic() - cached[0] < self.ttl:
                return {**cached[1], "projects": tabs, "project_id": project["id"]}
            try:
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
                        "month": sheet["title"].strip(), "rows": rows, "message": "",
                        "updated": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")}
            except MonitorNotFound:
                raise
            except Exception as exc:
                message = str(exc) if isinstance(exc, MonitorUnavailable) else "Не удалось загрузить данные этого проекта. Проверьте подключение Google в боте"
                data = self.empty_data(project["name"], message, tabs, project["id"])
            # Membership, token rotation and project deletion are checked after Google too.
            current_projects = connection.store.visible_projects(user, admins)
            if connection.store.monitor_user(token) != user or project["id"] not in current_projects:
                raise MonitorNotFound()
            tabs = [{"id": p["id"], "name": p["name"]} for p in current_projects.values()]
            if not data.get("message"):
                with self.lock:
                    now = time.monotonic()
                    self.cache = {k: v for k, v in self.cache.items() if now - v[0] < self.ttl}
                    self.cache[key] = (now, data)
            return {**data, "projects": tabs, "project_id": project["id"]}
        except (MonitorNotFound, MonitorUnavailable):
            raise
        except Exception:
            raise MonitorUnavailable("Не удалось загрузить мониторинг. Попробуйте обновить страницу") from None
        finally:
            if google is not None and hasattr(google, "http"):
                google.http.close()
            if connection is not None:
                connection.db.close()
                connection.store.db.close()

    @staticmethod
    def empty_data(name, message, projects, project_id=None):
        return {"name": name, "projects": projects, "project_id": project_id, "sheets": [],
                "sheet_id": None, "month": "", "rows": [], "updated": "", "message": message}


def assignment_html(text):
    """Link plain web URLs without interpreting assignment text as HTML."""
    parts = []
    end = 0
    for match in re.finditer(r"(?<![\w@])(?:https?://|www\.)[^\s<>\"'«»]+", text, re.IGNORECASE):
        url = match.group()
        while url:
            if url[-1] in ".,;:!?…":
                url = url[:-1]
            elif url[-1] in ")]}":
                closer = url[-1]
                opener = {')': '(', ']': '[', '}': '{'}[closer]
                if url.count(closer) <= url.count(opener):
                    break
                url = url[:-1]
            else:
                break
        parts.append(html.escape(text[end:match.start()]))
        href = "https://" + url if url.lower().startswith("www.") else url
        try:
            valid = bool(urlsplit(href).hostname)
        except ValueError:
            valid = False
        if valid:
            parts.append(f'<a class="task-link" href="{html.escape(href, quote=True)}" target="_blank" rel="noreferrer noopener">{html.escape(url)}</a>')
        else:
            parts.append(html.escape(url))
        end = match.start() + len(url)
    parts.append(html.escape(text[end:]))
    return "".join(parts)


def render_monitor(data, path):
    esc = html.escape
    rows = data["rows"]
    body = []
    for row in rows:
        status_label = esc(row["status"] or "Статус не задан", quote=True)
        assignment = assignment_html(row["assignment"]) if row["assignment"].strip() else '<span class="no-task">Задание не задано</span>'
        hint = f'<div class="task-hint">{esc(row["hint"])}</div>' if row["hint"] else ""
        link = (f'<a class="materials" href="{esc(row["materials"], quote=True)}" target="_blank" rel="noreferrer noopener">Материалы '
                '<svg aria-hidden="true" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></a>'
                if row["materials"] else "")
        body.append(f'<tr class="row-{row["tone"]}" aria-label="{status_label}"><td class="date"><time datetime="{row["date"]}">{row["date_label"]}</time></td>'
                    f'<td class="weekday">{esc(row["weekday"])}</td><td class="task"><div class="task-text">{assignment}</div>{hint}{link}</td></tr>')
    if not body:
        body.append(f'<tr><td colspan="3" class="empty-table">{esc(data.get("message") or "В этом месяце пока нет строк с датами")}</td></tr>')
    tabs = []
    for sheet in data["sheets"]:
        active = sheet["sheetId"] == data["sheet_id"]
        url = path + "?" + urlencode({"project": data["project_id"], "month": sheet["sheetId"]})
        tabs.append(f'<a class="month-tab{" active" if active else ""}" href="{esc(url, quote=True)}"'
                    f'{" aria-current=page" if active else ""}>{esc(sheet["title"].strip())}</a>')
    project_tabs = []
    for project in data["projects"]:
        active = project["id"] == data["project_id"]
        url = path + "?" + urlencode({"project": project["id"]})
        project_tabs.append(f'<a class="project-tab{" active" if active else ""}" href="{esc(url, quote=True)}"'
                            f'{" aria-current=page" if active else ""}>{esc(project["name"])}</a>')
    refresh = {}
    if data["project_id"] is not None:
        refresh["project"] = data["project_id"]
    if data["sheet_id"] is not None:
        refresh["month"] = data["sheet_id"]
    refresh["refresh"] = 1
    template = Template((ASSETS / "monitor.html").read_text())
    return template.substitute(title=esc(data["name"]), month=esc(data["month"] or "Мониторинг"),
                               updated=esc(data["updated"] or "Данные не загружены"),
                               assets=ASSET_VERSION, projects="".join(project_tabs) or '<span class="no-projects">Проектов пока нет</span>',
                               rows="".join(body), tabs="".join(tabs), refresh=esc(path + "?" + urlencode(refresh), quote=True))



def render_error(message):
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>BMauto · Мониторинг</title><link rel="stylesheet" href="/monitor-assets/monitor.css?v={ASSET_VERSION}">'
            '<body><main class="error-screen"><div class="brand">BMauto</div><h1>Мониторинг недоступен</h1>'
            f'<p>{html.escape(message)}</p><a class="refresh" href="">Попробовать снова ↻</a></main></body></html>')
