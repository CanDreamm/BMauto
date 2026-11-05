import io
from datetime import date

import pytest
from docx import Document

from bmauto.core import Store, column_letter, tasks_from_rows
from bmauto.google import publish, word_document
from bmauto.main import Bot


class FakeGoogle:
    def __init__(self):
        self.table = [["Дата", "День недели", "Задание", "Ссылка"], ["20.12.2030", "Пятница", "Текст", ""]]
        self.files = {}
        self.created = []
        self.shared = []
        self.count = 0
        self.fail_link = False

    def rows(self, project):
        return self.table

    def new_id(self):
        self.count += 1
        return str(self.count)

    def exists(self, file_id):
        return file_id in self.files

    def folder(self, file_id, parent, name):
        self.files.setdefault(file_id, name)

    def upload(self, file_id, parent, name, content, mime):
        self.created.append(name)
        self.files[file_id] = content

    def upload_content(self, file_id, content, mime):
        self.files[file_id] = content

    def share(self, file_id):
        self.shared.append(file_id)

    def set_link(self, project, row, headers, url):
        if self.fail_link:
            raise RuntimeError("temporary Sheets failure")
        self.table[row - 1][3] = url


def task(google, project):
    t = tasks_from_rows(google.table, project, date(2026, 1, 1))[0]
    return dict(row=t.row, day=t.day.isoformat(), fields=t.fields)


def test_future_dates_and_all_fields(project):
    rows = [["Дата", "День недели", "Ссылка", "Задание"],
            ["01.01.2026", "Четверг"], ["02.01.2026", "Пятница", "", "Фото машин"], ["", "", "", "skip"]]
    tasks = tasks_from_rows(rows, project, date(2026, 1, 1))
    assert len(tasks) == 1 and tasks[0].row == 3
    assert "Задание: Фото машин" in tasks[0].description()


def test_invalid_date_is_visible(project):
    with pytest.raises(ValueError, match="Строка 2"):
        tasks_from_rows([["Дата", "День недели", "Ссылка"], ["31.02.2030"]], project, date.today())


def test_duplicate_headers_rejected(project):
    with pytest.raises(ValueError):
        tasks_from_rows([["Дата", "Дата", "Ссылка"]], project, date.today())


def test_word_contains_text():
    doc = Document(io.BytesIO(word_document("Публикация\nВторая строка")))
    assert [p.text for p in doc.paragraphs] == ["Публикация", "Вторая строка"]


def test_store_survives_restart(tmp_path):
    store = Store(tmp_path)
    store.save_session(1, {"project": "test"})
    draft = store.draft("key")
    draft["text"] = "Saved"
    store.save_draft("key", draft)
    assert Store(tmp_path).draft("key")["text"] == "Saved"
    assert Store(tmp_path).session(1)["project"] == "test"


def test_publish_retry_does_not_duplicate_files(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    chosen = task(google, project)
    draft = store.draft("key")
    draft["text"] = "Готовый текст"
    (tmp_path / "photo.jpg").write_bytes(b"image")
    draft["photos"] = [{"path": "photo.jpg", "id": "p"}]
    store.save_draft("key", draft)
    google.fail_link = True
    with pytest.raises(RuntimeError):
        publish(google, store, "key", project, chosen)
    google.fail_link = False
    url = publish(google, Store(tmp_path), "key", project, chosen)
    assert publish(google, store, "key", project, chosen) == url
    assert google.created == ["publication.docx", "photo_01.jpg"]
    assert google.table[1][3] == url
    assert google.shared


def test_changed_task_not_published(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    chosen = task(google, project)
    draft = store.draft("key")
    draft["text"] = "Text"
    store.save_draft("key", draft)
    google.table[1][2] = "Changed"
    with pytest.raises(ValueError, match="изменилось"):
        publish(google, store, "key", project, chosen)
    assert not google.files


def test_existing_link_not_overwritten(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    chosen = task(google, project)
    draft = store.draft("key")
    draft["text"] = "Text"
    store.save_draft("key", draft)
    google.table[1][3] = "https://existing.example"
    with pytest.raises(ValueError, match="перезапись"):
        publish(google, store, "key", project, chosen)


class FakeVK:
    def __init__(self):
        self.messages = []

    def send(self, user, text, buttons=None):
        self.messages.append((text, buttons))

    def download_photo(self, url):
        return b"image"


class FakeLLM:
    def generate(self, project, task, supplement):
        assert project["concept"] == "Concept" and project["prompt"] == "Prompt"
        assert "Задание: Текст" in task and supplement == "Добавьте цену"
        return "Сгенерированный текст"


def test_vk_full_flow_and_photo_limit(tmp_path, project):
    vk, store, google = FakeVK(), Store(tmp_path), FakeGoogle()
    bot = Bot({"projects": [project]}, store, vk, google, FakeLLM(), {1})
    import json
    counter = 0

    def send(action=None, text="", attachments=None, **payload):
        nonlocal counter
        counter += 1
        bot.handle(dict(id=counter, from_id=1, peer_id=1, text=text,
                        payload=json.dumps({"a": action, **payload}) if action else "",
                        attachments=attachments or []))

    send(text="Начать")
    send("project", p="test")
    send("dates")
    send("task", i=0)
    assert "Задание: Текст" in vk.messages[-1][0]
    send("generate")
    send(text="Добавьте цену")
    send("photos")
    photos = [dict(type="photo", photo=dict(id=i, owner_id=1, sizes=[dict(width=10, height=10, url="photo")])) for i in range(10)]
    send(attachments=photos)
    assert "не сохранена" in vk.messages[-1][0]
    send(attachments=photos[:9])
    assert "9/9" in vk.messages[-1][0]
    send("done")
    send("publish")
    assert "Ссылка записана" in vk.messages[-1][0]
    assert len(google.created) == 10
    bot.handle(dict(id=counter, from_id=1, peer_id=1))  # Duplicate VK delivery.
    assert len(google.created) == 10


def test_access_denied(tmp_path, project):
    vk = FakeVK()
    Bot({"projects": [project]}, Store(tmp_path), vk, FakeGoogle(), FakeLLM(), {1}).handle(
        dict(id=1, from_id=2, peer_id=2, text="Начать"))
    assert not vk.messages


@pytest.mark.parametrize("index, expected", [(1, "A"), (26, "Z"), (27, "AA"), (703, "AAA")])
def test_sheet_columns(index, expected):
    assert column_letter(index) == expected


def test_google_uses_sheets_host_and_escaped_tab(project):
    import httpx
    from bmauto.google import Google

    class Credentials:
        valid = True
        token = "test-token"

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"values": [["Дата"]]})

    google = Google.__new__(Google)
    google.credentials = Credentials()
    google.http = httpx.Client(transport=httpx.MockTransport(respond))
    project["worksheet"] = "План 'осень'"
    google.rows(project)
    google.set_link(project, 2, ["Дата", "Ссылка"], "https://example.test/folder")
    assert all(r.url.host == "sheets.googleapis.com" for r in requests)
    assert requests[0].url.path.endswith("/'План ''осень'''" )
    assert requests[1].url.path.endswith("!B2")
    assert requests[1].url.params["valueInputOption"] == "RAW"
