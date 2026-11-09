import json
from datetime import date, datetime, timedelta

import pytest

from bmauto.core import Store
from bmauto.google import Google
from bmauto.main import Bot
from bmauto.vk import VK, keyboard
from test_projects import Conversation, actions, bot_at, create_project
from test_workflow import FakeGoogle, FakeLLM, FakeVK


@pytest.fixture(autouse=True)
def calendar_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(3000, 1, 1, tzinfo=tz)
    monkeypatch.setattr("bmauto.main.datetime", Clock)


def serialized(bot):
    return keyboard(bot.vk.messages[-1][1])["buttons"]


def payload(button):
    return json.loads(button["action"]["payload"])


def many_dates(google, count):
    start = date(3000, 1, 2)
    google.table = [["План публикаций"], ["Неделя", "Дата", "День недели", "Задание", "Ссылка"]] + [
        ["45", (start + timedelta(days=i)).strftime("%d.%m.%Y"), "Среда", f"Задание {i}", ""]
        for i in range(count)
    ]


def test_select_project_directly_shows_two_column_dates_and_only_two_footer_buttons(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    many_dates(bot.google, 5)
    Conversation(bot).send("project", p="test")
    rows = serialized(bot)
    assert [len(row) for row in rows] == [2, 1, 1, 1, 1, 1]
    assert [payload(b)["i"] for row in rows[:-2] for b in row] == [0, 4, 1, 2, 3]
    assert rows[0][0]["action"]["label"] == "02.01.3000 — Среда"
    assert rows[-2][0]["action"]["label"] == "Настройки"
    assert rows[-1][0]["action"]["label"] == "Все проекты"
    assert actions(bot) == {"task", "settings", "projects"}
    assert bot.store.session(1)["view"] == "dates"


def test_dates_pagination_retains_pairs_and_selects_correct_task(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    many_dates(bot.google, 29)
    chat = Conversation(bot)
    chat.send("project", p="test")
    first_page = serialized(bot)
    assert len(first_page) == 7 and all(len(row) == 2 for row in first_page[:4])
    chat.send("page", page=1)
    middle = serialized(bot)
    assert len(middle) == 7 and len(middle[-3]) == 2
    assert [payload(b)["i"] for row in middle[:4] for b in row] == [8, 12, 9, 13, 10, 14, 11, 15]
    chat.send("task", i=13)
    assert "Задание: Задание 13" in bot.vk.messages[-1][0]
    chat.send("project_home")
    assert actions(bot) == {"task", "page", "settings", "projects"}
    assert "task" not in bot.store.session(1)
    chat.send("page", page=3)
    last = serialized(bot)
    assert [len(row) for row in last[:4]] == [2, 1, 1, 1]
    assert [payload(b)["i"] for row in last[:4] for b in row] == [24, 28, 25, 26, 27]


def test_new_project_and_empty_calendar_have_exact_footer(tmp_path, project):
    bot = bot_at(tmp_path)
    create_project(Conversation(bot))
    assert actions(bot) == {"settings", "projects"}
    assert len(serialized(bot)) == 2
    assert "настройках" in bot.vk.messages[-1][0]
    empty = bot_at(tmp_path / "other", {"projects": [project]})
    empty.google.table = [["План публикаций"], ["Дата", "День недели", "Ссылка"]]
    Conversation(empty).send("project", p="test")
    assert actions(empty) == {"settings", "projects"}
    assert "Будущих дат нет" in empty.vk.messages[-1][0]


def test_missing_google_authorization_and_api_failure_keep_settings_accessible(tmp_path, project):
    vk = FakeVK()
    bot = Bot({"projects": [project]}, Store(tmp_path), vk, Google(str(tmp_path / "missing.json")), FakeLLM(), {1})
    chat = Conversation(bot)
    chat.send("project", p="test")
    assert actions(bot) == {"settings", "projects"}
    assert "Google ещё не подключён" in vk.messages[-1][0]
    chat.send("settings")
    assert "project_edit" in actions(bot)
    bot.google = FakeGoogle()
    many_dates(bot.google, 2)
    chat.send("project_home")
    assert actions(bot) == {"task", "settings", "projects"}
    def fail(project):
        raise RuntimeError("upstream failure")
    bot.google.rows = fail
    chat.send("project_home")
    assert actions(bot) == {"settings", "projects"}
    assert "Не удалось загрузить даты" in vk.messages[-1][0]
    assert not bot.store.session(1)["tasks"]


def test_settings_contain_fields_and_members_without_nested_settings_button(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("settings")
    assert actions(bot) == {"project_edit", "members", "google_connect", "project_home", "project_delete"}
    labels = [b["action"]["label"] for row in serialized(bot) for b in row]
    assert "Участники" in labels
    assert "Настройки" not in labels and "Настройки проекта" not in labels
    assert len(serialized(bot)) <= 10
    chat.send("project_edit", field="concept")
    assert "Текущее значение: Concept" in bot.vk.messages[-1][0]
    chat.send(text="Новая концепция")
    assert bot.store.session(1)["view"] == "settings"
    assert "members" in actions(bot) and "task" not in actions(bot)
    chat.send("members")
    assert "settings" in actions(bot)
    chat.send("settings")
    chat.send("project_home")
    assert actions(bot) == {"task", "settings", "projects"}


def test_vk_send_transmits_real_two_column_keyboard(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    many_dates(bot.google, 8)
    Conversation(bot).send("project", p="test")
    vk = VK.__new__(VK)
    calls = []
    vk.call = lambda method, **kwargs: calls.append((method, kwargs))
    text, buttons = bot.vk.messages[-1]
    vk.send(1, text, buttons)
    sent = json.loads(calls[0][1]["keyboard"])
    assert [len(row) for row in sent["buttons"]] == [2, 2, 2, 2, 1, 1]
    assert sent["buttons"][-2][0]["action"]["label"] == "Настройки"
    assert sent["buttons"][-1][0]["action"]["label"] == "Все проекты"


@pytest.mark.parametrize("status,url,prefix", [
    ("Запланировано", None, ""),
    ("Материалы отправлены", "https://drive.google.com/drive/folders/test", "🟡 "),
    ("Согласовано", "https://drive.google.com/drive/folders/test", "🟢 "),
])
def test_dates_show_material_status_emoji(tmp_path, project, status, url, prefix):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    session = bot.store.session(1)
    session["tasks"][0].update(status=status, material_url=url)
    bot.show_dates(1, session)
    button = serialized(bot)[0][0]
    label = button["action"]["label"]
    assert label.startswith(prefix)
    if not prefix:
        assert not label.startswith(("🟡", "🟢"))
    assert json.loads(button["action"]["payload"]) == {"a": "task", "i": 0}
    assert serialized(bot)[-2][0]["color"] == "primary"
    assert serialized(bot)[-1][0]["color"] == "primary"


def test_missing_prompt_does_not_add_settings_shortcuts_to_task_menus(tmp_path, project):
    project["prompt"] = ""
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    assert "settings" not in actions(bot)
    chat.send("photos")
    assert "settings" not in actions(bot)
    chat.send("task_home")
    chat.send("edit_text")
    assert "settings" not in actions(bot)
    for row in serialized(bot):
        for button in row:
            if button["action"]["label"] in {"Назад", "Меню проекта"}:
                assert button["color"] == "primary"
    chat.send("task_home")
    chat.send("project_home")
    assert "settings" in actions(bot)


def test_shared_navigation_colors_and_green_create_project():
    from bmauto.vk import keyboard
    buttons = keyboard([(label, {"a": "test"}) for label in (
        "Назад", "Меню проекта", "Настройки", "Все проекты", "Создать проект")])["buttons"]
    assert [row[0]["color"] for row in buttons] == ["primary"] * 4 + ["positive"]
