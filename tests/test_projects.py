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
    chat.send(text="Авто")
    return next(iter(chat.bot.store.projects().values()))


def test_create_and_restart_without_config_json(tmp_path):
    bot = bot_at(tmp_path)
    chat = Conversation(bot)
    chat.send(text="Начать")
    assert bot.vk.messages[-1][1] == [("Создать проект", {"a": "project_new"})]
    project = create_project(chat)
    assert project["name"] == "Авто"
    assert "spreadsheet_id" not in project
    assert "drive_parent_id" not in project
    assert "concept" not in project and "prompt" not in project
    assert project["date_column"] == "Дата"
    assert "public_read" not in project and "timezone" not in project
    assert "wizard" not in bot.store.session(1)
    restored = bot_at(tmp_path)
    assert restored.store.projects()[project["id"]] == project


@pytest.mark.parametrize("field,value", [("concept", "Новая концепция"),
                                        ("prompt", "Новый промпт")])
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


@pytest.mark.parametrize("field", ["worksheet", "drive_parent_id", "date_column", "weekday_column", "link_column", "public_read", "timezone", "month_rollover_days"])
def test_removed_settings_cannot_be_changed_by_old_buttons(tmp_path, project, field):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    before = bot.store.projects()["test"].copy()
    chat.send("project_edit", field=field)
    chat.send(text="Новое значение")
    updated = bot.store.projects()["test"]
    assert updated == before and "wizard" not in bot.store.session(1)


@pytest.mark.parametrize("field,value,expected", [
    ("spreadsheet_id", "https://docs.google.com/spreadsheets/d/new_sheet/edit", "new_sheet"),
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
    restarted = bot_at(tmp_path)
    Conversation(restarted).send(text="Первый проект")
    assert next(iter(restarted.store.projects().values()))["name"] == "Первый проект"
    assert "wizard" not in restarted.store.session(1)


def test_retired_pending_input_cannot_change_columns(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    session = bot.store.session(1)
    session.update(wizard={"kind": "edit", "field": "link_column", "project": "test", "revision": 1}, view="edit")
    bot.store.save_session(1, session)
    chat.send(text="Дата")
    assert bot.store.projects()["test"]["link_column"] == "Ссылка"
    assert "wizard" not in bot.store.session(1)


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
    assert any("Настройки проекта изменились" in text for text, _ in bot.vk.messages)
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
        rows = [item if isinstance(item, list) else [item] for item in buttons]
        reached.update(p["field"] for row in rows for _, p in row if p["a"] == "project_edit")
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


def actions(bot):
    rows = [item if isinstance(item, list) else [item] for item in bot.vk.messages[-1][1]]
    return {p["a"] for row in rows for _, p in row}


def test_create_only_name_then_independent_fields(tmp_path):
    bot = bot_at(tmp_path)
    chat = Conversation(bot)
    project = create_project(chat)
    assert actions(bot) == {"settings", "projects"}
    assert "dates" not in actions(bot)
    chat.send("project_edit", field="concept")
    assert actions(bot) == {"settings", "project_home"}
    chat.send(text="Наша концепция")
    assert bot.store.projects()[project["id"]]["concept"] == "Наша концепция"
    assert bot.store.session(1)["view"] == "settings"
    chat.send("project_edit", field="spreadsheet_id")
    chat.send(text="https://docs.google.com/spreadsheets/d/abc/edit")
    assert bot.store.session(1)["view"] == "settings"
    chat.send("project_edit", field="worksheet")
    chat.send(text="Sheet")
    chat.send("project_home")
    assert actions(bot) == {"task", "settings", "projects"}
    assert "generate" not in actions(bot)


def test_return_from_edit_does_not_save_input(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_edit", field="concept")
    chat.send("project_home")
    assert "wizard" not in bot.store.session(1)
    assert bot.store.session(1)["view"] == "dates"
    chat.send(text="Это уже не концепция")
    assert bot.store.projects()["test"]["concept"] == "Concept"
    assert actions(bot) == {"task", "settings", "projects"}


def test_return_to_settings_uses_one_screen(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("settings", page=1)
    chat.send("project_edit", field="concept")
    back = next(p for _, p in bot.vk.messages[-1][1] if p["a"] == "settings")
    assert back["page"] == 0
    chat.send(**{"action": back["a"], "page": back["page"]})
    assert bot.store.session(1)["settings_page"] == 0
    assert "wizard" not in bot.store.session(1)
    assert "project_home" in actions(bot)


def test_task_keyboard_matches_draft_and_input_step(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("dates")
    chat.send("task", i=0)
    assert actions(bot) == {"generate", "photos", "project_home"}
    chat.send("generate")
    assert actions(bot) == {"no_supplement", "task_home", "project_home"}
    chat.send("task_home")
    assert bot.store.session(1)["mode"] is None
    chat.send("photos")
    assert actions(bot) == {"done", "task_home", "project_home"}
    chat.send("done")
    key, draft = bot.draft_for(bot.store.session(1), project)
    draft["text"] = "Text"
    draft["photos"] = [{"id": str(i), "path": "unused"} for i in range(9)]
    bot.store.save_draft(key, draft)
    chat.send("task_home")
    assert actions(bot) == {"generate", "preview", "publish", "project_home"}
    draft["folder"] = "folder"
    bot.store.save_draft(key, draft)
    chat.send("task_home")
    assert actions(bot) == {"preview", "publish", "project_home"}
    draft["published"] = True
    bot.store.save_draft(key, draft)
    session = bot.store.session(1)
    session["task"]["material_url"] = "https://drive.google.com/drive/folders/folder"
    bot.store.save_session(1, session)
    chat.send("task_home")
    assert actions(bot) == {"preview", "project_home"}


def test_stale_action_cannot_publish_without_text(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("dates")
    chat.send("task", i=0)
    chat.send("publish")
    assert not bot.google.files
    assert actions(bot) == {"generate", "photos", "project_home"}
    assert any("недоступно" in text for text, _ in bot.vk.messages)


def test_missing_generation_settings_keep_button_and_explain_setup(tmp_path, project):
    project.pop("prompt")
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("dates")
    chat.send("task", i=0)
    assert "generate" in actions(bot) and "settings" in actions(bot)
    chat.send("generate")
    assert "Промпт" in bot.vk.messages[-1][0]
    assert "настройках проекта" in bot.vk.messages[-1][0]
    assert bot.store.session(1)["mode"] is None
    assert "generate" in actions(bot)


def test_remove_only_untouched_demo_and_prevent_reimport(tmp_path, project):
    demo = dict(project, id="example", name="Пример проекта",
                concept="Концепция проекта и описание аудитории",
                prompt="Напиши текст публикации по заданию. Не выдумывай факты.",
                spreadsheet_id="ID_ТАБЛИЦЫ_ИЗ_ССЫЛКИ", drive_parent_id="ID_ПАПКИ_ПРОЕКТА")
    store = Store(tmp_path)
    store.save_project(demo)
    bot = bot_at(tmp_path, {"projects": [demo, project]})
    assert set(bot.store.projects()) == {"test"}
    assert set(bot_at(tmp_path, {"projects": [demo, project]}).store.projects()) == {"test"}
    changed = store.save_project(demo)
    store.save_project({**changed, "concept": "Моя реальная концепция"}, expected_revision=1)
    bot = bot_at(tmp_path, {"projects": [demo]})
    assert bot.store.projects()["example"]["concept"] == "Моя реальная концепция"


def test_menu_text_exits_current_input(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_edit", field="prompt")
    chat.send(text="Меню")
    assert "wizard" not in bot.store.session(1)
    assert bot.store.session(1)["view"] == "dates"
    assert bot.store.projects()["test"]["prompt"] == "Prompt"
