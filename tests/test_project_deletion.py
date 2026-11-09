import json

import pytest

from bmauto.core import Store
from bmauto.main import Bot
from bmauto.vk import keyboard
from test_projects import Conversation, actions, bot_at, create_project
from test_workflow import FakeGoogle, FakeLLM, FakeVK


def all_buttons(bot):
    return [button for row in keyboard(bot.vk.messages[-1][1])["buttons"] for button in row]


def confirmation(bot):
    return json.loads(all_buttons(bot)[0]["action"]["payload"])


def test_settings_have_red_delete_and_back_without_rename(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    for page in (0, 1):
        chat.send("settings", page=page)
        buttons = all_buttons(bot)
        delete = next(b for b in buttons if b["action"]["label"] == "Удалить проект")
        assert delete["color"] == "negative"
        assert "_color" not in json.loads(delete["action"]["payload"])
        assert any(b["action"]["label"] == "Назад к датам" for b in buttons)
        assert not any(b["action"]["label"] == "Название" for b in buttons)
        assert len(keyboard(bot.vk.messages[-1][1])["buttons"]) <= 10
    chat.send("project_home")
    assert bot.store.session(1)["view"] == "dates"


def test_create_name_works_but_old_rename_action_is_rejected(tmp_path):
    bot = bot_at(tmp_path)
    chat = Conversation(bot)
    project = create_project(chat)
    chat.send("project_edit", field="name")
    chat.send(text="Новое название")
    assert bot.store.projects()[project["id"]]["name"] == "Авто"
    session = bot.store.session(1)
    session.update(wizard={"kind": "edit", "field": "name", "project": project["id"], "revision": 1}, view="edit")
    bot.store.save_session(1, session)
    chat.send(text="Ещё одно имя")
    assert bot.store.projects()[project["id"]]["name"] == "Авто"
    assert "wizard" not in bot.store.session(1)


def test_delete_requires_red_confirmation_and_does_not_touch_google(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_delete")
    assert "test" in bot.store.projects()
    assert all_buttons(bot)[0]["color"] == "negative"
    payload = confirmation(bot)
    chat.send(payload.pop("a"), **payload)
    assert "test" not in bot.store.projects()
    assert bot.store.members("test") == {}
    assert bot.store.session(1)["view"] == "projects"
    assert "project" not in bot.store.session(1)
    assert bot.google.files == {} and bot.google.created == [] and bot.google.shared == []
    assert bot.google.table[2][3] == "Текст"
    assert actions(bot) == {"project_new"}
    assert any("Файлы на Google Диске и данные таблицы сохранены" in text for text, _ in bot.vk.messages)
    # A legacy config cannot re-import the deleted project, even after restart.
    restarted = bot_at(tmp_path, {"projects": [project]})
    assert not restarted.store.projects()
    with pytest.raises(ValueError, match="удалён"):
        restarted.store.save_project(project, expected_revision=2)


@pytest.mark.parametrize("cancel", ["settings", "project_home", "projects"])
def test_cancel_makes_old_confirmation_unusable(tmp_path, project, cancel):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_delete")
    payload = confirmation(bot)
    chat.send(cancel)
    assert "delete_confirmation" not in bot.store.session(1)
    chat.send(payload.pop("a"), **payload)
    assert "test" in bot.store.projects()


def test_old_confirmation_cannot_delete_different_selected_project(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project, {**project, "id": "second"}]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_delete")
    payload = confirmation(bot)
    chat.send("project", p="second")
    chat.send(payload.pop("a"), **payload)
    assert set(bot.store.projects()) == {"test", "second"}


def test_changed_project_requires_new_confirmation(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("project_delete")
    payload = confirmation(bot)
    current = bot.store.projects()["test"]
    bot.store.save_project({**current, "concept": "Изменено"}, current["revision"])
    chat.send(payload.pop("a"), **payload)
    assert "test" in bot.store.projects()
    assert bot.store.session(1)["view"] == "settings"
    assert "delete_confirmation" not in bot.store.session(1)


def test_delete_revokes_all_sessions_but_keeps_other_project_and_drafts(tmp_path, project):
    store = Store(tmp_path)
    bot = Bot({"projects": [project, {**project, "id": "other"}]}, store, FakeVK(), FakeGoogle(), FakeLLM(), {1})
    store.add_member("test", 1, 3, {1})
    store.add_member("other", 1, 3, {1})
    member = Conversation(bot, 3)
    member.send("project", p="test")
    member.send("task", i=0)
    owner = Conversation(bot)
    owner.send("project", p="test")
    owner.send("project_delete")
    payload = confirmation(bot)
    store.save_draft("preserved", {"text": "Сохранено"})
    owner.send(payload.pop("a"), **payload)
    assert "task" not in store.session(3) and "project" not in store.session(3)
    assert set(store.visible_projects(3, {1})) == {"other"}
    assert store.draft("preserved")["text"] == "Сохранено"
    member.send("project", p="test")
    assert "project" not in store.session(3)


def test_members_cannot_delete_even_with_forged_confirmation(tmp_path, project):
    bot = Bot({"projects": [project]}, Store(tmp_path), FakeVK(), FakeGoogle(), FakeLLM(), {1})
    bot.store.add_member("test", 1, 3, {1})
    member = Conversation(bot, 3)
    member.send("project", p="test")
    member.send("settings")
    assert actions(bot) == {"google_connect", "project_home"}
    member.send("project_delete")
    assert "delete_confirmation" not in bot.store.session(3)
    session = bot.store.session(3)
    session.update(view="project_delete", delete_confirmation={"project": "test", "revision": 1, "token": "fake"})
    bot.store.save_session(3, session)
    member.send("project_delete_confirm", p="test", token="fake")
    assert "test" in bot.store.projects()


def test_pending_google_authorization_cannot_restore_deleted_project(tmp_path, project):
    from bmauto.oauth import GoogleConnections
    client = tmp_path / "credentials-web.json"
    client.write_text(json.dumps({"web": {"client_id": "fake", "client_secret": "fake",
                                        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}))
    bot = bot_at(tmp_path, {"projects": [project]})
    conn = GoogleConnections(bot.store, {1}, str(client), "https://bot.itiscat.ru/oauth/google/callback")
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(conn.begin("test", 1)).query)["state"][0]
    bot.store.delete_project("test", 1, {1}, expected_revision=1)
    with pytest.raises(ValueError, match="больше нет прав"):
        conn.finish(state, "fake-code")
    assert not bot.store.projects()
