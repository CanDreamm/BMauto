import re
import secrets
import uuid
from urllib.parse import urlsplit
from .core import DEFAULT_COLUMNS
from .vk import back_footer
from .monitor import monitor_url


# Settings are edited independently after creating a project by name.
FIELDS = [
    ("concept", "Концепция", "Отправьте концепцию проекта и описание аудитории", None),
    ("prompt", "Промпт", "Отправьте промпт для генерации текста публикаций", None),
    ("spreadsheet_id", "Google Таблица", "Отправьте ссылку на Google Таблицу или её ID", None),
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
    if key not in FIELD_MAP:
        raise ValueError("Эта настройка больше не используется")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Значение не может быть пустым")
    value = value.strip()
    if key == "name" and len(value) > 40:
        raise ValueError("Название должно быть не длиннее 40 символов")
    if key == "spreadsheet_id":
        return google_id(value, key)
    return value


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
        details = [f"Настройки проекта «{project['name']}»"]
        for key, label, _, default in FIELDS:
            value = project.get(key, default)
            details.append(f"{label}: {value if value is not None else 'Не задано'}")
        connected = self.connections is not None and self.connections.connected(project)
        buttons = [("Сменить Google-аккаунт" if connected else "Подключить Google", {"a": "google_connect"})]
        if self.store.can_manage(project["id"], user, self.admins):
            buttons.extend((label, {"a": "project_edit", "field": key})
                           for key, label, _, _ in sorted(FIELDS, key=lambda field: field[0] != "spreadsheet_id"))
            buttons.append(("Участники", {"a": "members"}))
            buttons.append(("Мониторинг", {"a": "monitor"}))
            buttons.append(back_footer({"a": "project_home"}, ("Удалить проект", {"a": "project_delete", "_color": "negative"})))
        else:
            details.append("Вы можете подключить свой Google для этого проекта. Остальные настройки меняет владелец проекта")
            buttons.append(("Мониторинг", {"a": "monitor"}))
            buttons.append(back_footer({"a": "project_home"}))
        message = (f"Настройки проекта «{project['name']}»\nВыберите параметр"
                   if self.store.can_manage(project["id"], user, self.admins) else "\n\n".join(details))
        self.vk.send(user, message, buttons)

    def show_monitor(self, user, project):
        if not self.store.can_access(project["id"], user, self.admins):
            raise ValueError("Нет доступа к проекту")
        self.view(user, "monitor")
        action = ("Сменить ссылку", {"a": "monitor_rotate", "_color": "negative"}) if self.store.can_manage(project["id"], user, self.admins) else None
        self.vk.send(user, f"Мониторинг проекта «{project['name']}»\n\n{monitor_url(self.store, project, self.connections)}\n\nТолько просмотр. Страница доступна каждому, у кого есть эта секретная ссылка.", [
            ("Открыть мониторинг", {"a": "monitor_open", "_color": "positive"}), back_footer({"a": "settings"}, action)])

    def ask(self, user, wizard):
        if wizard["kind"] != "create" and (wizard.get("field") == "name" or wizard.get("field") not in FIELD_MAP):
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
            prompt = f"{label}\nТекущее значение: {current if current is not None else 'Не задано'}\n\n{prompt}"
        buttons = []
        if default is not None:
            buttons = [(f"Оставить: {default}", {"a": "project_default"})]
        if wizard["kind"] == "create":
            buttons.append(("Назад", {"a": "projects"}))
        else:
            buttons.append(back_footer({"a": "settings", "page": wizard.get("page", 0)}))
        self.vk.send(user, prompt, buttons)

    def input_buttons(self, wizard):
        key = "name" if wizard["kind"] == "create" else wizard["field"]
        actions = set()
        if FIELD_MAP[key][3] is not None:
            actions.add("project_default")
        return actions

    def handle(self, user, action, payload, text, session):
        """Return True when the message belongs to project administration."""
        if action in {"monitor", "monitor_open", "monitor_rotate", "monitor_rotate_confirm"}:
            project = self.store.projects().get(session.get("project"))
            if not project or not self.store.can_access(project["id"], user, self.admins):
                raise ValueError("Нет доступа к проекту")
            for key in ("wizard", "member_input", "member_remove", "delete_confirmation"):
                session.pop(key, None)
            session["mode"] = None
            if action == "monitor_open":
                if session.get("view") != "monitor":
                    raise ValueError("Откройте мониторинг через настройки проекта")
                self.vk.open_callback_link(user, monitor_url(self.store, project, self.connections))
            elif action == "monitor_rotate":
                self.managed(user, project["id"])
                session["monitor_confirmation"] = {"token": secrets.token_urlsafe(18), "project": project["id"]}
                session["view"] = "monitor_rotate"
                self.store.save_session(user, session)
                self.show_monitor_rotation(user, project, session["monitor_confirmation"])
            elif action == "monitor_rotate_confirm":
                self.managed(user, project["id"])
                confirmation = session.get("monitor_confirmation", {})
                if session.get("view") != "monitor_rotate" or confirmation.get("project") != project["id"] or not secrets.compare_digest(str(payload.get("token", "")), confirmation.get("token", "-")):
                    raise ValueError("Подтверждение устарело. Запросите смену ссылки заново")
                self.store.monitor_token(project["id"], rotate=True)
                session.pop("monitor_confirmation", None)
                self.store.save_session(user, session)
                self.show_monitor(user, project)
            else:
                session.pop("monitor_confirmation", None)
                self.store.save_session(user, session)
                self.show_monitor(user, project)
            return True
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
            session["settings_page"] = 0
            self.store.save_session(user, session)
            self.show_settings(user, project, int(payload.get("page", 0)))
            return True
        elif action == "project_edit":
            if payload.get("field") not in FIELD_MAP or payload.get("field") == "name":
                raise ValueError("Неизвестная настройка")
            project = self.managed(user, session.get("project"))
            session["wizard"] = {"kind": "edit", "field": payload["field"], "project": project["id"],
                                 "revision": project.get("revision", 1), "page": 0}
        elif "wizard" in session and (action is None or action in {"project_value", "project_default"}):
            wizard = session["wizard"]
            if wizard["kind"] != "create" and (wizard.get("field") == "name" or wizard.get("field") not in FIELD_MAP):
                session.pop("wizard", None)
                self.store.save_session(user, session)
                raise ValueError("Эта настройка больше не используется")
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
                values.update(DEFAULT_COLUMNS, id=uuid.uuid4().hex, name=value)
                project = self.store.save_project(values, owner=user)
            else:
                project = self.managed(user, wizard["project"])
                updated = {**project, key: value}
                if key == "spreadsheet_id" and project.get(key) != value:
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
        self.vk.send(user, f"Удалить проект «{project['name']}» из бота? Участники потеряют доступ. Файлы на Google Диске и данные таблицы останутся.", [back_footer(
            {"a": "settings"}, ("Да, удалить проект", {"a": "project_delete_confirm", "p": project["id"], "token": confirmation["token"], "_color": "negative"}))])

    def show_monitor_rotation(self, user, project, confirmation):
        self.managed(user, project["id"])
        self.vk.send(user, "Сменить секретную ссылку мониторинга? Прежний адрес перестанет работать. Данные проекта сохранятся.", [back_footer(
            {"a": "monitor"}, ("Да, сменить", {"a": "monitor_rotate_confirm", "token": confirmation["token"], "_color": "negative"}))])
