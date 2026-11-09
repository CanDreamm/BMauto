import pytest

from bmauto.core import Store
from bmauto.main import Bot
from bmauto.vk import VK
from test_projects import Conversation, actions, create_project
from test_workflow import FakeGoogle, FakeLLM, FakeVK


def make_bot(tmp_path, projects):
    vk = FakeVK()
    vk.resolve_user = lambda value: VK.resolve_user(vk, value)
    return Bot({"projects": projects}, Store(tmp_path), vk, FakeGoogle(), FakeLLM(), {1})


def add_through_menu(bot, member=3):
    owner = Conversation(bot)
    owner.send("project", p="test")
    owner.send("members")
    owner.send("member_add")
    assert actions(bot) == {"members"}
    owner.send(text=f"https://vk.com/id{member}")
    assert bot.store.members("test")[member] == "member"
    return owner


def test_invite_without_env_edit_and_filter_projects(tmp_path, project):
    other = {**project, "id": "private", "name": "Скрытый проект"}
    bot = make_bot(tmp_path, [project, other])
    add_through_menu(bot)
    chat = Conversation(bot, 3)
    chat.send(text="Начать")
    buttons = bot.vk.messages[-1][1]
    assert buttons == [("Test", {"a": "project", "p": "test"}), ("Мой мониторинг", {"a": "monitor", "_color": "primary"})]
    assert "Скрытый проект" not in bot.vk.messages[-1][0]
    chat.send("project", p="test")
    assert actions(bot) == {"task", "settings", "projects"}
    chat.send("project", p="private")
    assert bot.store.session(3)["project"] == "test"
    assert actions(bot) == {"task", "settings", "projects"}


def test_new_project_creator_is_owner_and_members_persist(tmp_path):
    bot = make_bot(tmp_path, [])
    project = create_project(Conversation(bot))
    assert bot.store.members(project["id"]) == {1: "owner"}
    bot.store.add_member(project["id"], 1, 3, bot.allowed)
    restarted = make_bot(tmp_path, [])
    assert restarted.store.can_access(project["id"], 3, restarted.allowed)
    assert restarted.store.members(project["id"])[1] == "owner"


@pytest.mark.parametrize("action,payload", [("project_edit", {"field": "concept"}),
                                           ("members", {}), ("member_add", {}),
                                           ("member_remove", {"user": 1}), ("project_new", {})])
def test_members_cannot_change_settings_or_access(tmp_path, project, action, payload):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    chat = Conversation(bot, 3)
    chat.send("project", p="test")
    before = bot.store.projects()["test"].copy()
    chat.send(action, **payload)
    assert bot.store.projects()["test"] == before
    assert bot.store.members("test") == {1: "owner", 3: "member"}
    assert actions(bot) == {"task", "settings", "projects"}
    assert "wizard" not in bot.store.session(3)


def test_shared_draft_photo_upload_and_publish_by_member(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    owner = Conversation(bot, 1)
    owner.send("dates")
    owner.send("task", i=0)
    owner.send("generate")
    owner.send(text="Добавьте цену")
    owner_key, _ = bot.draft_for(bot.store.session(1), project)
    chat = Conversation(bot, 3)
    chat.send("project", p="test")
    chat.send("dates")
    chat.send("task", i=0)
    member_key, _ = bot.draft_for(bot.store.session(3), project)
    assert owner_key == member_key
    chat.send("preview")
    assert "Ссылка появится после отправки" in bot.vk.messages[-1][0]
    assert bot.store.draft(owner_key)["text"] == "Сгенерированный текст"
    chat.send("photos")
    session = bot.store.session(3)
    bot.handle(dict(id=session["last_message"] + 1, from_id=3, peer_id=3, text="",
                    attachments=[dict(type="photo", photo=dict(owner_id=3, id=5,
                                  sizes=[dict(width=10, height=10, url="photo")]))]))
    chat.send("task_home")
    assert len(bot.store.draft(owner_key)["photos"]) == 1
    chat.send("publish")
    assert bot.store.draft(owner_key)["published"]
    assert len(bot.google.created) == 2


def test_remove_confirm_and_revoke_stale_buttons(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    owner = add_through_menu(bot)
    participant = Conversation(bot, 3)
    participant.send("project", p="test")
    participant.send("dates")
    participant.send("task", i=0)
    owner.send("member_remove", user=3)
    assert bot.store.can_access("test", 3, bot.allowed)
    assert actions(bot) == {"member_remove_confirm", "members"}
    owner.send("member_remove_confirm", user=3)
    assert not bot.store.can_access("test", 3, bot.allowed)
    assert "task" not in bot.store.session(3)
    participant.send("generate")
    assert not bot.google.files
    assert bot.vk.messages[-1][1] == []
    assert "нет доступных проектов" in bot.vk.messages[-1][0]


def test_revocation_preserves_other_project_access(tmp_path, project):
    bot = make_bot(tmp_path, [project, {**project, "id": "second", "name": "Второй"}])
    add_through_menu(bot)
    bot.store.add_member("second", 1, 3, bot.allowed)
    participant = Conversation(bot, 3)
    participant.send("project", p="test")
    bot.store.remove_member("test", 1, 3, bot.allowed)
    participant.send("projects")
    assert bot.vk.messages[-1][1] == [("Второй", {"a": "project", "p": "second"}), ("Мой мониторинг", {"a": "monitor", "_color": "primary"})]
    participant.send("project", p="test")
    assert "project" not in bot.store.session(3)


def test_duplicate_invite_and_owner_cannot_be_removed(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    owner = add_through_menu(bot)
    owner.send("member_add")
    owner.send(text="3")
    assert bot.store.members("test") == {1: "owner", 3: "member"}
    assert any("уже есть доступ" in text for text, _ in bot.vk.messages)
    owner.send("members")
    owner.send("member_remove", user=1)
    assert bot.store.members("test")[1] == "owner"


def test_member_remove_requires_current_confirmation(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    owner = add_through_menu(bot)
    owner.send("member_remove_confirm", user=3)
    assert bot.store.can_access("test", 3, bot.allowed)
    owner.send("member_remove", user=3)
    owner.send("project_home")
    owner.send("member_remove_confirm", user=3)
    assert bot.store.can_access("test", 3, bot.allowed)


def test_owner_keeps_management_after_admin_list_changes(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    restarted = Bot({}, Store(tmp_path), bot.vk, FakeGoogle(), FakeLLM(), {2})
    owner = Conversation(restarted, 1)
    owner.send("project", p="test")
    owner.send("member_add")
    owner.send(text="3")
    assert restarted.store.members("test") == {1: "owner", 3: "member"}
    assert restarted.store.can_manage("test", 1, {2})
    assert restarted.store.can_manage("test", 2, {2})


def test_members_keyboard_pagination_and_cancel(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    owner = Conversation(bot)
    owner.send("project", p="test")
    for user in range(3, 24):
        bot.store.add_member("test", 1, user, bot.allowed)
    owner.send("members", page=1)
    assert len(bot.vk.messages[-1][1]) == 9
    owner.send("member_add")
    owner.send("project_home")
    owner.send(text="100")
    assert 100 not in bot.store.members("test")
    assert "member_input" not in bot.store.session(1)


@pytest.mark.parametrize("action", ["preview", "publish", "photos", "generate", "project_home", "dates", "settings", "members"])
def test_every_action_rechecks_access_even_if_old_session_survives(tmp_path, project, action):
    bot = make_bot(tmp_path, [project, {**project, "id": "second"}])
    add_through_menu(bot)
    bot.store.add_member("second", 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    member.send("project", p="test")
    member.send("dates")
    member.send("task", i=0)
    with bot.store.db:
        bot.store.db.execute("DELETE FROM project_members WHERE project_id='test' AND user_id=3")
    old_count = len(bot.vk.messages)
    member.send(action)
    assert "project" not in bot.store.session(3)
    assert not bot.google.files
    assert actions(bot) == {"project", "monitor"}
    assert all("Задание: Текст" not in text for text, _ in bot.vk.messages[old_count:])


def test_legacy_cancel_clears_member_input(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    owner = Conversation(bot)
    owner.send("project", p="test")
    owner.send("member_add")
    owner.send("project_cancel")
    owner.send(text="123")
    assert 123 not in bot.store.members("test")


def test_member_cannot_resume_old_admin_wizard(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    session = {"project": "test", "wizard": {"kind": "create"}, "view": "create"}
    bot.store.save_session(3, session)
    Conversation(bot, 3).send(text="Новый проект")
    assert set(bot.store.projects()) == {"test"}
    assert "wizard" not in bot.store.session(3)
    assert actions(bot) == {"task", "settings", "projects"}


def test_member_can_read_settings_but_cannot_edit(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    member = Conversation(bot, 3)
    member.send("project", p="test")
    member.send("settings")
    assert actions(bot) == {"google_connect", "project_home"}
    assert "Concept" in bot.vk.messages[-1][0]
    member.send("project_edit", field="concept")
    member.send(text="Подмена концепции")
    assert bot.store.projects()["test"]["concept"] == "Concept"
    assert actions(bot) == {"google_connect", "project_home"}


@pytest.mark.parametrize("value,expected", [("123", 123), ("id123", 123),
                                          ("https://vk.com/id123", 123), ("https://vk.ru/id123?x=1", 123)])
def test_vk_numeric_profile(value, expected):
    vk = VK.__new__(VK)
    assert vk.resolve_user(value) == expected


@pytest.mark.parametrize("name", ["example.name", "idols"])
def test_vk_vanity_profile_uses_fixed_api(name):
    vk = VK.__new__(VK)
    seen = []
    def call(method, **kwargs):
        seen.append((method, kwargs))
        return [{"id": 123}]
    vk.call = call
    assert vk.resolve_user(f"https://vk.com/{name}") == 123
    assert seen == [("users.get", {"user_ids": name})]


@pytest.mark.parametrize("value", ["0", "-1", "https://evil.test/id123", "https://vk.com.evil.test/id123", "999999999999999999999999999", "[id123|name]"])
def test_invalid_vk_profile(value):
    with pytest.raises(ValueError):
        VK.__new__(VK).resolve_user(value)
