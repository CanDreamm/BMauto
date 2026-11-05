import json

import pytest

from bmauto.core import Store
from bmauto.google import Google
from bmauto.main import Bot
from bmauto.projects import FIELDS, google_id
from test_workflow import FakeGoogle, FakeLLM, FakeVK


class Conversation:
    def __init__(self, bot, user=1):
        self.bot, self.user = bot, user

    def send(self, action=None, text="", **payload):
        number = self.bot.store.session(self.user).get("last_message", 0) + 1
        self.bot.handle(dict(id=number, from_id=self.user, peer_id=self.user, text=text,
                             payload=json.dumps({"a": action, **payload}) if action else ""))


def bot_at(path, config=None):
    return Bot(config or {}, Store(path), FakeVK(), FakeGoogle(), FakeLLM(), {1, 2})


def create_project(chat):
    chat.send("project_new")
    for value in ("Авто", "Для покупателей авто", "Напиши публикацию",
                  "https://docs.google.com/spreadsheets/d/sheet_123/edit", "Контент",
                  "https://drive.google.com/drive/folders/folder_123"):
        chat.send(text=value)
    for _ in range(3):
        chat.send("project_default")
    chat.send("project_value", value=True)
    chat.send("project_default")
    return next(iter(chat.bot.store.projects().values()))


def test_create_and_restart_without_config_json(tmp_path):
    bot = bot_at(tmp_path)
    chat = Conversation(bot)
    chat.send(text="Начать")
    assert bot.vk.messages[-1][1] == [("Создать проект", {"a": "project_new"})]
    project = create_project(chat)
    assert project["spreadsheet_id"] == "sheet_123"
    assert project["drive_parent_id"] == "folder_123"
    assert project["date_column"] == "Дата"
    assert project["public_read"] is True
    assert project["timezone"] == "Europe/Moscow"
    assert "wizard" not in bot.store.session(1)
    restored = bot_at(tmp_path)
    assert restored.store.projects()[project["id"]] == project


@pytest.mark.parametrize("field,value", [("name", "Новое имя"), ("concept", "Новая концепция"),
                                        ("prompt", "Новый промпт"), ("worksheet", "Новая вкладка"),
                                        ("date_column", "Дата публикации"), ("weekday_column", "День"),
                                        ("link_column", "Материалы"), ("timezone", "Asia/Yekaterinburg")])
def test_edit_fields_survive_restart_and_legacy_import(tmp_path, project, field, value):
    config = {"projects": [project]}
    bot = bot_at(tmp_path, config)
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("settings")
    chat.send("project_edit", field=field)
    chat.send(text=value)
    restarted = bot_at(tmp_path, config)
    updated = restarted.store.projects()["test"]
    assert updated[field] == value and updated["revision"] == 2
    assert "task" not in restarted.store.session(1)


def test_public_access_edit_and_new_destination_isolation(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_edit", field="public_read")
    chat.send("project_value", value=False)
    updated = bot.store.projects()["test"]
    assert updated["public_read"] is False and updated["draft_scope"]


@pytest.mark.parametrize("field,value,expected", [
    ("spreadsheet_id", "https://docs.google.com/spreadsheets/d/new_sheet/edit", "new_sheet"),
    ("drive_parent_id", "https://drive.google.com/drive/u/0/folders/new_folder", "new_folder"),
])
def test_change_google_links(tmp_path, project, field, value, expected):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_edit", field=field)
    chat.send(text=value)
    updated = bot.store.projects()["test"]
    assert updated[field] == expected and updated["draft_scope"]


def test_cancel_and_invalid_input_do_not_write_project(tmp_path):
    bot = bot_at(tmp_path)
    chat = Conversation(bot)
    chat.send("project_new")
    chat.send(text="")
    assert "wizard" in bot.store.session(1)
    assert any("не может быть пустым" in text for text, _ in bot.vk.messages)
    chat.send("project_cancel")
    assert not bot.store.projects()
    assert "wizard" not in bot.store.session(1)


def test_wizard_resumes_after_restart(tmp_path):
    chat = Conversation(bot_at(tmp_path))
    chat.send("project_new")
    chat.send(text="Первый проект")
    restarted = bot_at(tmp_path)
    Conversation(restarted).send(text="Сохранённая концепция")
    wizard = restarted.store.session(1)["wizard"]
    assert wizard["step"] == 2
    assert wizard["values"]["name"] == "Первый проект"
    assert wizard["values"]["concept"] == "Сохранённая концепция"


def test_duplicate_column_is_rejected(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_edit", field="link_column")
    chat.send(text="Дата")
    assert bot.store.projects()["test"]["link_column"] == "Ссылка"
    assert "wizard" in bot.store.session(1)
    chat.send(text="Материалы")
    assert bot.store.projects()["test"]["link_column"] == "Материалы"


def test_other_operator_must_reload_after_settings_change(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    operator = Conversation(bot, 2)
    operator.send("project", p="test")
    operator.send("dates")
    editor = Conversation(bot, 1)
    editor.send("project", p="test")
    editor.send("project_edit", field="concept")
    editor.send(text="Новая концепция")
    operator.send("task", i=0)
    assert "Настройки проекта изменились" in bot.vk.messages[-1][0]
    assert "task" not in bot.store.session(2)


def test_concurrent_edit_does_not_overwrite(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    a, b = Conversation(bot, 1), Conversation(bot, 2)
    for chat in (a, b):
        chat.send("project", p="test")
        chat.send("project_edit", field="concept")
    a.send(text="Первый редактор")
    b.send(text="Второй редактор")
    assert bot.store.projects()["test"]["concept"] == "Первый редактор"
    assert any("изменились" in text for text, _ in bot.vk.messages)


def test_all_fields_reachable_and_keyboards_fit_vk(tmp_path, project):
    projects = [{**project, "id": str(i), "name": str(i)} for i in range(25)]
    bot = bot_at(tmp_path, {"projects": projects})
    chat = Conversation(bot)
    chat.send("project_page", page=1)
    assert len(bot.vk.messages[-1][1]) == 10
    chat.send("project", p="0")
    reached = set()
    for page in (0, 1):
        chat.send("settings", page=page)
        buttons = bot.vk.messages[-1][1]
        assert len(buttons) <= 10
        reached.update(p["field"] for _, p in buttons if p["a"] == "project_edit")
    assert reached == {field[0] for field in FIELDS}


def test_unauthorized_cannot_create_projects(tmp_path):
    bot = bot_at(tmp_path)
    Conversation(bot, 99).send("project_new")
    assert not bot.store.projects() and not bot.store.session(99)


@pytest.mark.parametrize("value", ["https://evil.test/spreadsheets/d/abc/edit", "https://docs.google.com.evil.test/spreadsheets/d/abc", "http://docs.google.com/spreadsheets/d/abc", "garbage text"])
def test_bad_google_link_rejected(value):
    with pytest.raises(ValueError):
        google_id(value, "spreadsheet_id")


def test_google_authorization_is_lazy(tmp_path):
    google = Google(str(tmp_path / "missing-token.json"))
    assert google.credentials is None
    with pytest.raises(ValueError, match="Google ещё не подключён"):
        google.rows(dict(worksheet="Sheet", spreadsheet_id="123"))
