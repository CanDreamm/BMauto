import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass
class Task:
    row: int
    day: date
    weekday: str
    fields: dict[str, str]

    def description(self):
        return "\n".join(f"{key}: {value}" for key, value in self.fields.items() if value.strip())


def parse_date(value):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Неподдерживаемая дата: {value}")


def tasks_from_rows(rows, project, today):
    if not rows:
        return []
    headers = [str(x).strip() for x in rows[0]]
    required = [project[k] for k in ("date_column", "weekday_column", "link_column")]
    if len(headers) != len(set(headers)) or "" in headers:
        raise ValueError("Заголовки таблицы должны быть непустыми и уникальными")
    if any(name not in headers for name in required):
        raise ValueError("В таблице нет обязательных столбцов: " + ", ".join(required))
    result = []
    for number, row in enumerate(rows[1:], 2):
        fields = dict(zip(headers, [str(x) for x in row] + [""] * (len(headers) - len(row))))
        value = fields[project["date_column"]].strip()
        if not value:
            continue
        try:
            day = parse_date(value)
        except ValueError as exc:
            raise ValueError(f"Строка {number}: {exc}") from exc
        if day > today:
            result.append(Task(number, day, fields[project["weekday_column"]], fields))
    return sorted(result, key=lambda t: (t.day, t.row))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (user INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS drafts (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS project_members (project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner', 'member')), PRIMARY KEY (project_id, user_id))")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS project_owner ON project_members(project_id) WHERE role='owner'")
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

    def draft(self, key):
        row = self.db.execute("SELECT body FROM drafts WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {"text": "", "photos": [], "folder": None, "uploads": {}, "published": False}

    def save_draft(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts VALUES (?,?)", (key, json.dumps(value)))


def column_letter(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result
