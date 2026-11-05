import re
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Ordered fields are shared by creation and editing so every setting is reachable.
FIELDS = [
    ("name", "Название", "Введите название проекта", None),
    ("concept", "Концепция", "Отправьте концепцию проекта и описание аудитории", None),
    ("prompt", "Промпт", "Отправьте промпт для генерации текста публикаций", None),
    ("spreadsheet_id", "Google Таблица", "Отправьте ссылку на Google Таблицу или её ID", None),
    ("worksheet", "Вкладка таблицы", "Введите точное название вкладки таблицы", "Лист1"),
    ("drive_parent_id", "Папка Google Диска", "Отправьте ссылку на папку проекта в Google Диске или её ID", None),
    ("date_column", "Столбец даты", "Как называется столбец даты?", "Дата"),
    ("weekday_column", "Столбец дня недели", "Как называется столбец дня недели?", "День недели"),
    ("link_column", "Столбец ссылки", "Как называется столбец для ссылки на материалы?", "Ссылка"),
    ("public_read", "Доступ по ссылке", "Открывать созданные папки всем по ссылке? Это откроет доступ к фото и тексту", None),
    ("timezone", "Часовой пояс", "Введите часовой пояс, например Europe/Moscow. По нему определяются будущие даты", "Europe/Moscow"),
]
FIELD_MAP = {f[0]: f for f in FIELDS}


def google_id(value, kind):
    if value.startswith(("https://", "http://")):
        parsed = urlsplit(value)
        host, path = ("docs.google.com", r"/spreadsheets/d/([\w-]+)") if kind == "spreadsheet_id" else ("drive.google.com", r"/(?:drive/(?:u/\d+/)?)?folders/([\w-]+)")
        match = re.search(path, parsed.path) if parsed.hostname == host and parsed.scheme == "https" else None
        if not match:
            raise ValueError("Нужна HTTPS-ссылка на Google Таблицу" if kind == "spreadsheet_id" else "Нужна HTTPS-ссылка на папку Google Диска")
        return match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Неверный ID Google. Отправьте ссылку целиком")
    return value


def validate_field(key, value):
    if key == "public_read":
        if type(value) is not bool:
            raise ValueError("Выберите «Да» или «Нет» кнопкой")
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Значение не может быть пустым")
    value = value.strip()
    if key == "name" and len(value) > 40:
        raise ValueError("Название должно быть не длиннее 40 символов")
    if key in {"spreadsheet_id", "drive_parent_id"}:
        return google_id(value, key)
    if key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Неизвестный часовой пояс. Например: Europe/Moscow") from exc
    return value


def validate_columns(project):
    columns = [project.get(k) for k in ("date_column", "weekday_column", "link_column") if project.get(k)]
    if len(columns) != len(set(columns)):
        raise ValueError("Столбцы даты, дня недели и ссылки должны иметь разные названия")


class ProjectMenus:
    def __init__(self, store, vk):
        self.store, self.vk = store, vk

    def show_projects(self, user, page=0):
        projects = list(self.store.projects().values())
        page = min(max(0, page), max(0, (len(projects) - 1) // 7))
        buttons = [(p["name"], {"a": "project", "p": p["id"]}) for p in projects[page * 7:(page + 1) * 7]]
        if page:
            buttons.append(("Назад", {"a": "project_page", "page": page - 1}))
        if (page + 1) * 7 < len(projects):
            buttons.append(("Далее", {"a": "project_page", "page": page + 1}))
        buttons.append(("Создать проект", {"a": "project_new"}))
        self.vk.send(user, "Выберите проект" if projects else "Пока нет проектов. Создайте первый", buttons)

    def show_settings(self, user, project, page=0):
        page = min(max(0, page), (len(FIELDS) - 1) // 7)
        details = [f"Настройки проекта «{project['name']}»"]
        for key, label, _, default in FIELDS:
            value = project.get(key, default)
            if key == "public_read":
                value = "Всем по ссылке" if value else "Только существующие права"
            details.append(f"{label}: {value if value is not None else 'Не задано'}")
        buttons = [(label, {"a": "project_edit", "field": key}) for key, label, _, _ in FIELDS[page * 7:(page + 1) * 7]]
        if page:
            buttons.append(("Назад", {"a": "settings", "page": page - 1}))
        if (page + 1) * 7 < len(FIELDS):
            buttons.append(("Далее", {"a": "settings", "page": page + 1}))
        buttons.append(("Открыть задания", {"a": "dates"}))
        self.vk.send(user, "\n\n".join(details), buttons)

    def ask(self, user, wizard):
        key, _, prompt, default = FIELDS[wizard["step"]] if wizard["kind"] == "create" else FIELD_MAP[wizard["field"]]
        buttons = []
        if key == "public_read":
            buttons = [("Да, всем по ссылке", {"a": "project_value", "value": True}),
                       ("Нет, ограниченный доступ", {"a": "project_value", "value": False})]
        elif default is not None:
            buttons = [(f"Оставить: {default}", {"a": "project_default"})]
        buttons.append(("Отмена", {"a": "project_cancel"}))
        self.vk.send(user, prompt, buttons)

    def handle(self, user, action, payload, text, session):
        """Return True when the message belongs to project administration."""
        if action in {"projects", "project_page"}:
            session.pop("wizard", None)
            session["mode"] = None
            self.store.save_session(user, session)
            self.show_projects(user, int(payload.get("page", 0)))
            return True
        if action == "project_new":
            session["wizard"] = {"kind": "create", "step": 0, "values": {"id": uuid.uuid4().hex}}
        elif action == "project_cancel":
            session.pop("wizard", None)
            self.store.save_session(user, session)
            self.show_projects(user)
            return True
        elif action == "settings":
            session.pop("wizard", None)
            session["mode"] = None
            project = self.store.projects().get(session.get("project"))
            if not project:
                raise ValueError("Сначала выберите проект")
            session["settings_revision"] = project.get("revision", 1)
            self.store.save_session(user, session)
            self.show_settings(user, project, int(payload.get("page", 0)))
            return True
        elif action == "project_edit":
            if payload.get("field") not in FIELD_MAP:
                raise ValueError("Неизвестная настройка")
            project = self.store.projects().get(session.get("project"))
            if not project:
                raise ValueError("Сначала выберите проект")
            session["wizard"] = {"kind": "edit", "field": payload["field"], "project": project["id"],
                                 "revision": project.get("revision", 1)}
        elif "wizard" in session and (action is None or action in {"project_value", "project_default"}):
            wizard = session["wizard"]
            key, _, _, default = FIELDS[wizard["step"]] if wizard["kind"] == "create" else FIELD_MAP[wizard["field"]]
            value = default if action == "project_default" else payload.get("value") if action == "project_value" else text
            value = validate_field(key, value)
            if wizard["kind"] == "create":
                values = {**wizard["values"], key: value}
                validate_columns(values)
                wizard["values"] = values
                wizard["step"] += 1
                if wizard["step"] < len(FIELDS):
                    self.store.save_session(user, session)
                    self.ask(user, wizard)
                    return True
                project = self.store.save_project(values)
            else:
                project = self.store.projects().get(wizard["project"])
                if not project:
                    raise ValueError("Проект не найден")
                updated = {**project, key: value}
                validate_columns(updated)
                if key in {"spreadsheet_id", "worksheet", "drive_parent_id", "public_read"} and project.get(key) != value:
                    # A changed destination must not reuse a draft/folder from the old one.
                    updated["draft_scope"] = uuid.uuid4().hex
                project = self.store.save_project(updated, wizard["revision"])
            session.pop("wizard")
            session["project"] = project["id"]
            for stale in ("task", "tasks", "mode", "project_revision"):
                session.pop(stale, None)
            session["settings_revision"] = project["revision"]
            self.store.save_session(user, session)
            self.vk.send(user, "Настройки проекта сохранены")
            self.show_settings(user, project)
            return True
        else:
            return False
        session["mode"] = None
        self.store.save_session(user, session)
        self.ask(user, session["wizard"])
        return True
