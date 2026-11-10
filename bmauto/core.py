import json
import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone, time as clock_time
from pathlib import Path

DEFAULT_COLUMNS = {"date_column": "Дата", "weekday_column": "День недели", "time_column": "Время", "link_column": "Ссылка"}
HEADER_ROW = 2
MATERIALS_MARKER = "Готовые материалы:"
MATERIALS_FOOTER = re.compile(r"(?s)^(?:(.*)\n\n)?Готовые материалы:\n(https://drive\.google\.com/drive/folders/[A-Za-z0-9_-]+)\s*$")


def assignment_cell(value):
    value = str(value) if value is not None else ""
    match = MATERIALS_FOOTER.fullmatch(value)
    return (match[1] or "", match[2]) if match else (value, None)


def assignment_from_task(task):
    if task.get("assignment") is not None:
        return task["assignment"]
    headers = list(task["fields"])
    values = list(task["fields"].values())
    index = assignment_index(headers)
    return assignment_cell(values[index] if len(values) > index else "")[0]


def task_context(task):
    clock = task.get("time", task.get("fields", {}).get("Время", ""))
    clock_line = f"\nВремя: {clock}" if clock else ""
    return f"Дата: {task['day']}{clock_line}\nДень недели: {task['weekday']}\nЗадание: {assignment_from_task(task)}"


def append_materials(value, url):
    description, existing = assignment_cell(value)
    if existing:
        if existing != url:
            raise ValueError("У задания уже есть другая ссылка; перезапись запрещена")
        return value
    return f"{description}\n\n{MATERIALS_MARKER}\n{url}" if description else f"{MATERIALS_MARKER}\n{url}"


@dataclass
class Task:
    row: int
    day: date
    weekday: str
    fields: dict[str, str]
    header_row: int = HEADER_ROW
    assignment: str | None = None
    material_url: str | None = None
    status: str = ""
    time: str = ""

    def description(self):
        return task_context({"day": self.day.isoformat(), "weekday": self.weekday, "fields": self.fields, "assignment": self.assignment, "time": self.time})


def parse_date(value):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Неподдерживаемая дата: {value}")


def parse_time(value):
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?", str(value or "").strip())
    if not match:
        raise ValueError("Укажите время в формате ЧЧ:ММ")
    return clock_time(int(match[1]), int(match[2]), int(match[3] or 0))


def time_sort_key(value):
    try:
        clock = parse_time(value)
        return clock.hour, clock.minute, clock.second
    except ValueError:
        return 24, 0, 0


def legacy_draft_keys(project, task):
    identity = {k: v for k, v in task['fields'].items() if k != project.get('link_column', DEFAULT_COLUMNS['link_column'])}
    parts = [project['id'], task['row'], identity]
    if 'sheet_id' in task:
        parts.append([task['source_spreadsheet_id'], task['sheet_id']])
    if project.get('draft_scope'):
        parts.append(project['draft_scope'])
    legacy = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    parts[2] = {'date': task['day'], 'assignment_D': assignment_from_task(task)}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest(), legacy


def table_layout(rows, project=None):
    project = {**DEFAULT_COLUMNS, **(project or {})}
    required = {project["date_column"], project["weekday_column"]}
    for number in (2, 3):
        if len(rows) < number:
            continue
        headers = [str(x).strip() if x is not None else "" for x in rows[number - 1]]
        while headers and not headers[-1]:
            headers.pop()
        if not required.issubset(headers):
            continue
        if len(headers) != len(set(headers)) or "" in headers:
            raise ValueError("Заголовки таблицы должны быть непустыми и уникальными")
        return number, headers
    raise ValueError("Не найдены заголовки «Дата» и «День недели» во второй или третьей строке")


def assignment_index(headers, project=None):
    project = {**DEFAULT_COLUMNS, **(project or {})}
    time_column = project["time_column"]
    if time_column in headers:
        if headers.index(time_column) != headers.index(project["date_column"]) + 1:
            raise ValueError("Колонка «Время» должна находиться сразу после даты")
        index = 4
    else:
        index = 3
    return index


def schedule_field_names(project):
    project = {**DEFAULT_COLUMNS, **project}
    return tuple(project[key] for key in ("date_column", "time_column", "weekday_column"))


def table_headers(rows, project=None):
    return table_layout(rows, project)[1]


def tasks_from_rows(rows, project, today):
    project = {**DEFAULT_COLUMNS, **project}
    if not rows:
        return []
    header_row, headers = table_layout(rows, project)
    index = assignment_index(headers, project)
    result = []
    for number, row in enumerate(rows[header_row:], header_row + 1):
        fields = dict(zip(headers, [str(x) for x in row] + [""] * (len(headers) - len(row))))
        value = fields[project["date_column"]].strip()
        if not value:
            continue
        try:
            day = parse_date(value)
        except ValueError as exc:
            raise ValueError(f"Строка {number}: {exc}") from exc
        if day > today or (day == today and project["time_column"] in headers):
            if len(headers) <= index:
                raise ValueError("Не найдена колонка задания после дня недели")
            description, material_url = assignment_cell(row[index] if len(row) > index else "")
            if not description.strip():
                continue
            result.append(Task(number, day, fields[project["weekday_column"]], fields, header_row, description, material_url,
                               str(row[0]) if row else "", fields.get(project["time_column"], "").strip()))
    return sorted(result, key=lambda t: (t.day, time_sort_key(t.time), t.row))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        (self.root / "state.sqlite3").chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (user INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS drafts (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS draft_tasks (key TEXT PRIMARY KEY, project TEXT NOT NULL, book TEXT NOT NULL, sheet TEXT NOT NULL, scope TEXT NOT NULL, row INTEGER NOT NULL, day TEXT NOT NULL, assignment TEXT NOT NULL, folder TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS draft_tasks_scope ON draft_tasks(project,book,scope)")
        self.db.execute("CREATE TABLE IF NOT EXISTS pmp_credentials (user_id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS pmp_connections (project_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS pmp_jobs (key TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS monitor_project_links (project_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE)")
        self.db.execute("CREATE TABLE IF NOT EXISTS monitor_members (user_id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE)")
        self.db.execute("CREATE TABLE IF NOT EXISTS vk_chat_messages (peer INTEGER NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(peer,key))")
        self.db.execute("CREATE TABLE IF NOT EXISTS handled_messages (user INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY(user,event))")
        self.db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS project_members (project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner', 'member')), PRIMARY KEY (project_id, user_id))")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS project_owner ON project_members(project_id) WHERE role='owner'")
        self.db.execute("CREATE TABLE IF NOT EXISTS drive_locations (account TEXT NOT NULL, parent TEXT NOT NULL, location TEXT NOT NULL, file_id TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,parent,location))")
        if "ready" not in {row[1] for row in self.db.execute("PRAGMA table_info(drive_locations)")}:
            self.db.execute("ALTER TABLE drive_locations ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
        self.db.commit()
        self.migrate_pmp_credentials()

    @staticmethod
    def is_demo(project):
        return (project.get("id") == "example" and project.get("name") == "Пример проекта"
                and project.get("spreadsheet_id") == "ID_ТАБЛИЦЫ_ИЗ_ССЫЛКИ"
                and project.get("drive_parent_id") == "ID_ПАПКИ_ПРОЕКТА"
                and project.get("concept") == "Концепция проекта и описание аудитории"
                and project.get("prompt") == "Напиши текст публикации по заданию. Не выдумывай факты."
                and project.get("revision", 1) == 1)

    def remove_demo(self):
        with self.db:
            for key, project in self.projects().items():
                if self.is_demo(project):
                    self.db.execute("DELETE FROM projects WHERE id=?", (key,))
                    self.db.execute("DELETE FROM project_members WHERE project_id=?", (key,))

    def seed_projects(self, projects):
        # Legacy config is an initial import only; never overwrite bot edits.
        with self.db:
            for project in projects:
                if self.is_demo(project):
                    continue
                project = {**project, "revision": 1}
                self.db.execute("INSERT OR IGNORE INTO projects VALUES (?,?)", (project["id"], json.dumps(project)))

    def projects(self):
        return {key: project for key, body in self.db.execute("SELECT id, body FROM projects ORDER BY rowid")
                if not (project := json.loads(body)).get("deleted_at")}

    def pmp_credential(self, user):
        row = self.db.execute("SELECT body FROM pmp_credentials WHERE user_id=?", (user,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_pmp_credential(self, user, token, admins, expected_revision):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not isinstance(user, int) or user <= 0 or not self.can_enter(user, admins):
                raise ValueError("Нет доступа к боту")
            current = self.pmp_credential(user) or {"revision": 0}
            if current["revision"] != expected_revision:
                raise ValueError("Личный токен изменился. Откройте подключение заново")
            updated = {"token": token, "revision": current["revision"] + 1}
            self.db.execute("INSERT OR REPLACE INTO pmp_credentials VALUES (?,?)", (user, json.dumps(updated)))
        return updated

    def migrate_pmp_credentials(self):
        rows = [(ident, json.loads(body)) for ident, body in self.db.execute("SELECT project_id,body FROM pmp_connections")]
        if not any("token" in body for _, body in rows):
            return
        backup = self.root / (".pmp-migration-" + secrets.token_hex(8) + ".sqlite3")
        backup.touch(mode=0o600, exist_ok=False)
        target = sqlite3.connect(backup)
        try:
            self.db.backup(target)
        finally:
            target.close()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            owners = dict(self.db.execute("SELECT project_id,user_id FROM project_members WHERE role='owner'"))
            rows = [(ident, json.loads(body)) for ident, body in self.db.execute("SELECT project_id,body FROM pmp_connections")]
            tokens = {}
            for ident, config in rows:
                owner = config.get("credential_owner", owners.get(ident))
                # Never turn an unowned project secret into somebody's personal key.
                if "token" in config and config.get("credential_owner") is not None:
                    tokens.setdefault(owner, set()).add(config["token"])
            for owner, values in tokens.items():
                # Conflicting old project tokens require one explicit personal connection.
                if len(values) == 1 and not self.pmp_credential(owner):
                    self.db.execute("INSERT OR IGNORE INTO pmp_credentials VALUES (?,?)", (owner, json.dumps({"token": next(iter(values)), "revision": 1})))
            for ident, config in rows:
                owner = config.get("credential_owner", owners.get(ident))
                if "token" not in config or owner is None:
                    continue
                config.pop("token")
                config["credential_owner"] = owner
                config["accounts"] = [{**item, "remote_project": item.get("remote_project", config.get("remote_project"))} for item in config.get("accounts", [])]
                self.db.execute("UPDATE pmp_connections SET body=? WHERE project_id=?", (json.dumps(config), ident))

    def pmp_connection(self, project_id):
        row = self.db.execute("SELECT body FROM pmp_connections WHERE project_id=?", (project_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_pmp_connection(self, project_id, actor, admins, changes, expected_revision, credential_revision=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.can_manage(project_id, actor, admins):
                raise ValueError("Подключение Postmypost меняет владелец или администратор")
            if "token" in changes:
                raise ValueError("Токен сохраняется только в личном подключении Postmypost")
            current = self.pmp_connection(project_id) or {"revision": 0}
            if current["revision"] != expected_revision:
                raise ValueError("Подключение Postmypost изменилось. Откройте настройки заново")
            updated = {**current, **changes, "revision": current["revision"] + 1}
            personal = self.pmp_credential(actor)
            if updated.get("accounts") and (updated.get("credential_owner") != actor or not personal):
                raise ValueError("Для подключения соцсетей используется ваш личный токен")
            if credential_revision is not None and (not personal or personal["revision"] != credential_revision):
                raise ValueError("Личный токен изменился. Выберите соцсети заново")
            self.db.execute("INSERT OR REPLACE INTO pmp_connections VALUES (?,?)", (project_id, json.dumps(updated)))
        return updated

    def pmp_job(self, key):
        row = self.db.execute("SELECT body FROM pmp_jobs WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def claim_pmp_job(self, key, project_id, before, job, revision, credential_revision=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            config = self.pmp_connection(project_id)
            if project_id not in self.projects() or not config or config["revision"] != revision:
                raise ValueError("Настройки Postmypost изменились. Откройте задание заново")
            credential = self.pmp_credential(config.get("credential_owner"))
            if credential_revision is not None and (not credential or credential["revision"] != credential_revision):
                raise ValueError("Личный токен изменился. Откройте задание заново")
            if self.pmp_job(key) != before:
                raise ValueError("Публикация уже обрабатывается. Откройте задание и проверьте результат")
            self.db.execute("INSERT OR REPLACE INTO pmp_jobs VALUES (?,?,?)", (key, project_id, json.dumps(job)))

    def save_pmp_job(self, key, project_id, job):
        with self.db:
            self.db.execute("UPDATE pmp_jobs SET body=? WHERE key=? AND project_id=?", (json.dumps(job), key, project_id))

    def monitor_token(self, user, admins=(), rotate=False):
        if not isinstance(user, int) or user <= 0 or not self.can_enter(user, admins):
            raise ValueError("Нет доступных проектов")
        with self.db:
            if rotate:
                self.db.execute("DELETE FROM monitor_members WHERE user_id=?", (user,))
            self.db.execute("INSERT OR IGNORE INTO monitor_members VALUES (?,?)", (user, secrets.token_urlsafe(32)))
            return self.db.execute("SELECT token FROM monitor_members WHERE user_id=?", (user,)).fetchone()[0]

    def monitor_user(self, token):
        row = self.db.execute("SELECT user_id FROM monitor_members WHERE token=?", (token,)).fetchone()
        return row[0] if row else None

    def project_monitor_token(self, project_id, actor, admins=(), rotate=False):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.can_manage(project_id, actor, admins):
                raise ValueError("Ссылкой проекта управляет владелец или администратор")
            if rotate:
                self.db.execute("DELETE FROM monitor_project_links WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM pmp_connections WHERE project_id=?", (project_id,))
            self.db.execute("INSERT OR IGNORE INTO monitor_project_links VALUES (?,?)", (project_id, secrets.token_urlsafe(32)))
            return self.db.execute("SELECT token FROM monitor_project_links WHERE project_id=?", (project_id,)).fetchone()[0]

    def monitor_project(self, token):
        row = self.db.execute("SELECT project_id FROM monitor_project_links WHERE token=?", (token,)).fetchone()
        return self.projects().get(row[0]) if row else None

    def save_project(self, project, expected_revision=None, owner=None):
        with self.db:
            row = self.db.execute("SELECT body FROM projects WHERE id=?", (project["id"],)).fetchone()
            if row and json.loads(row[0]).get("deleted_at"):
                raise ValueError("Проект удалён")
            current_revision = json.loads(row[0]).get("revision", 1) if row else None
            if current_revision != expected_revision:
                raise ValueError("Настройки проекта изменились. Откройте настройки заново")
            updated = {**project, "revision": (current_revision or 0) + 1}
            self.db.execute("INSERT OR REPLACE INTO projects VALUES (?,?)", (project["id"], json.dumps(updated)))
            if current_revision is None and owner is not None:
                self.db.execute("INSERT INTO project_members VALUES (?,?,'owner')", (project["id"], owner))
        return updated

    def delete_project(self, project_id, actor, admins, expected_revision):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.can_manage(project_id, actor, admins):
                raise ValueError("Удалить проект может только владелец или администратор")
            project = self.projects()[project_id]
            if project.get("revision", 1) != expected_revision:
                raise ValueError("Проект изменился. Подтвердите удаление заново")
            archived = {**project, "deleted_at": datetime.now(timezone.utc).isoformat(),
                        "deleted_by": actor, "revision": expected_revision + 1}
            # Keep a tombstone so legacy config cannot recreate the deleted project.
            self.db.execute("UPDATE projects SET body=? WHERE id=?", (json.dumps(archived), project_id))
            self.db.execute("DELETE FROM project_members WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM monitor_project_links WHERE project_id=?", (project_id,))
            self.db.execute("DELETE FROM pmp_connections WHERE project_id=?", (project_id,))
            for user, body in self.db.execute("SELECT user,body FROM sessions").fetchall():
                session = json.loads(body)
                if session.get("project") == project_id:
                    clean = {"last_message": session.get("last_message", -1), "view": "projects"}
                    self.db.execute("UPDATE sessions SET body=? WHERE user=?", (json.dumps(clean), user))
        return project

    def ensure_owners(self, default_owner):
        if default_owner is None:
            return
        with self.db:
            for key in self.projects():
                row = self.db.execute("SELECT 1 FROM project_members WHERE project_id=? AND role='owner'", (key,)).fetchone()
                if not row:
                    self.db.execute("INSERT INTO project_members VALUES (?,?,'owner') ON CONFLICT(project_id,user_id) DO UPDATE SET role='owner'", (key, default_owner))
        self.migrate_pmp_credentials()

    def members(self, project_id):
        return dict(self.db.execute("SELECT user_id,role FROM project_members WHERE project_id=? ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END,user_id", (project_id,)))

    def can_access(self, project_id, user, admins):
        return project_id in self.projects() and (user in admins or user in self.members(project_id))

    def can_manage(self, project_id, user, admins):
        return project_id in self.projects() and (user in admins or self.members(project_id).get(user) == 'owner')

    def visible_projects(self, user, admins):
        projects = self.projects()
        if user in admins:
            return projects
        assigned = {row[0] for row in self.db.execute("SELECT project_id FROM project_members WHERE user_id=?", (user,))}
        return {key: project for key, project in projects.items() if key in assigned}

    def can_enter(self, user, admins):
        return user in admins or self.db.execute("SELECT 1 FROM project_members WHERE user_id=? LIMIT 1", (user,)).fetchone() is not None

    def add_member(self, project_id, actor, user, admins):
        if not self.can_manage(project_id, actor, admins):
            raise ValueError("Управлять участниками может только владелец проекта или администратор")
        if type(user) is not int or not 0 < user < 2**63:
            raise ValueError("Неверный числовой ID пользователя ВК")
        if user in admins or user in self.members(project_id):
            raise ValueError("У этого пользователя уже есть доступ к проекту")
        with self.db:
            self.db.execute("INSERT INTO project_members VALUES (?,?,'member')", (project_id, user))

    def remove_member(self, project_id, actor, user, admins):
        if not self.can_manage(project_id, actor, admins):
            raise ValueError("Управлять участниками может только владелец проекта или администратор")
        if self.members(project_id).get(user) == 'owner' or user in admins:
            raise ValueError("Нельзя удалить владельца или администратора")
        with self.db:
            self.db.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (project_id, user))
            session = self.session(user)
            if session.get("project") == project_id:
                self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (user, json.dumps({"last_message": session.get("last_message", -1), "view": "projects"})))

    def session(self, user):
        row = self.db.execute("SELECT body FROM sessions WHERE user=?", (user,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_session(self, user, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (user, json.dumps(value)))

    def chat_messages(self, peer):
        return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM vk_chat_messages WHERE peer=?", (peer,))]

    def track_chat_message(self, peer, message):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO vk_chat_messages VALUES (?,?,?)", (peer, message["key"], json.dumps(message)))

    def forget_chat_message(self, peer, key):
        with self.db:
            self.db.execute("DELETE FROM vk_chat_messages WHERE peer=? AND key=?", (peer, key))

    @staticmethod
    def draft_source(project, task):
        return (project['id'], task.get('source_spreadsheet_id', project.get('spreadsheet_id', '')),
                str(task.get('sheet_id', task.get('worksheet', project.get('worksheet', '')))), project.get('draft_scope', ''))

    def remember_draft_task(self, key, project, task):
        source = self.draft_source(project, task)
        folder = self.draft(key).get('folder')
        with self.db:
            self.db.execute("INSERT INTO draft_tasks VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                            "sheet=excluded.sheet,row=excluded.row,day=excluded.day,assignment=excluded.assignment,folder=excluded.folder",
                            (key, *source, task['row'], task['day'], assignment_from_task(task), folder))

    def index_legacy_drafts(self):
        projects = self.projects()
        for (body,) in self.db.execute('SELECT body FROM sessions').fetchall():
            session = json.loads(body)
            project = projects.get(session.get('project'))
            if not project:
                continue
            tasks = ([session['task']] if session.get('task') else []) + session.get('tasks', [])
            for task in tasks:
                if not isinstance(task, dict) or not {'fields', 'row', 'day'}.issubset(task) or not isinstance(task['fields'], dict):
                    continue
                if 'sheet_id' in task and 'source_spreadsheet_id' not in task:
                    continue
                modern, legacy = legacy_draft_keys(project, task)
                key = modern if self.has_draft(modern) else legacy
                if self.has_draft(key) and not self.db.execute('SELECT 1 FROM draft_tasks WHERE key=?', (key,)).fetchone():
                    self.remember_draft_task(key, project, task)

    def task_draft(self, project, task):
        modern, legacy = legacy_draft_keys(project, task)
        project_id, book, sheet, scope = self.draft_source(project, task)
        url = task.get('material_url')
        folder = url.rsplit('/', 1)[-1] if url else None
        known = self.db.execute('SELECT key,sheet,row,day,assignment,folder FROM draft_tasks WHERE project=? AND book=? AND scope=?',
                                (project_id, book, scope)).fetchall()
        key = None
        if folder:
            matches = [r[0] for r in known if r[5] == folder and self.has_draft(r[0])]
            if len(matches) > 1:
                raise ValueError('У материалов несколько черновиков. Нужна проверка привязки задания')
            if matches:
                key = matches[0]
        if key is None:
            if not self.has_draft(modern) and self.has_draft(legacy):
                self.save_draft(modern, self.draft(legacy))
            if self.has_draft(modern):
                key = modern
        if key is None:
            candidates = []
            for candidate, old_sheet, row, day, assignment, old_folder in known:
                if old_sheet != sheet or row != task['row'] or not self.has_draft(candidate):
                    continue
                draft = self.draft(candidate)
                if draft.get('published') or (folder and old_folder and folder != old_folder):
                    continue
                if day == task['day'] or assignment == assignment_from_task(task):
                    candidates.append(candidate)
            if len(candidates) == 1:
                key = candidates[0]
        key = key or modern
        self.remember_draft_task(key, project, task)
        return key, self.draft(key)

    def draft(self, key):
        row = self.db.execute("SELECT body FROM drafts WHERE key=?", (key,)).fetchone()
        return {"text": "", "photos": [], "folder": None, "uploads": {}, "published": False, "dirty": False,
                **(json.loads(row[0]) if row else {})}

    def message_handled(self, user, event):
        return self.db.execute("SELECT 1 FROM handled_messages WHERE user=? AND event=?", (user, event)).fetchone() is not None

    def mark_message_handled(self, user, event):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO handled_messages VALUES (?,?)", (user, event))

    def save_draft(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts VALUES (?,?)", (key, json.dumps(value)))
            self.db.execute("UPDATE draft_tasks SET folder=? WHERE key=?", (value.get('folder'), key))

    def has_draft(self, key):
        return self.db.execute("SELECT 1 FROM drafts WHERE key=?", (key,)).fetchone() is not None

    def drive_location(self, account, parent, location):
        row = self.db.execute("SELECT file_id,ready FROM drive_locations WHERE account=? AND parent=? AND location=?", (account, parent, location)).fetchone()
        return {"id": row[0], "ready": bool(row[1])} if row else None

    def save_drive_location(self, account, parent, location, file_id, ready=False):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drive_locations VALUES (?,?,?,?,?)", (account, parent, location, file_id, int(ready)))


def column_letter(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result
