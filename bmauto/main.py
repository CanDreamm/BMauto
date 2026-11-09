import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .core import DEFAULT_COLUMNS, Store, assignment_from_task, task_context
from .calendar import load_tasks
from .google import publish
from .llm import GenAPI
from .members import MemberMenus
from .oauth import GoogleMenus, ProjectGoogle
from .oauth_server import connections_from_env
from .projects import ProjectMenus
from .vk import VK, back_footer, photo_format, photo_identity, photo_size
from .statuses import APPROVED, EXPIRED, PLANNED, SENT


class Bot:
    def __init__(self, config, store, vk, google, llm, allowed, connections=None):
        store.seed_projects(config.get("projects", []))
        store.remove_demo()
        store.ensure_owners(min(allowed) if allowed else None)
        self.project_menus = ProjectMenus(store, vk, allowed, self.open_project, connections)
        self.member_menus = MemberMenus(store, vk, allowed)
        self.google_menus = GoogleMenus(connections, store, vk, allowed)
        self.zone = ZoneInfo("Europe/Moscow")
        self.store, self.vk, self.google, self.llm, self.allowed = store, vk, google, llm, allowed

    def google_for(self, project):
        return self.google.for_project(project) if isinstance(self.google, ProjectGoogle) else self.google

    def open_project(self, user, project):
        if not self.store.can_access(project["id"], user, self.allowed):
            raise ValueError("Нет доступа к проекту")
        session = self.store.session(user)
        for key in ("wizard", "task", "member_input", "member_remove", "dates_error", "delete_confirmation"):
            session.pop(key, None)
        session.update(project=project["id"], view="dates", mode=None, page=0,
                       project_revision=project.get("revision", 1), tasks=[])
        if not project.get("spreadsheet_id"):
            session["dates_error"] = "Укажите Google Таблицу в настройках проекта, чтобы появились даты"
        else:
            try:
                tasks, warnings = load_tasks(self.google_for(project), project, datetime.now(self.zone).date())
                session["tasks"] = tasks
                if warnings:
                    session["dates_error"] = "\n".join(warnings)
            except ValueError as exc:
                session["dates_error"] = str(exc)
            except Exception:
                session["dates_error"] = "Не удалось загрузить даты. Проверьте подключение Google и доступ к таблице в настройках проекта"
        self.store.save_session(user, session)
        self.show_dates(user, session)

    def draft_for(self, session, project):
        task = session["task"]
        identity = {k: v for k, v in task["fields"].items() if k != project.get("link_column", DEFAULT_COLUMNS["link_column"])}
        parts = [project["id"], task["row"], identity]
        if "sheet_id" in task:
            parts.append([task["source_spreadsheet_id"], task["sheet_id"]])
        if project.get("draft_scope"):
            parts.append(project["draft_scope"])
        legacy_key = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        parts[2] = {"date": task["day"], "assignment_D": assignment_from_task(task)}
        key = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        if not self.store.has_draft(key) and self.store.has_draft(legacy_key):
            self.store.save_draft(key, self.store.draft(legacy_key))
        return key, self.store.draft(key)

    def task_display(self, task):
        message = task_context(task)
        if task.get("material_url"):
            message += "\n\nГотовые материалы:\n" + task["material_url"]
        if task.get("status") in {APPROVED, SENT, EXPIRED, PLANNED}:
            message += "\n\nСтатус: " + task["status"]
        return message

    def missing_generation_settings(self, project):
        return [label for key, label in (("concept", "Концепция"), ("prompt", "Промпт")) if not str(project.get(key) or "").strip()]

    def generation_setup_message(self, project, user):
        missing = ", ".join(self.missing_generation_settings(project))
        action = "Заполните их в настройках проекта" if self.store.can_manage(project["id"], user, self.allowed) else "Попросите владельца заполнить их в настройках проекта"
        return f"Для генерации не заполнены: {missing}. {action}"

    def task_buttons(self, session, project):
        _, draft = self.draft_for(session, project)
        navigation = [back_footer({"a": "task_home"})]
        send_button = None
        if (draft["text"].strip() or draft["photos"]) and (draft.get("dirty") or not draft["published"] or session["task"].get("material_url") != f"https://drive.google.com/drive/folders/{draft['folder']}"):
            send_button = ("Отправить в таблицу", {"a": "publish", "_color": "positive"})
        if session.get("mode") == "supplement":
            return [back_footer({"a": "task_home"}, ("Без дополнений", {"a": "no_supplement", "_color": "positive"}))]
        if session.get("mode") == "text":
            return navigation
        if session.get("mode") == "photos":
            remove = [(f"Удалить фото {i}", {"a": "remove_photo", "id": photo["id"]}) for i, photo in enumerate(draft["photos"], 1)]
            rows = [remove[i:i + 3] for i in range(0, len(remove), 3)]
            clear = ("Удалить все фото", {"a": "clear_photos", "_color": "negative"}) if remove else None
            return rows + [back_footer({"a": "task_home"}, clear)]
        buttons = []
        if draft["text"] or draft["photos"]:
            buttons.append(("Посмотреть черновик", {"a": "preview"}))
        buttons.append([("Редактировать текст" if draft["text"] else "Написать текст", {"a": "edit_text"}),
                        ("Сгенерировать заново" if draft["text"] else "Сгенерировать текст", {"a": "generate"})])
        buttons.append((f"Фотографии · {len(draft['photos'])}/9", {"a": "photos"}))
        if not send_button and session["task"].get("material_url") and not draft.get("dirty") and not draft.get("approved") and session["task"].get("status") != APPROVED:
            send_button = ("Согласовано", {"a": "approve", "_color": "positive"})
        return buttons + [back_footer({"a": "project_home"}, send_button)]

    def render(self, user, session=None, notice=None):
        session = self.store.session(user) if session is None else session
        if notice:
            self.vk.send(user, notice)
        project = self.store.projects().get(session.get("project"))
        if project and not self.store.can_access(project["id"], user, self.allowed):
            session = {"last_message": session.get("last_message", -1), "view": "projects"}
            self.store.save_session(user, session)
            project = None
        wizard = session.get("wizard")
        if wizard and ((wizard["kind"] == "create" and user not in self.allowed) or (wizard["kind"] != "create" and not self.store.can_manage(wizard["project"], user, self.allowed))):
            session.pop("wizard", None)
            session["view"] = "home" if project else "projects"
            self.store.save_session(user, session)
        if project and session.get("view") == "project_delete" and session.get("delete_confirmation"):
            if self.store.can_manage(project["id"], user, self.allowed):
                self.project_menus.show_delete(user, project, session["delete_confirmation"])
            else:
                session.pop("delete_confirmation", None)
                self.store.save_session(user, session)
                self.project_menus.show_home(user, project)
            return
        if session.get("view") in {"members", "member_add", "member_remove"} and project:
            if self.store.can_manage(project["id"], user, self.allowed):
                if session.get("member_input"):
                    self.member_menus.ask(user)
                else:
                    self.member_menus.show(user, project, session.get("members_page", 0))
            else:
                self.project_menus.show_home(user, project)
            return
        if project and not self.store.can_manage(project["id"], user, self.allowed) and ("wizard" in session or session.get("view") in {"edit", "create"}):
            session.pop("wizard", None)
            self.store.save_session(user, session)
            self.project_menus.show_home(user, project)
            return
        if project and session.get("view") in {"task", "supplement", "photos", "text", "dates"} and session.get("project_revision") != project.get("revision", 1):
            for key in ("tasks", "task", "mode"):
                session.pop(key, None)
            self.store.save_session(user, session)
            self.project_menus.show_home(user, project)
            return
        if "wizard" in session:
            self.project_menus.ask(user, session["wizard"])
        elif not project or session.get("view") == "projects":
            self.project_menus.show_projects(user)
        elif session.get("view") == "settings":
            self.project_menus.show_settings(user, project, session.get("settings_page", 0))
        elif "task" in session and session.get("view") in {"task", "supplement", "photos", "text"}:
            self.vk.send(user, "Выберите действие для задания", self.task_buttons(session, project))
        elif session.get("view") == "dates" and "tasks" in session:
            self.show_dates(user, session)
        else:
            self.project_menus.show_home(user, project)

    def handle(self, message):
        user, peer = message["from_id"], message["peer_id"]
        if peer != user or user <= 0:
            return
        if not self.store.can_enter(user, self.allowed):
            if self.store.session(user):
                self.vk.send(user, "У вас нет доступных проектов. Обратитесь к владельцу", [])
            return
        session = self.store.session(user)
        has_photos = any(a.get("type") == "photo" for a in message.get("attachments", []))
        if has_photos:
            message = self.vk.full_photo_message(message)
        if message.get("conversation_message_id"):
            event = "conversation:" + str(message["conversation_message_id"])
        elif message.get("id", 0) > 0:
            event = "id:" + str(message["id"])
        else:
            event = "content:" + hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
        if has_photos:
            # A later delivery of the same VK message can contain additional photos.
            event += ":photos:" + hashlib.sha256(json.dumps(message.get("attachments", []), sort_keys=True).encode()).hexdigest()
        if self.store.message_handled(user, event):
            return
        session["last_message"] = max(message.get("id", 0), session.get("last_message", -1))
        self.store.save_session(user, session)
        try:
            if self.dispatch(user, message, session) is not False:
                self.store.mark_message_handled(user, event)
        except ValueError as exc:
            self.render(user, notice=str(exc))
        except Exception:
            self.render(user, notice="Операция не завершена. Черновик сохранён. Проверьте доступы и настройки API, затем повторите действие.")

    def dispatch(self, user, message, session):
        text = message.get("text", "").strip()
        payload = json.loads(message.get("payload") or "{}")
        action = payload.get("a")
        if text.lower() in ("начать", "start", "/start", "проекты") or not session:
            action = "projects"
        elif text.lower() in ("меню", "/menu", "меню проекта"):
            action = "project_home" if session.get("project") else "projects"
        if action not in {"projects", "project_page", "project", "project_new"} and session.get("project") and not self.store.can_access(session["project"], user, self.allowed):
            session = {"last_message": session.get("last_message", -1), "view": "projects"}
            self.store.save_session(user, session)
            raise ValueError("Нет доступа к проекту")
        if action in {"projects", "project_page", "project", "project_new", "project_home", "project_cancel", "settings", "project_edit", "dates"}:
            session.pop("delete_confirmation", None)
            session.pop("member_input", None)
            session.pop("member_remove", None)
        if self.member_menus.handle(user, action, payload, text, session):
            return
        if self.google_menus.handle(user, action, session):
            return
        if self.project_menus.handle(user, action, payload, text, session):
            return
        projects = self.store.visible_projects(user, self.allowed)
        if action == "project":
            if payload.get("p") not in projects:
                raise ValueError("Проект не найден. Откройте список проектов")
            session.clear()
            session.update(project=payload["p"], last_message=message["id"])
            self.store.save_session(user, session)
            self.project_menus.show_home(user, projects[payload["p"]])
            return
        if session.get("project") not in projects:
            self.project_menus.show_projects(user)
            return
        project = projects[session["project"]]
        if action in {"project_home", "dates"}:
            self.project_menus.show_home(user, project)
            return
        if session.get("view") in {"home", "settings", "projects", "create", "edit"}:
            self.render(user, notice="Выберите действие кнопкой текущего меню")
            return
        if session.get("project_revision") != project.get("revision", 1):
            raise ValueError("Настройки проекта изменились. Откройте задания заново через меню проекта")
        if action == "page":
            session["page"] = max(0, int(payload["page"]))
            session.pop("task", None)
            session["view"], session["mode"] = "dates", None
            self.store.save_session(user, session)
            self.show_dates(user, session)
            return
        if action == "task":
            task = session["tasks"][int(payload["i"])]
            session["task"], session["mode"] = task, None
            session["view"] = "task"
            self.store.save_session(user, session)
            self.vk.send(user, self.task_display(task), self.task_buttons(session, project))
            return
        if "task" not in session:
            raise ValueError("Выберите дату кнопкой")
        task = session["task"]
        key, draft = self.draft_for(session, project)
        if action == "task_home":
            session["mode"], session["view"] = None, "task"
            self.store.save_session(user, session)
            self.vk.send(user, self.task_display(task), self.task_buttons(session, project))
            return
        buttons = self.task_buttons(session, project)
        if action is not None and action not in {p["a"] for item in buttons for _, p in (item if isinstance(item, list) else [item])}:
            self.render(user, notice="Это действие сейчас недоступно. Выберите кнопку текущего меню")
            return
        if action == "preview":
            photos = [(self.store.root / photo["path"], photo.get("mime", "image/jpeg")) for photo in draft["photos"]]
            if photos:
                self.vk.send(user, "Готовлю предпросмотр черновика…")
            self.vk.send_draft(user, draft["text"] or "Текст пока не создан", photos, self.task_buttons(session, project))
            return
        if action == "approve":
            if draft.get("dirty"):
                raise ValueError("Сначала сохраните изменения в таблицу")
            self.google_for(project).set_status(project, task, APPROVED, task.get("material_url"))
            draft["approved"] = True
            self.store.save_draft(key, draft)
            session["task"]["status"] = APPROVED
            self.store.save_session(user, session)
            self.vk.send(user, "Согласовано. Статус в колонке A отмечен зелёным.", self.task_buttons(session, project))
            return
        if action == "publish":
            session["mode"], session["view"] = None, "task"
            self.store.save_session(user, session)
            self.vk.send(user, "Сохраняю материалы на Google Диск…")
            url = publish(self.google_for(project), self.store, key, project, task)
            session["task"]["material_url"] = url
            session["task"]["status"] = EXPIRED if task["day"] < datetime.now(self.zone).date().isoformat() else SENT
            self.store.save_session(user, session)
            self.vk.send(user, "Готово. Ссылка записана под описанием в D:\n" + url, self.task_buttons(session, project))
            return
        if action == "edit_text":
            session["mode"], session["view"] = "text", "text"
            self.store.save_session(user, session)
            self.vk.send(user, "Отправьте новый текст целиком. Он заменит текст черновика.\n\n" + draft["text"], self.task_buttons(session, project))
            return
        if session.get("mode") == "text" and not action:
            if not text:
                raise ValueError("Отправьте текст публикации")
            draft["text"], draft["dirty"] = text, True
            self.store.save_draft(key, draft)
            session["mode"], session["view"] = None, "task"
            self.store.save_session(user, session)
            self.vk.send(user, "Текст черновика обновлён. Для обновления папки нажмите кнопку сохранения.", self.task_buttons(session, project))
            return
        if action in {"remove_photo", "clear_photos"}:
            draft["photos"] = [photo for photo in draft["photos"] if action != "clear_photos" and photo["id"] != payload.get("id")]
            draft["dirty"] = True
            self.store.save_draft(key, draft)
            self.vk.send(user, f"В черновике {len(draft['photos'])}/9 фото. Можно прикрепить новые.", self.task_buttons(session, project))
            return
        if action == "generate":
            if self.missing_generation_settings(project):
                self.vk.send(user, self.generation_setup_message(project, user), self.task_buttons(session, project))
                return
            session["mode"] = "supplement"
            session["view"] = "supplement"
            self.store.save_session(user, session)
            self.vk.send(user, "Напишите дополнение к заданию или нажмите «Без дополнений». Повторная генерация заменит текст черновика.", self.task_buttons(session, project))
            return
        if action == "photos":
            session["mode"] = "photos"
            session["view"] = "photos"
            self.store.save_session(user, session)
            self.vk.send(user, f"Прикрепляйте фотографии как фото ВК. Сохранено {len(draft['photos'])}/9. Фото сохраняются автоматически. Нажмите «Назад», чтобы вернуться к заданию.", self.task_buttons(session, project))
            return
        if session.get("mode") == "supplement" and (not action or action == "no_supplement"):
            if self.missing_generation_settings(project):
                raise ValueError(self.generation_setup_message(project, user))
            if not text and action != "no_supplement":
                raise ValueError("Напишите дополнение текстом")
            description = task_context(task)
            self.vk.send(user, "Генерирую текст…")
            draft["text"] = self.llm.generate(project, description, "" if action == "no_supplement" else text)
            draft["dirty"] = True
            self.store.save_draft(key, draft)
            session["mode"] = None
            session["view"] = "task"
            self.store.save_session(user, session)
            self.vk.send(user, draft["text"], self.task_buttons(session, project))
            return
        if session.get("mode") == "photos" or (not action and session.get("view") == "task" and any(a.get("type") == "photo" for a in message.get("attachments", []))):
            received = [a["photo"] for a in message.get("attachments", []) if a["type"] == "photo"]
            known = {p["id"] for p in draft["photos"]}
            photos = {photo_identity(p): p for p in received}
            photos = {k: p for k, p in photos.items() if k not in known}
            legacy_temporary = any("0" in p["id"].split("_") for p in draft["photos"])
            if len(draft["photos"]) + len(photos) > 9 and not legacy_temporary:
                raise ValueError("Не более 9 фотографий. Эта партия не сохранена")
            if not photos:
                raise ValueError("Прикрепите новые фотографии как фото ВК")
            directory = self.store.root / key
            directory.mkdir(exist_ok=True)
            failed = 0
            # Recover batches previously collapsed under IDs such as 1_0 / 0_0.
            temporary_hashes = set()
            for saved in draft["photos"]:
                if saved["id"].startswith("source_") or "0" in saved["id"].split("_"):
                    path = self.store.root / saved["path"]
                    if path.is_file():
                        temporary_hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
            previous_temporary_hashes = set(temporary_hashes)
            pending = []
            for photo_id, photo in photos.items():
                try:
                    size = photo_size(photo)
                    content = self.vk.download_photo(size["url"])
                    extension, mime = photo_format(content)
                except Exception:
                    failed += 1
                    continue
                fingerprint = hashlib.sha256(content).hexdigest()
                if fingerprint in previous_temporary_hashes or (photo_id.startswith("source_") and fingerprint in temporary_hashes):
                    continue
                pending.append((photo_id, content, extension, mime))
                if photo_id.startswith("source_"):
                    temporary_hashes.add(fingerprint)
            if len(draft["photos"]) + len(pending) > 9:
                raise ValueError("Не более 9 фотографий. Эта партия не сохранена")
            for photo_id, content, extension, mime in pending:
                filename = directory / f"{photo_id}.{extension}"
                filename.write_bytes(content)
                draft["photos"].append({"id": photo_id, "path": str(filename.relative_to(self.store.root)), "extension": extension, "mime": mime})
                draft["dirty"] = True
                self.store.save_draft(key, draft)
            notice = f"Получено от ВК: {len(received)} фото. Сохранено {len(draft['photos'])}/9 фото"
            if failed:
                notice += f". Не удалось скачать: {failed}. Пришлите их повторно; сохранённые фото не дублируются."
            if message.get("_photo_lookup_failed"):
                notice += ". Не удалось проверить полный список вложений ВК. Пришлите сообщение повторно."
            self.vk.send(user, notice, self.task_buttons(session, project))
            return False if failed or message.get("_photo_lookup_failed") else None
        raise ValueError("Выберите действие кнопкой меню")

    def show_dates(self, user, session):
        project = self.store.projects()[session["project"]]
        tasks = session.get("tasks", [])
        page = min(max(0, session.get("page", 0)), max(0, (len(tasks) - 1) // 12))
        session["page"] = page
        self.store.save_session(user, session)
        date_buttons = []
        days = [t["day"] for t in tasks]
        for i, task in enumerate(tasks):
            if page * 12 <= i < (page + 1) * 12:
                label = f"{datetime.fromisoformat(task['day']):%d.%m.%Y} — {task['weekday']}"
                if task.get("status") == APPROVED:
                    label = "🟢 " + label
                elif task.get("status") == SENT or task.get("material_url"):
                    label = "🟡 " + label
                if days.count(task["day"]) > 1:
                    label += f" · {task['row']}"
                date_buttons.append((label, {"a": "task", "i": i}))
        rows = [date_buttons[i:i + 2] for i in range(0, len(date_buttons), 2)]
        pagination = []
        if page:
            pagination.append(("Назад", {"a": "page", "page": page - 1}))
        if (page + 1) * 12 < len(tasks):
            pagination.append(("Далее", {"a": "page", "page": page + 1}))
        if pagination:
            rows.append(pagination)
        rows.extend([[("Настройки", {"a": "settings"})], [("Все проекты", {"a": "projects"})]])
        status = session.get("dates_error") or ("Выберите дату" if tasks else "Будущих дат нет")
        self.vk.send(user, f"Проект «{project['name']}»\n{status}", rows)


def main():
    load_dotenv()
    required = [k for k in ("VK_TOKEN", "VK_GROUP_ID", "VK_ALLOWED_USERS") if not os.getenv(k)]
    if required:
        raise SystemExit("Заполните переменные: " + ", ".join(required))
    config_path = Path(os.getenv("APP_CONFIG") or "config.json")
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    vk = VK(os.environ["VK_TOKEN"], int(os.environ["VK_GROUP_ID"]))
    store = Store(os.getenv("APP_DATA", "data"))
    store.seed_projects(config.get("projects", []))
    connections = connections_from_env(store)
    bot = Bot(config, store, vk, ProjectGoogle(connections), GenAPI(),
              {int(x.strip()) for x in os.environ["VK_ALLOWED_USERS"].split(",") if x.strip()}, connections)
    vk.run(bot.handle)


if __name__ == "__main__":
    main()
