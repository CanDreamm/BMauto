import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .core import Store, tasks_from_rows
from .google import Google, publish
from .llm import GenAPI
from .projects import ProjectMenus
from .vk import VK


MENU = [("Сгенерировать текст", {"a": "generate"}), ("Загрузить фотографии", {"a": "photos"}),
        ("Посмотреть черновик", {"a": "preview"}), ("Отправить в таблицу", {"a": "publish"}),
        ("Выбрать дату", {"a": "dates"}), ("Настройки проекта", {"a": "settings"}), ("Проекты", {"a": "projects"})]


class Bot:
    def __init__(self, config, store, vk, google, llm, allowed):
        store.seed_projects([{**p, "timezone": p.get("timezone", config.get("timezone", "Europe/Moscow"))}
                             for p in config.get("projects", [])])
        self.project_menus = ProjectMenus(store, vk)
        self.zone = ZoneInfo(config.get("timezone", "Europe/Moscow"))
        self.store, self.vk, self.google, self.llm, self.allowed = store, vk, google, llm, allowed

    def handle(self, message):
        user, peer = message["from_id"], message["peer_id"]
        if user not in self.allowed or peer != user:
            return  # Private conversations with explicitly allowed users only.
        session = self.store.session(user)
        if message["id"] <= session.get("last_message", -1):
            return
        session["last_message"] = message["id"]
        self.store.save_session(user, session)
        try:
            self.dispatch(user, message, session)
        except ValueError as exc:
            self.vk.send(peer, str(exc), MENU if "project" in session else [("Проекты", {"a": "projects"})])
            if "wizard" in session:
                self.project_menus.ask(user, session["wizard"])
        except Exception:
            self.vk.send(peer, "Операция не завершена. Черновик сохранён. Проверьте доступы и настройки API, затем повторите действие.")

    def dispatch(self, user, message, session):
        text = message.get("text", "").strip()
        payload = json.loads(message.get("payload") or "{}")
        action = payload.get("a")
        if text.lower() in ("начать", "start", "/start", "проекты") or not session:
            action = "projects"
        if self.project_menus.handle(user, action, payload, text, session):
            return
        projects = self.store.projects()
        if action == "project":
            if payload.get("p") not in projects:
                raise ValueError("Проект не найден. Откройте список проектов")
            session.clear()
            session.update(project=payload["p"], last_message=message["id"])
            self.store.save_session(user, session)
            self.vk.send(user, f"Проект «{projects[payload['p']]['name']}»", [
                ("Открыть задания", {"a": "dates"}), ("Настройки проекта", {"a": "settings"}),
                ("Проекты", {"a": "projects"})])
            return
        if session.get("project") not in projects:
            self.project_menus.show_projects(user)
            return
        project = projects[session["project"]]
        if action == "dates":
            zone = ZoneInfo(project.get("timezone", str(self.zone)))
            tasks = tasks_from_rows(self.google.rows(project), project, datetime.now(zone).date())
            session["tasks"] = [{**asdict(t), "day": t.day.isoformat()} for t in tasks]
            session.pop("task", None)
            session["mode"] = None
            session["page"] = 0
            session["project_revision"] = project.get("revision", 1)
            self.store.save_session(user, session)
            self.show_dates(user, session)
            return
        if session.get("project_revision") != project.get("revision", 1):
            raise ValueError("Настройки проекта изменились. Нажмите «Выбрать дату», чтобы обновить задания")
        if action == "page":
            session["page"] = max(0, int(payload["page"]))
            self.store.save_session(user, session)
            self.show_dates(user, session)
            return
        if action == "task":
            task = session["tasks"][int(payload["i"])]
            session["task"], session["mode"] = task, None
            self.store.save_session(user, session)
            self.vk.send(user, "\n".join(f"{k}: {v}" for k, v in task["fields"].items() if v.strip()), MENU)
            return
        if "task" not in session:
            raise ValueError("Выберите дату кнопкой")
        task = session["task"]
        # One shared draft per project/task, even if different operators select it.
        identity = {k: v for k, v in task["fields"].items() if k != project["link_column"]}
        draft_identity = [project["id"], task["row"], identity]
        if project.get("draft_scope"):
            draft_identity.append(project["draft_scope"])
        key = hashlib.sha256(json.dumps(draft_identity, sort_keys=True).encode()).hexdigest()
        draft = self.store.draft(key)
        if action == "preview":
            self.vk.send(user, f"{draft['text'] or 'Текст пока не создан'}\n\nФото: {len(draft['photos'])}/9", MENU)
            return
        if action == "publish":
            self.vk.send(user, "Сохраняю материалы на Google Диск…")
            url = publish(self.google, self.store, key, project, task)
            self.vk.send(user, "Готово. Ссылка записана в таблицу:\n" + url, MENU)
            return
        if draft["folder"]:
            raise ValueError("Выгрузка уже начата: материалы зафиксированы. Можно повторить отправку в таблицу")
        if action == "generate":
            session["mode"] = "supplement"
            self.store.save_session(user, session)
            self.vk.send(user, "Напишите дополнение к заданию или нажмите «Без дополнений». Повторная генерация заменит текст черновика.", [("Без дополнений", {"a": "no_supplement"})])
            return
        if action == "photos":
            session["mode"] = "photos"
            self.store.save_session(user, session)
            self.vk.send(user, f"Прикрепляйте фотографии как фото ВК. Сохранено {len(draft['photos'])}/9. Затем нажмите «Готово».", [("Готово", {"a": "done"})])
            return
        if action == "done":
            session["mode"] = None
            self.store.save_session(user, session)
            self.vk.send(user, f"Черновик сохранён. Фото: {len(draft['photos'])}/9", MENU)
            return
        if session.get("mode") == "supplement" and (not action or action == "no_supplement"):
            if not text and action != "no_supplement":
                raise ValueError("Напишите дополнение текстом")
            description = "\n".join(f"{k}: {v}" for k, v in identity.items() if v.strip())
            self.vk.send(user, "Генерирую текст…")
            draft["text"] = self.llm.generate(project, description, "" if action == "no_supplement" else text)
            self.store.save_draft(key, draft)
            session["mode"] = None
            self.store.save_session(user, session)
            self.vk.send(user, draft["text"], MENU)
            return
        if session.get("mode") == "photos":
            photos = [a["photo"] for a in message.get("attachments", []) if a["type"] == "photo"]
            known = {p["id"] for p in draft["photos"]}
            photos = {f"{p['owner_id']}_{p['id']}": p for p in photos}
            photos = {k: p for k, p in photos.items() if k not in known}
            if len(draft["photos"]) + len(photos) > 9:
                raise ValueError("Не более 9 фотографий. Эта партия не сохранена")
            if not photos:
                raise ValueError("Прикрепите новые фотографии как фото ВК")
            directory = self.store.root / key
            directory.mkdir(exist_ok=True)
            for photo_id, photo in photos.items():
                size = max(photo["sizes"], key=lambda s: s.get("width", 0) * s.get("height", 0))
                content = self.vk.download_photo(size["url"])
                filename = directory / f"{photo_id}.jpg"
                filename.write_bytes(content)
                draft["photos"].append({"id": photo_id, "path": str(filename.relative_to(self.store.root))})
                self.store.save_draft(key, draft)
            self.vk.send(user, f"Сохранено {len(draft['photos'])}/9 фото", [("Готово", {"a": "done"})])
            return
        raise ValueError("Выберите действие кнопкой меню")

    def show_dates(self, user, session):
        page = session.get("page", 0)
        buttons = [(f"{t['day']} — {t['weekday']} (стр. {t['row']})", {"a": "task", "i": i})
                   for i, t in enumerate(session["tasks"]) if page * 6 <= i < (page + 1) * 6]
        if page:
            buttons.append(("Назад", {"a": "page", "page": page - 1}))
        if (page + 1) * 6 < len(session["tasks"]):
            buttons.append(("Далее", {"a": "page", "page": page + 1}))
        buttons.extend([("Настройки проекта", {"a": "settings"}), ("Проекты", {"a": "projects"})])
        self.vk.send(user, "Выберите будущую дату" if session["tasks"] else "Будущих дат нет", buttons)


def main():
    load_dotenv()
    required = [k for k in ("VK_TOKEN", "VK_GROUP_ID", "VK_ALLOWED_USERS") if not os.getenv(k)]
    if required:
        raise SystemExit("Заполните переменные: " + ", ".join(required))
    config_path = Path(os.getenv("APP_CONFIG") or "config.json")
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    vk = VK(os.environ["VK_TOKEN"], int(os.environ["VK_GROUP_ID"]))
    bot = Bot(config, Store(os.getenv("APP_DATA", "data")), vk,
              Google(os.getenv("GOOGLE_CREDENTIALS_FILE", "token.json")), GenAPI(),
              {int(x.strip()) for x in os.environ["VK_ALLOWED_USERS"].split(",") if x.strip()})
    vk.run(bot.handle)


if __name__ == "__main__":
    main()
