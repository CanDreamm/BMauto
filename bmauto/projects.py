import re
import secrets
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar import AUTO


# Settings are edited independently after creating a project by name.
FIELDS = [
    ("concept", "Концепция", "Отправьте концепцию проекта и описание аудитории", None),
    ("prompt", "Промпт", "Отправьте промпт для генерации текста публикаций", None),
    ("spreadsheet_id", "Google Таблица", "Отправьте ссылку на Google Таблицу или её ID", None),
    ("worksheet", "Выбор вкладок", "Нажмите «Авто» для вкладок по месяцам или укажите фиксированную вкладку", AUTO),
    ("drive_parent_id", "Папка Google Диска", "Отправьте ссылку на папку проекта в Google Диске или её ID", None),
    ("date_column", "Столбец даты", "Как называется столбец даты?", "Дата"),
    ("weekday_column", "Столбец дня недели", "Как называется столбец дня недели?", "День недели"),
    ("link_column", "Столбец ссылки", "Как называется столбец для ссылки на материалы?", "Ссылка"),
    ("public_read", "Доступ по ссылке", "Открывать созданные папки всем по ссылке? Это откроет доступ к фото и тексту", None),
    ("timezone", "Часовой пояс", "Введите часовой пояс, например Europe/Moscow. По нему определяются будущие даты", "Europe/Moscow"),
    ("month_rollover_days", "Когда брать следующий месяц", "За сколько дней до конца месяца показывать даты следующего? Введите число от 0 до 31. Если будущих дат текущего месяца нет, следующий берётся сразу", 7),
]
FIELD_MAP = {f[0]: f for f in FIELDS}
FIELD_MAP["name"] = ("name", "Название", "Введите название проекта", None)


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
    if key == "month_rollover_days":
        if isinstance(value, bool):
            raise ValueError("Введите целое число от 0 до 31")
        try:
            result = int(str(value).strip())
        except (ValueError, TypeError) as exc:
            raise ValueError("Введите целое число от 0 до 31") from exc
        if not 0 <= result <= 31:
            raise ValueError("Введите целое число от 0 до 31")
        return result
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
    def __init__(self, store, vk, admins, open_project, connections=None):
        self.store, self.vk, self.admins = store, vk, admins
        self.open_project = open_project
        self.connections = connections

    def managed(self, user, project_id):
        if not self.store.can_manage(project_id, user, self.admins):
            raise ValueError("Настройки меняет владелец проекта или администратор")
        return self.store.projects()[project_id]

    def view(self, user, name):
        session = self.store.session(user)
        session["view"] = name
        self.store.save_session(user, session)

    def show_home(self, user, project):
        self.open_project(user, project)

    def show_projects(self, user, page=0):
        self.view(user, "projects")
        projects = list(self.store.visible_projects(user, self.admins).values())
        page = min(max(0, page), max(0, (len(projects) - 1) // 7))
        buttons = [(p["name"], {"a": "project", "p": p["id"]}) for p in projects[page * 7:(page + 1) * 7]]
        if page:
            buttons.append(("Назад", {"a": "project_page", "page": page - 1}))
        if (page + 1) * 7 < len(projects):
            buttons.append(("Далее", {"a": "project_page", "page": page + 1}))
        if user in self.admins:
            buttons.append(("Создать проект", {"a": "project_new"}))
        self.vk.send(user, "Выберите проект" if projects else "Нет доступных проектов", buttons)

    def show_settings(self, user, project, page=0):
        if not self.store.can_access(project["id"], user, self.admins):
            raise ValueError("Нет доступа к проекту")
        self.view(user, "settings")
        page = min(max(0, page), (len(FIELDS) - 1) // 6)
        details = [f"Настройки проекта «{project['name']}»"]
        for key, label, _, default in FIELDS:
            value = project.get(key, default)
            if key == "public_read":
                value = "Всем по ссылке" if value else "Только существующие права"
            details.append(f"{label}: {value if value is not None else 'Не задано'}")
        buttons = []
        if self.store.can_manage(project["id"], user, self.admins):
            connected = self.connections is not None and self.connections.connected(project)
            buttons = [("Сменить Google-аккаунт" if connected else "Подключить Google", {"a": "google_connect"})]
            buttons.append([("Назад", {"a": "project_home"}),
                            ("Удалить проект", {"a": "project_delete", "_color": "negative"})])
            buttons.extend((label, {"a": "project_edit", "field": key}) for key, label, _, _ in FIELDS[page * 6:(page + 1) * 6])
            if page:
                buttons.append(("Предыдущие поля", {"a": "settings", "page": page - 1}))
            if (page + 1) * 6 < len(FIELDS):
                buttons.append(("Следующие поля", {"a": "settings", "page": page + 1}))
            buttons.append(("Участники", {"a": "members"}))
        else:
            details.append("Настройки доступны для просмотра. Изменения вносит владелец проекта")
            buttons.append(("Назад", {"a": "project_home"}))
        message = (f"Настройки проекта «{project['name']}»\nВыберите параметр"
                   if self.store.can_manage(project["id"], user, self.admins) else "\n\n".join(details))
        self.vk.send(user, message, buttons)

    def ask(self, user, wizard):
        if wizard["kind"] != "create" and wizard.get("field") == "name":
            session = self.store.session(user)
            session.pop("wizard", None)
            self.store.save_session(user, session)
            self.show_settings(user, self.managed(user, wizard["project"]))
            return
        if wizard["kind"] == "create":
            if user not in self.admins:
                raise ValueError("Создавать проекты может только администратор")
        else:
            self.managed(user, wizard["project"])
        self.view(user, "create" if wizard["kind"] == "create" else "edit")
        key, label, prompt, default = FIELD_MAP["name"] if wizard["kind"] == "create" else FIELD_MAP[wizard["field"]]
        if wizard["kind"] != "create":
            current = self.store.projects()[wizard["project"]].get(key, default)
            if key == "public_read":
                current = "Всем по ссылке" if current else "Только существующие права"
            prompt = f"{label}\nТекущее значение: {current if current is not None else 'Не задано'}\n\n{prompt}"
        buttons = []
        if key == "public_read":
            buttons = [("Да, всем по ссылке", {"a": "project_value", "value": True}),
                       ("Нет, ограниченный доступ", {"a": "project_value", "value": False})]
        elif default is not None:
            buttons = [(f"Оставить: {default}", {"a": "project_default"})]
        if wizard["kind"] == "create":
            buttons.append(("В меню", {"a": "projects"}))
        else:
            buttons.extend([("Назад к настройкам", {"a": "settings", "page": wizard.get("page", 0)}),
                            ("К датам проекта", {"a": "project_home"})])
        self.vk.send(user, prompt, buttons)

    def input_buttons(self, wizard):
        key = "name" if wizard["kind"] == "create" else wizard["field"]
        actions = {"project_value"} if key == "public_read" else set()
        if FIELD_MAP[key][3] is not None:
            actions.add("project_default")
        return actions

    def handle(self, user, action, payload, text, session):
        """Return True when the message belongs to project administration."""
        if action == "project_delete":
            project = self.managed(user, session.get("project"))
            session.pop("wizard", None)
            token = secrets.token_urlsafe(16)
            session.update(view="project_delete", mode=None, delete_confirmation={
                "project": project["id"], "revision": project.get("revision", 1), "token": token})
            self.store.save_session(user, session)
            self.show_delete(user, project, session["delete_confirmation"])
            return True
        if action == "project_delete_confirm":
            confirmation = session.get("delete_confirmation", {})
            if (session.get("view") != "project_delete" or not confirmation or
                payload.get("p") != session.get("project") or payload.get("p") != confirmation.get("project") or
                not isinstance(payload.get("token"), str) or not secrets.compare_digest(payload["token"].encode(), confirmation["token"].encode())):
                raise ValueError("Подтверждение устарело. Откройте настройки проекта")
            try:
                self.store.delete_project(session["project"], user, self.admins, confirmation["revision"])
            except ValueError:
                session.pop("delete_confirmation", None)
                session["view"] = "settings"
                self.store.save_session(user, session)
                raise
            self.vk.send(user, "Проект удалён из бота. Файлы на Google Диске и данные таблицы сохранены")
            self.show_projects(user)
            return True
        if action in {"projects", "project_page"}:
            session.pop("wizard", None)
            session["mode"] = None
            self.store.save_session(user, session)
            self.show_projects(user, int(payload.get("page", 0)))
            return True
        if action == "project_new":
            if user not in self.admins:
                raise ValueError("Создавать проекты может только администратор")
            session["wizard"] = {"kind": "create"}
        elif action == "project_cancel":
            session.pop("wizard", None)
            self.store.save_session(user, session)
            self.show_projects(user)
            return True
        elif action == "settings":
            session.pop("wizard", None)
            session["mode"] = None
            if not self.store.can_access(session.get("project"), user, self.admins):
                raise ValueError("Нет доступа к проекту")
            project = self.store.projects()[session["project"]]
            session["settings_revision"] = project.get("revision", 1)
            session["settings_page"] = int(payload.get("page", 0))
            self.store.save_session(user, session)
            self.show_settings(user, project, int(payload.get("page", 0)))
            return True
        elif action == "project_edit":
            if payload.get("field") not in FIELD_MAP or payload.get("field") == "name":
                raise ValueError("Неизвестная настройка")
            project = self.managed(user, session.get("project"))
            session["wizard"] = {"kind": "edit", "field": payload["field"], "project": project["id"],
                                 "revision": project.get("revision", 1), "page": list(FIELD_MAP).index(payload["field"]) // 6}
        elif "wizard" in session and (action is None or action in {"project_value", "project_default"}):
            wizard = session["wizard"]
            if wizard["kind"] != "create" and wizard.get("field") == "name":
                session.pop("wizard", None)
                self.store.save_session(user, session)
                raise ValueError("Смена названия проекта отключена")
            if action is not None and action not in self.input_buttons(wizard):
                self.ask(user, wizard)
                return True
            key, _, _, default = FIELD_MAP["name"] if wizard["kind"] == "create" else FIELD_MAP[wizard["field"]]
            value = default if action == "project_default" else payload.get("value") if action == "project_value" else text
            value = validate_field(key, value)
            if wizard["kind"] == "create":
                if user not in self.admins:
                    raise ValueError("Создавать проекты может только администратор")
                values = {k: default for k, _, _, default in FIELDS if default is not None}
                values.update(id=uuid.uuid4().hex, name=value, public_read=True)
                project = self.store.save_project(values, owner=user)
            else:
                project = self.managed(user, wizard["project"])
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
            self.vk.send(user, "Проект создан" if wizard["kind"] == "create" else "Настройки проекта сохранены")
            if wizard["kind"] == "create":
                self.show_home(user, project)
            else:
                session["settings_page"] = wizard.get("page", 0)
                self.store.save_session(user, session)
                self.show_settings(user, project, session["settings_page"])
            return True
        else:
            return False
        session["mode"] = None
        self.store.save_session(user, session)
        self.ask(user, session["wizard"])
        return True

    def show_delete(self, user, project, confirmation):
        self.managed(user, project["id"])
        self.vk.send(user, f"Удалить проект «{project['name']}» из бота? Участники потеряют доступ. Файлы на Google Диске и данные таблицы останутся.", [
            ("Да, удалить проект", {"a": "project_delete_confirm", "p": project["id"], "token": confirmation["token"], "_color": "negative"}),
            ("Отмена", {"a": "settings"})])
