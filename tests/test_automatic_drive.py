import json
import re

import httpx
import pytest

from bmauto.core import Store, append_materials
from bmauto.google import Google, publish
from test_projects import Conversation, actions, bot_at
from test_workflow import FakeGoogle, task


class Drive:
    def __init__(self, account="account-A"):
        self.account, self.files, self.calls = account, {}, []
        self.counter = 0
        self.fail_root_once = False

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/about"):
            return httpx.Response(200, json={"user": {"permissionId": self.account}})
        if path.endswith("/generateIds"):
            self.counter += 1
            return httpx.Response(200, json={"ids": [f"{self.account}-{self.counter}"]})
        if path.endswith("/files") and request.method == "GET":
            query = request.url.params["q"]
            parent = re.search(r"'([^']+)' in parents", query)[1]
            name = re.search(r"and name='((?:\\.|[^'])*)'", query)[1].replace("\\'", "'").replace("\\\\", "\\")
            marker = re.search(r"value='((?:\\.|[^'])*)'", query)
            items = [{"id": key} for key, value in self.files.items()
                     if not value.get("trashed") and value["name"] == name and parent in value["parents"]
                     and (not marker or value.get("appProperties", {}).get("bmautoProject") == marker[1])]
            return httpx.Response(200, json={"files": items[:1]})
        if path.endswith("/files") and request.method == "POST":
            body = json.loads(request.content)
            assert body["id"] not in self.files
            self.files[body["id"]] = body
            if self.fail_root_once and body["name"] == "BMauto":
                self.fail_root_once = False
                return httpx.Response(503, json={"error": "reply lost after create"})
            return httpx.Response(200, json=body)
        file_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=self.files[file_id]) if file_id in self.files else httpx.Response(404, json={})

    def google(self):
        class Credentials:
            valid = True
            token = "fake"
        google = Google.__new__(Google)
        google.credentials = Credentials()
        google.http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return google


def test_drive_layout_is_automatic_and_reused_after_restart(tmp_path, project):
    drive, store = Drive(), Store(tmp_path)
    google = drive.google()
    child = google.project_folder(store, project)
    root_id = next(key for key, value in drive.files.items() if value["name"] == "BMauto")
    assert drive.files[root_id]["parents"] == ["root"]
    assert drive.files[child]["name"] == project["name"]
    assert drive.files[child]["parents"] == [root_id]
    assert drive.files[child]["appProperties"] == {"bmautoProject": "test"}
    assert drive.google().project_folder(Store(tmp_path), project) == child
    assert len(drive.files) == 2


def test_partial_folder_creation_retries_preallocated_id(tmp_path, project):
    drive, store = Drive(), Store(tmp_path)
    drive.fail_root_once = True
    with pytest.raises(httpx.HTTPStatusError):
        drive.google().project_folder(store, project)
    cached = store.drive_location("account-A", "root", "BMauto")
    assert cached is not None and not cached["ready"]
    child = drive.google().project_folder(Store(tmp_path), project)
    assert len(drive.files) == 2
    assert drive.files[child]["parents"] == [cached["id"]]


def test_projects_with_same_name_are_separate_but_share_bmauto_root(tmp_path, project):
    drive, store = Drive(), Store(tmp_path)
    a = drive.google().project_folder(store, project)
    b = drive.google().project_folder(store, {**project, "id": "another"})
    assert a != b
    assert drive.files[a]["parents"] == drive.files[b]["parents"]
    assert sum(file["name"] == "BMauto" for file in drive.files.values()) == 1


def test_google_accounts_have_independent_bmauto_roots(tmp_path, project):
    store = Store(tmp_path)
    a, b = Drive("account-A"), Drive("account-B")
    folder_a = a.google().project_folder(store, project)
    folder_b = b.google().project_folder(store, project)
    assert folder_a != folder_b
    assert len(a.files) == len(b.files) == 2


def test_existing_bmauto_root_is_reused_and_names_are_escaped(tmp_path, project):
    drive, store = Drive(), Store(tmp_path)
    drive.files["existing"] = {"name": "BMauto", "parents": ["root"]}
    child = drive.google().project_folder(store, {**project, "name": "O'Connor"})
    assert drive.files[child]["parents"] == ["existing"]
    assert drive.files[child]["name"] == "O'Connor"
    assert len(drive.files) == 2


def test_trashed_root_is_replaced_without_reusing_old_project_folder(tmp_path, project):
    drive, store = Drive(), Store(tmp_path)
    old_child = drive.google().project_folder(store, project)
    old_root = drive.files[old_child]["parents"][0]
    drive.files[old_root]["trashed"] = True
    child = drive.google().project_folder(store, project)
    assert child != old_child
    assert drive.files[child]["parents"] != [old_root]


def test_publication_always_shares_and_ignores_old_parent_setting(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    project.update(public_read=False, drive_parent_id="obsolete-parent")
    draft = store.draft("draft")
    draft["text"] = "Публикация"
    store.save_draft("draft", draft)
    selected = task(google, project)
    url = publish(google, store, "draft", project, selected)
    final = store.draft("draft")
    assert final["parent"] == "project-parent"
    assert google.shared == [final["folder"]]
    assert google.table[2][3] == append_materials("Текст", url)


def test_settings_only_show_essential_fields_without_pages(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("settings")
    buttons = bot.vk.messages[-1][1]
    rows = [item if isinstance(item, list) else [item] for item in buttons]
    fields = {p["field"] for row in rows for _, p in row if p["a"] == "project_edit"}
    assert fields == {"concept", "prompt", "spreadsheet_id"}
    assert "settings" not in actions(bot)
    assert len(rows) == 7


def test_publication_available_without_manual_parent_folder(tmp_path, project):
    project.pop("drive_parent_id")
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    key, draft = bot.draft_for(bot.store.session(1), project)
    draft["text"] = "Текст"
    bot.store.save_draft(key, draft)
    chat.send("task_home")
    assert "publish" in actions(bot)
