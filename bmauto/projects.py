import re
import secrets
import uuid
from urllib.parse import urlsplit
from .core import DEFAULT_COLUMNS
from .vk import back_footer
from .monitor import monitor_url, project_monitor_url


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
            raise ValueError("Нет доступа к настройкам этого проекта")
        return self.store.projects()[project_id]

    def can_edit(self, user, project_id, field):
        if field not in FIELD_MAP or field == "name":
            return False
        return self.store.can_access(project_id, user, self.admins)

    def editable(self, user, project_id, field):
        if not self.can_edit(user, project_id, field):
            raise ValueError("Нет доступа к настройкам этого проекта")
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
        page = min(max(0, page), max(0, (len(projects) - 1) // 6))
        buttons = [(p["name"], {"a": "project", "p": p["id"]}) for p in projects[page * 6:(page + 1) * 6]]
        pagination = []
        if page:
            pagination.append(("Назад", {"a": "project_page", "page": page - 1}))
        if (page + 1) * 6 < len(projects):
            pagination.append(("Далее", {"a": "project_page", "page": page + 1}))
        if pagination:
            buttons.append(pagination)
        buttons.append(("Мой Postmypost", {"a": "pmp_personal", "_color": "primary"}))
        buttons.append(("Мой мониторинг", {"a": "monitor", "_color": "primary"}))
        if user in self.admins:
            buttons.append(("Создать проект", {"a": "project_new"}))
        self.vk.send(user, "Выберите проект" if projects else "Нет доступных проектов", buttons)

    def show_settings(self, user, project, page=0):
        if not self.store.can_access(project["id"], user, self.admins):
            raise ValueError("Нет доступа к проекту")
        self.view(user, "settings")
        connected = self.connections is not None and self.connections.connected(project)
        buttons = [("Сменить Google-аккаунт" if connected else "Подключить Google", {"a": "google_connect"})]
        buttons.extend((label, {"a": "project_edit", "field": key})
                       for key, label, _, _ in sorted(FIELDS, key=lambda field: field[0] != "spreadsheet_id"))
        buttons.append(("Участники", {"a": "members"}))
        buttons.append(("Соцсети · Postmypost", {"a": "pmp", "_color": "primary"}))
        buttons.append(("Мониторинг проекта", {"a": "project_monitor", "_color": "primary"}))
        buttons.append(back_footer({"a": "project_home"}, ("Удалить проект", {"a": "project_delete", "_color": "negative"})))
        message = f"Настройки проекта «{project['name']}»\nВыберите параметр"
        self.vk.send(user, message, buttons)

    def show_monitor(self, user):
        if not self.store.can_enter(user, self.admins):
            raise ValueError("Нет доступных проектов")
        self.view(user, "monitor")
        self.vk.send(user, f"Ваш мониторинг\n\n{monitor_url(self.store, user, self.connections, self.admins)}\n\nПо этой личной ссылке доступны только ваши проекты. Не передавайте её посторонним.", [
            ("Открыть мониторинг", {"a": "monitor_open", "_color": "positive"}),
            back_footer({"a": "projects"}, ("Сменить ссылку", {"a": "monitor_rotate", "_color": "negative"}))])

    def show_project_monitor(self, user, project):
        self.managed(user, project["id"])
        url = project_monitor_url(self.store, project["id"], user, self.connections, self.admins)
        self.view(user, "project_monitor")
        self.vk.send(user, f"Мониторинг проекта «{project['name']}»\n\n{url}\n\nПо этой секретной ссылке доступен только этот проект, без редактирования. Её можно передать тем, кому нужен просмотр проекта.", [
            ("Открыть мониторинг", {"a": "project_monitor_open", "_color": "positive"}),
            back_footer({"a": "settings"}, ("Сменить ссылку", {"a": "project_monitor_rotate", "_color": "negative"}))])

    def show_project_monitor_rotation(self, user, project, confirmation):
        self.managed(user, project["id"])
        self.vk.send(user, f"Сменить ссылку мониторинга проекта «{project['name']}»? Прежний адрес перестанет работать для всех, кому его передали. Личные ссылки участников сохранятся.", [back_footer(
            {"a": "project_monitor"}, ("Да, сменить", {"a": "project_monitor_rotate_confirm", "token": confirmation["token"], "_color": "negative"}))])

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
            self.editable(user, wizard["project"], wizard["field"])
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
        if action in {"project_monitor", "project_monitor_open", "project_monitor_rotate", "project_monitor_rotate_confirm"}:
            project = self.managed(user, session.get("project"))
            for key in ("wizard", "member_input", "member_remove", "delete_confirmation", "monitor_confirmation"):
                session.pop(key, None)
            session["mode"] = None
            if action == "project_monitor_open":
                if session.get("view") != "project_monitor":
                    raise ValueError("Откройте мониторинг проекта в его настройках")
                self.vk.open_callback_link(user, project_monitor_url(self.store, project["id"], user, self.connections, self.admins))
            elif action == "project_monitor_rotate":
                confirmation = {"token": secrets.token_urlsafe(18), "project": project["id"], "user": user}
                session.update(view="project_monitor_rotate", project_monitor_confirmation=confirmation)
                self.store.save_session(user, session)
                self.show_project_monitor_rotation(user, project, confirmation)
            elif action == "project_monitor_rotate_confirm":
                confirmation = session.get("project_monitor_confirmation", {})
                supplied = str(payload.get("token", "")).encode()
                expected = confirmation.get("token", "-").encode()
                if (session.get("view") != "project_monitor_rotate" or confirmation.get("project") != project["id"]
                        or confirmation.get("user") != user or not secrets.compare_digest(supplied, expected)):
                    raise ValueError("Подтверждение устарело. Запросите смену ссылки заново")
                self.store.project_monitor_token(project["id"], user, self.admins, rotate=True)
                session.pop("project_monitor_confirmation", None)
                self.store.save_session(user, session)
                self.show_project_monitor(user, project)
            else:
                session.pop("project_monitor_confirmation", None)
                self.store.save_session(user, session)
                self.show_project_monitor(user, project)
            return True
        if action in {"monitor", "monitor_open", "monitor_rotate", "monitor_rotate_confirm"}:
            if not self.store.can_enter(user, self.admins):
                raise ValueError("Нет доступных проектов")
            for key in ("project", "task", "tasks", "wizard", "member_input", "member_remove", "delete_confirmation"):
                session.pop(key, None)
            session["mode"] = None
            if action == "monitor_open":
                if session.get("view") != "monitor":
                    raise ValueError("Откройте «Мой мониторинг» в списке проектов")
                self.vk.open_callback_link(user, monitor_url(self.store, user, self.connections, self.admins))
            elif action == "monitor_rotate":
                session["monitor_confirmation"] = {"token": secrets.token_urlsafe(18), "user": user}
                session["view"] = "monitor_rotate"
                self.store.save_session(user, session)
                self.show_monitor_rotation(user, session["monitor_confirmation"])
            elif action == "monitor_rotate_confirm":
                confirmation = session.get("monitor_confirmation", {})
                supplied = str(payload.get("token", "")).encode()
                expected = confirmation.get("token", "-").encode()
                if session.get("view") != "monitor_rotate" or confirmation.get("user") != user or not secrets.compare_digest(supplied, expected):
                    raise ValueError("Подтверждение устарело. Запросите смену ссылки заново")
                self.store.monitor_token(user, self.admins, rotate=True)
                session.pop("monitor_confirmation", None)
                self.store.save_session(user, session)
                self.show_monitor(user)
            else:
                session.pop("monitor_confirmation", None)
                self.store.save_session(user, session)
                self.show_monitor(user)
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
            project = self.editable(user, session.get("project"), payload["field"])
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
                project = self.editable(user, wizard["project"], key)
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

    def show_monitor_rotation(self, user, confirmation):
        if not self.store.can_enter(user, self.admins):
            raise ValueError("Нет доступных проектов")
        self.vk.send(user, "Сменить личную ссылку мониторинга? Прежний адрес перестанет работать. Проекты и данные сохранятся.", [back_footer(
            {"a": "monitor"}, ("Да, сменить", {"a": "monitor_rotate_confirm", "token": confirmation["token"], "_color": "negative"}))])
