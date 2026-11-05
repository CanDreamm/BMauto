import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
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
        self.db.commit()

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
