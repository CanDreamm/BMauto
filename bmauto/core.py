import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_COLUMNS = {"date_column": "Дата", "weekday_column": "День недели", "link_column": "Ссылка"}
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
    values = list(task["fields"].values())
    return assignment_cell(values[3] if len(values) > 3 else "")[0]


def task_context(task):
    return f"Дата: {task['day']}\nДень недели: {task['weekday']}\nЗадание: {assignment_from_task(task)}"


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

    def description(self):
        return task_context({"day": self.day.isoformat(), "weekday": self.weekday, "fields": self.fields, "assignment": self.assignment})


def parse_date(value):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Неподдерживаемая дата: {value}")


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


def table_headers(rows, project=None):
    return table_layout(rows, project)[1]


def tasks_from_rows(rows, project, today):
    project = {**DEFAULT_COLUMNS, **project}
    if not rows:
        return []
    header_row, headers = table_layout(rows, project)
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
        if day > today:
            if len(headers) < 4:
                raise ValueError("Задание должно находиться в столбце D")
            description, material_url = assignment_cell(row[3] if len(row) > 3 else "")
            if not description.strip():
                continue
            result.append(Task(number, day, fields[project["weekday_column"]], fields, header_row, description, material_url,
                               str(row[0]) if row else ""))
    return sorted(result, key=lambda t: (t.day, t.row))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        (self.root / "state.sqlite3").chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (user INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS drafts (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS monitor_links (project_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE)")
        self.db.execute("CREATE TABLE IF NOT EXISTS vk_chat_messages (peer INTEGER NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(peer,key))")
        self.db.execute("CREATE TABLE IF NOT EXISTS handled_messages (user INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY(user,event))")
        self.db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS project_members (project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner', 'member')), PRIMARY KEY (project_id, user_id))")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS project_owner ON project_members(project_id) WHERE role='owner'")
        self.db.execute("CREATE TABLE IF NOT EXISTS drive_locations (account TEXT NOT NULL, parent TEXT NOT NULL, location TEXT NOT NULL, file_id TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(account,parent,location))")
        if "ready" not in {row[1] for row in self.db.execute("PRAGMA table_info(drive_locations)")}:
            self.db.execute("ALTER TABLE drive_locations ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

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

    def monitor_token(self, project_id, rotate=False):
        if project_id not in self.projects():
            raise ValueError("Проект не найден")
        with self.db:
            if rotate:
                self.db.execute("DELETE FROM monitor_links WHERE project_id=?", (project_id,))
            self.db.execute("INSERT OR IGNORE INTO monitor_links VALUES (?,?)", (project_id, secrets.token_urlsafe(32)))
            return self.db.execute("SELECT token FROM monitor_links WHERE project_id=?", (project_id,)).fetchone()[0]

    def monitor_project(self, token):
        row = self.db.execute("SELECT project_id FROM monitor_links WHERE token=?", (token,)).fetchone()
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

    def members(self, project_id):
        return dict(self.db.execute("SELECT user_id,role FROM project_members WHERE project_id=? ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END,user_id", (project_id,)))

    def can_access(self, project_id, user, admins):
        return project_id in self.projects() and (user in admins or user in self.members(project_id))

    def can_manage(self, project_id, user, admins):
        return project_id in self.projects() and (user in admins or self.members(project_id).get(user) == 'owner')

    def visible_projects(self, user, admins):
        return {key: p for key, p in self.projects().items() if self.can_access(key, user, admins)}

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
