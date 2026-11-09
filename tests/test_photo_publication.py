import pytest

from bmauto.core import Store, assignment_cell
from bmauto.google import publish
from test_projects import Conversation, actions, bot_at
from test_workflow import FakeGoogle, task


@pytest.mark.parametrize("with_text", [False, True])
def test_one_photo_is_sent_from_task_menu_after_saving(tmp_path, project, with_text):
    bot = bot_at(tmp_path, {"projects": [project]})
    chat = Conversation(bot)
    chat.send("project", p="test")
    chat.send("task", i=0)
    if with_text:
        key, draft = bot.draft_for(bot.store.session(1), project)
        draft["text"] = "Готовый текст"
        bot.store.save_draft(key, draft)
    chat.send("photos")
    session = bot.store.session(1)
    bot.handle(dict(id=session["last_message"] + 1, from_id=1, peer_id=1, text="", attachments=[
        {"type": "photo", "photo": {"owner_id": 1, "id": 5, "sizes": [{"width": 100, "height": 100, "url": "photo"}]}}
    ]))
    assert "publish" not in actions(bot)
    chat.send("done")
    assert "publish" in actions(bot)
    chat.send("publish")
    assert bot.google.created == (["publication.docx"] if with_text else []) + ["photo_01.jpg"]
    assert bot.store.session(1)["mode"] is None
    assert bot.store.session(1)["view"] == "task"
    description, url = assignment_cell(bot.google.table[2][3])
    assert description == "Текст" and url
    assert "publish" not in actions(bot)


def test_photo_only_retry_does_not_duplicate_uploads(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    (tmp_path / "photo.jpg").write_bytes(b"photo")
    draft = store.draft("draft")
    draft["photos"] = [{"id": "photo", "path": "photo.jpg"}]
    store.save_draft("draft", draft)
    selected = task(google, project)
    google.fail_link = True
    with pytest.raises(RuntimeError):
        publish(google, store, "draft", project, selected)
    google.fail_link = False
    url = publish(google, Store(tmp_path), "draft", project, selected)
    assert google.created == ["photo_01.jpg"]
    assert google.table[2][3].endswith(url)
    assert store.draft("draft")["published"]


def test_empty_draft_and_more_than_nine_photos_are_rejected_before_upload(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    selected = task(google, project)
    with pytest.raises(ValueError, match="хотя бы одно фото"):
        publish(google, store, "draft", project, selected)
    draft = store.draft("draft")
    draft["photos"] = [{"id": str(i), "path": "unused"} for i in range(10)]
    store.save_draft("draft", draft)
    with pytest.raises(ValueError, match="не более 9"):
        publish(google, store, "draft", project, selected)
    assert not google.files
