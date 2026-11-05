import io
import json
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from google.oauth2.credentials import Credentials

from bmauto.core import Store
from bmauto.main import Bot
from bmauto.oauth import GoogleConnections, ProjectGoogle
from bmauto.oauth_server import make_handler
from bmauto.google import SCOPES
from bmauto.vk import keyboard
from test_projects import Conversation
from test_workflow import FakeGoogle, FakeLLM, FakeVK


@pytest.fixture
def connection(tmp_path, project):
    store = Store(tmp_path / "data")
    store.seed_projects([project, {**project, "id": "other", "name": "Другой"}])
    store.ensure_owners(1)
    config = {"web": {"client_id": "fake-client", "client_secret": "fake-secret",
                      "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}
    client_file = tmp_path / "credentials-web.json"
    client_file.write_text(json.dumps(config))
    return GoogleConnections(store, {1}, str(client_file), "https://bot.itiscat.ru/oauth/google/callback", str(tmp_path / "token.json"))


def state_of(url):
    return parse_qs(urlsplit(url).query)["state"][0]


def token_flow(monkeypatch, connection, refresh="fake-refresh", scopes=None):
    calls = []
    credentials = Credentials(token="fake-token", refresh_token=refresh,
                              token_uri="https://oauth2.googleapis.com/token", client_id="fake-client",
                              client_secret="fake-secret", scopes=SCOPES,
                              granted_scopes=scopes if scopes is not None else SCOPES)
    class FakeFlow:
        code_verifier = "fake-pkce-verifier"
        def fetch_token(self, **kwargs):
            calls.append(kwargs)
    flow = FakeFlow()
    flow.credentials = credentials
    monkeypatch.setattr(connection, "flow", lambda **kwargs: flow)
    return calls


def test_authorization_url_and_pkce_are_bound_to_project(connection):
    url = connection.begin("test", 1)
    params = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "accounts.google.com"
    assert params["redirect_uri"] == ["https://bot.itiscat.ru/oauth/google/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert set(params["scope"][0].split()) == set(SCOPES)
    row = connection.db.execute("SELECT state_hash,project,actor,verifier FROM flows").fetchone()
    assert row[0] != state_of(url) and row[1:3] == ("test", 1) and row[3]


def test_callback_connects_one_project_only_and_rejects_replay(connection, monkeypatch):
    state = state_of(connection.begin("test", 1))
    before_other = connection.store.projects()["other"].copy()
    calls = token_flow(monkeypatch, connection)
    assert connection.finish(state, "fake-code") == "Test"
    project = connection.store.projects()["test"]
    assert project["google_connection_id"] and project["draft_scope"]
    assert connection.store.projects()["other"] == before_other
    path = connection.path_for(project)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(connection.root.stat().st_mode) == 0o700
    assert calls == [{"code": "fake-code", "timeout": 30}]
    assert json.loads(path.read_text())["type"] == "authorized_user"
    with pytest.raises(ValueError, match="уже использована"):
        connection.finish(state, "fake-code")


def test_two_google_accounts_are_routed_independently(connection, monkeypatch):
    first = state_of(connection.begin("test", 1))
    second = state_of(connection.begin("other", 1))
    token_flow(monkeypatch, connection, refresh="account-A")
    connection.finish(first, "code-A")
    token_flow(monkeypatch, connection, refresh="account-B")
    connection.finish(second, "code-B")
    router = ProjectGoogle(connection)
    a, b = [connection.store.projects()[key] for key in ("test", "other")]
    client_a, client_b = router.for_project(a), router.for_project(b)
    assert client_a.credentials_file != client_b.credentials_file
    assert json.loads(connection.path_for(a).read_text())["refresh_token"] == "account-A"
    assert json.loads(connection.path_for(b).read_text())["refresh_token"] == "account-B"
    assert router.for_project(a) is client_a


def test_newer_auth_link_expires_previous_one(connection):
    old = state_of(connection.begin("test", 1))
    new = state_of(connection.begin("test", 1))
    assert old != new
    with pytest.raises(ValueError, match="истекла"):
        connection.finish(old, "unused-code")


def test_expiry_and_google_cancellation_do_not_change_project(connection):
    state = state_of(connection.begin("test", 1))
    with connection.db:
        connection.db.execute("UPDATE flows SET expires=?", (time.time() - 1,))
    with pytest.raises(ValueError, match="истекла"):
        connection.finish(state, "code")
    state = state_of(connection.begin("test", 1))
    with pytest.raises(ValueError, match="отменено"):
        connection.finish(state, error="access_denied")
    assert not connection.store.projects()["test"].get("google_connection_id")


def test_member_can_connect_google_and_revocation_is_checked_on_callback(connection, monkeypatch):
    connection.store.add_member("test", 1, 3, {1})
    member_state = state_of(connection.begin("test", 3))
    connection.store.remove_member("test", 1, 3, {1})
    with pytest.raises(ValueError, match="больше нет прав"):
        connection.finish(member_state, "unused-code")
    connection.store.db.execute("UPDATE project_members SET user_id=4 WHERE project_id='test' AND role='owner'")
    connection.store.db.commit()
    state = state_of(connection.begin("test", 4))
    connection.store.db.execute("DELETE FROM project_members WHERE project_id='test' AND user_id=4")
    connection.store.db.commit()
    with pytest.raises(ValueError, match="больше нет прав"):
        connection.finish(state, "code")


@pytest.mark.parametrize("scopes,refresh,error", [(SCOPES[:1], "refresh", "разрешить доступ"), (SCOPES, None, "refresh token")])
def test_incomplete_consent_does_not_replace_existing_account(connection, monkeypatch, scopes, refresh, error):
    state = state_of(connection.begin("test", 1))
    token_flow(monkeypatch, connection, refresh=refresh, scopes=scopes)
    with pytest.raises(ValueError, match=error):
        connection.finish(state, "code")
    assert not connection.store.projects()["test"].get("google_connection_id")
    assert not list(connection.root.glob("*.json"))


def test_legacy_global_account_is_not_inherited_by_new_projects(connection):
    connection.legacy_file.write_text("{}")
    assert connection.path_for(connection.store.projects()["test"]) == connection.legacy_file
    fresh = connection.store.save_project({"id": "new", "name": "New"}, owner=1)
    with pytest.raises(ValueError, match="не подключён"):
        connection.path_for(fresh)
    restarted = GoogleConnections(connection.store, {1}, connection.client_file, connection.redirect_uri, str(connection.legacy_file))
    assert not restarted.connected(fresh)


def test_bot_oauth_button_is_only_in_settings_and_uses_vk_link(connection):
    vk = FakeVK()
    bot = Bot({}, connection.store, vk, FakeGoogle(), FakeLLM(), {1}, connection)
    chat = Conversation(bot)
    chat.send("project", p="test")
    buttons = keyboard(vk.messages[-1][1])["buttons"]
    assert all(b["action"]["label"] != "Подключить Google" for row in buttons for b in row)
    chat.send("settings")
    assert vk.messages[-1][1][0] == ("Подключить Google", {"a": "google_connect"})
    chat.send("google_connect")
    sent = keyboard(vk.messages[-1][1])["buttons"]
    assert sent[0][0]["action"]["type"] == "open_link"
    assert urlsplit(sent[0][0]["action"]["link"]).hostname == "accounts.google.com"
    assert "color" not in sent[0][0]


def test_callback_handler_escapes_html_and_does_not_log_codes():
    seen = []
    class Connection:
        def finish(self, *args):
            seen.append(args)
            return "<script>alert(1)</script>"
        def close(self):
            pass
    connection = Connection()
    connection.db = connection
    connection.store = connection
    handler_class = make_handler(lambda: connection)
    handler = handler_class.__new__(handler_class)
    handler.path = "/oauth/google/callback?state=fake-state&code=fake-code"
    handler.wfile = io.BytesIO()
    handler.send_response = lambda status: seen.append(status)
    headers = {}
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler.do_GET()
    body = handler.wfile.getvalue().decode()
    assert seen[0] == ("fake-state", "fake-code", None)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert headers["Cache-Control"] == "no-store"
    assert "fake-code" not in body


def test_superseded_callback_after_exchange_cannot_bind_account(connection, monkeypatch):
    state = state_of(connection.begin("test", 1))
    token_flow(monkeypatch, connection)
    flow = connection.flow()
    def newer_attempt(**kwargs):
        with connection.db:
            connection.db.execute("UPDATE latest SET state_hash='newer-attempt' WHERE project='test'")
    flow.fetch_token = newer_attempt
    with pytest.raises(ValueError, match="более новое"):
        connection.finish(state, "code")
    assert not connection.store.projects()["test"].get("google_connection_id")
    assert not list(connection.root.glob("*.json"))


def test_project_workflow_uses_account_selected_for_that_project(connection, monkeypatch):
    state_a = state_of(connection.begin("test", 1))
    state_b = state_of(connection.begin("other", 1))
    token_flow(monkeypatch, connection, refresh="account-A")
    connection.finish(state_a, "code-A")
    token_flow(monkeypatch, connection, refresh="account-B")
    connection.finish(state_b, "code-B")
    clients = {}
    def google_client(path):
        client = FakeGoogle()
        clients[json.loads(Path(path).read_text())["refresh_token"]] = client
        return client
    monkeypatch.setattr("bmauto.oauth.Google", google_client)
    vk = FakeVK()
    bot = Bot({}, connection.store, vk, ProjectGoogle(connection), FakeLLM(), {1}, connection)
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    chat.send("generate")
    chat.send(text="Добавьте цену")
    chat.send("publish")
    chat.send("project", p="other")
    assert clients["account-A"].created == ["publication.docx"]
    assert clients["account-B"].created == []


@pytest.mark.parametrize("uri", ["http://bot.itiscat.ru/oauth/google/callback", "https://bot.itiscat.ru/wrong", "https://bot.itiscat.ru/oauth/google/callback?redirect=evil"])
def test_callback_configuration_requires_fixed_https_path(connection, uri):
    connection.redirect_uri = uri
    with pytest.raises(ValueError, match="GOOGLE_OAUTH_REDIRECT_URI"):
        connection.begin("test", 1)


def test_assigned_member_connects_own_google_from_vk_settings(connection, monkeypatch):
    from bmauto.vk import VK
    vk = FakeVK()
    vk.resolve_user = lambda value: VK.resolve_user(vk, value)
    bot = Bot({}, connection.store, vk, FakeGoogle(), FakeLLM(), {1}, connection)
    owner = Conversation(bot, 1)
    owner.send("project", p="test")
    owner.send("settings")
    assert vk.messages[-1][1][2] == ("Участники", {"a": "members"})
    owner.send("member_add")
    owner.send(text="3")
    member = Conversation(bot, 3)
    member.send(text="Начать")
    assert vk.messages[-1][1] == [("Test", {"a": "project", "p": "test"})]
    member.send("project", p="test")
    member.send("settings")
    assert vk.messages[-1][1] == [("Подключить Google", {"a": "google_connect"}), ("Назад", {"a": "project_home"})]
    member.send("google_connect")
    url = vk.messages[-1][1][0][1]["open_link"]
    assert "всеми участниками проекта" in vk.messages[-1][0]
    token_flow(monkeypatch, connection, refresh="member-account")
    connection.finish(state_of(url), "member-code")
    updated = connection.store.projects()["test"]
    assert updated["google_connected_by"] == 3
    assert json.loads(connection.path_for(updated).read_text())["refresh_token"] == "member-account"
    assert not connection.store.projects()["other"].get("google_connection_id")
    member.send("settings")
    assert vk.messages[-1][1][0][0] == "Сменить Google-аккаунт"


def test_project_member_cannot_connect_google_to_another_project(connection):
    connection.store.add_member("test", 1, 3, {1})
    with pytest.raises(ValueError, match="участники"):
        connection.begin("other", 3)
    with pytest.raises(ValueError, match="участники"):
        connection.begin("test", 99)


def test_removed_member_cannot_bind_google_even_if_token_exchange_started(connection, monkeypatch):
    connection.store.add_member("test", 1, 3, {1})
    state = state_of(connection.begin("test", 3))
    token_flow(monkeypatch, connection)
    flow = connection.flow()
    flow.fetch_token = lambda **kwargs: connection.store.remove_member("test", 1, 3, {1})
    with pytest.raises(ValueError, match="Доступ к проекту изменился"):
        connection.finish(state, "member-code")
    assert not connection.store.projects()["test"].get("google_connection_id")
    assert not list(connection.root.glob("*.json"))
