import io

import httpx
import pytest
from docx import Document

from bmauto.core import Store
from bmauto.google import Google
from bmauto.vk import VK, keyboard
from test_projects import Conversation, actions, bot_at


def selected_bot(tmp_path, project):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p=project["id"])
    chat.send("task", i=0)
    return bot, chat


def photo(number):
    return {"type": "photo", "photo": {"owner_id": 1, "id": number,
            "sizes": [{"width": 100, "height": 100, "url": str(number)}]}}


def send_photos(bot, *numbers, **ids):
    message = dict(id=bot.store.session(1)["last_message"] + 1, from_id=1, peer_id=1,
                   text="", attachments=[photo(n) for n in numbers])
    message.update(ids)
    bot.handle(message)
    return message


def test_edit_published_text_keeps_folder_and_file_ids_after_restart(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    chat.send("edit_text")
    chat.send(text="Первая версия")
    chat.send("publish")
    key, before = bot.draft_for(bot.store.session(1), project)
    url = bot.google.table[2][3]
    chat.send("edit_text")
    chat.send(text="Исправленный текст")
    bot.store = Store(tmp_path)
    assert bot.store.draft(key)["dirty"]
    assert "publish" in actions(bot)
    chat.send("publish")
    after = bot.store.draft(key)
    assert after["folder"] == before["folder"]
    assert after["uploads"]["publication.docx"]["id"] == before["uploads"]["publication.docx"]["id"]
    content = bot.google.files[after["uploads"]["publication.docx"]["id"]]
    assert Document(io.BytesIO(content)).paragraphs[0].text == "Исправленный текст"
    assert bot.google.created == ["publication.docx"]
    assert bot.google.table[2][3] == url
    assert not after["dirty"]


def test_regenerate_after_publication(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    chat.send("edit_text")
    chat.send(text="Старый текст")
    chat.send("publish")
    chat.send("generate")
    chat.send(text="Добавьте цену")
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert draft["text"] == "Сгенерированный текст" and draft["dirty"]
    chat.send("publish")
    assert bot.google.created == ["publication.docx"]


def test_nine_photos_in_single_message_and_duplicates(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    chat.send("photos")
    message = send_photos(bot, *range(9), id=0, conversation_message_id=20)
    bot.handle(message)
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 9
    assert len(keyboard(bot.vk.messages[-1][1])["buttons"]) <= 10
    chat.send("done")
    chat.send("publish")
    assert len(bot.google.created) == 9


def test_photo_series_zero_ids_out_of_order_and_after_publication(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    send_photos(bot, 1, id=0, conversation_message_id=30)
    chat.send("publish")
    key, before = bot.draft_for(bot.store.session(1), project)
    send_photos(bot, 2, id=0, conversation_message_id=32)
    send_photos(bot, 3, id=0, conversation_message_id=31)
    assert len(bot.store.draft(key)["photos"]) == 3
    chat.send("publish")
    assert len(bot.google.created) == 3
    assert bot.store.draft(key)["folder"] == before["folder"]


def test_one_failed_photo_does_not_discard_remaining_and_can_retry(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    chat.send("photos")
    original = bot.vk.download_photo

    def download(url):
        if url == "2":
            raise httpx.ReadTimeout("unavailable")
        return original(url)

    bot.vk.download_photo = download
    message = send_photos(bot, 1, 2, 3)
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert [p["id"] for p in draft["photos"]] == ["1_1", "1_3"]
    assert "Не удалось скачать: 1" in bot.vk.messages[-1][0]
    bot.vk.download_photo = original
    bot.handle(message)
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 3


def test_replace_and_remove_published_photos_only_trashes_managed_files(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    bot.vk.download_photo = lambda url: b"\xff\xd8" + url.encode()
    send_photos(bot, 1, 2, 3)
    chat.send("publish")
    key, before = bot.draft_for(bot.store.session(1), project)
    bot.google.files["external"] = b"user file"
    chat.send("photos")
    chat.send("remove_photo", id="1_2")
    chat.send("done")
    chat.send("publish")
    after = bot.store.draft(key)
    assert bot.google.files[after["uploads"]["photo_02.jpg"]["id"]] == b"\xff\xd83"
    assert before["uploads"]["photo_03.jpg"]["id"] not in bot.google.files
    assert bot.google.files["external"] == b"user file"
    chat.send("photos")
    chat.send("clear_photos")
    send_photos(bot, 4)
    chat.send("done")
    chat.send("publish")
    assert list(bot.store.draft(key)["uploads"]) == ["photo_01.jpg"]
    assert bot.google.files[before["uploads"]["photo_01.jpg"]["id"]] == b"\xff\xd84"


def test_edit_retry_after_sheets_failure_does_not_duplicate_files(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    send_photos(bot, 1)
    chat.send("publish")
    send_photos(bot, 2)
    bot.google.fail_link = True
    chat.send("publish")
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert draft["dirty"]
    bot.google.fail_link = False
    chat.send("publish")
    assert bot.google.created == ["photo_01.jpg", "photo_02.jpg"]
    assert not bot.store.draft(key)["dirty"]


def test_clear_all_photos_keeps_published_text(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    chat.send("edit_text")
    chat.send(text="Текст остаётся")
    send_photos(bot, 1, 2)
    chat.send("publish")
    chat.send("photos")
    chat.send("clear_photos")
    chat.send("done")
    chat.send("publish")
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert set(draft["uploads"]) == {"publication.docx"}
    assert draft["text"] == "Текст остаётся"


def test_out_of_order_positive_ids_are_not_lost_and_dedupe_survives_restart(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    send_photos(bot, 1, id=100)
    send_photos(bot, 2, id=99)
    message = send_photos(bot, 3, id=101)
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 3
    bot.store = Store(tmp_path)
    count = len(bot.vk.messages)
    bot.handle(message)
    assert len(bot.vk.messages) == count
    assert len(bot.store.draft(key)["photos"]) == 3


@pytest.mark.parametrize("data,extension,mime", [
    (b"\xff\xd8jpeg", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\npng", "png", "image/png"),
    (b"RIFF1234WEBPwebp", "webp", "image/webp"),
])
def test_photo_formats_preserved_on_drive(tmp_path, project, data, extension, mime):
    bot, chat = selected_bot(tmp_path, project)
    bot.vk.download_photo = lambda url: data
    send_photos(bot, 1)
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert draft["photos"][0]["mime"] == mime
    chat.send("publish")
    assert bot.google.created == [f"photo_01.{extension}"]


def test_google_media_content_type_and_trash_request():
    requests = []
    google = Google("unused")
    google.credentials = type("Credentials", (), {"valid": True, "token": "test"})()
    google.http = httpx.Client(transport=httpx.MockTransport(lambda request: (requests.append(request) or httpx.Response(200, json={}))))
    google.upload_content("file", b"bytes", "image/png")
    google.trash("obsolete")
    assert requests[0].headers["content-type"] == "image/png"
    assert requests[0].headers["authorization"] == "Bearer test"
    assert requests[1].content == b'{"trashed":true}'


def test_photo_receiver_accepts_png_from_vk_cdn():
    vk = VK("test", 1)
    data = b"\x89PNG\r\n\x1a\nimage"
    vk.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data, headers={"content-type": "image/png"})))
    assert vk.download_photo("https://sun.userapi.com/photo") == data
