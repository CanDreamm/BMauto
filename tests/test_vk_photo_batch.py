from types import SimpleNamespace

import pytest

from bmauto.vk import VK
from test_draft_editing import selected_bot


def attachment(number, owner=0, photo_id=0):
    return {"type": "photo", "photo": {"owner_id": owner, "id": photo_id,
        "sizes": [{"width": 100, "height": 100, "url": f"photo-{number}"}]}}


def message(bot, photos, conversation_id=100):
    return {"id": 0, "conversation_message_id": conversation_id, "peer_id": 1, "from_id": 1,
            "text": "", "attachments": photos}


def distinct_download(url):
    return b"\xff\xd8" + url.encode()


@pytest.mark.parametrize("owner,photo_id", [(0, 0), (123, 0), (None, None), (-123, -1)])
def test_three_temporary_photos_with_same_ids_are_all_saved_and_published(tmp_path, project, owner, photo_id):
    bot, chat = selected_bot(tmp_path, project)
    bot.vk.download_photo = distinct_download
    incoming = message(bot, [attachment(i, owner, photo_id) for i in range(3)])
    bot.handle(incoming)
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 3
    assert len({p["id"] for p in draft["photos"]}) == 3
    assert "Получено от ВК: 3 фото. Сохранено 3/9" in bot.vk.messages[-1][0]
    bot.handle(incoming)
    assert len(bot.store.draft(key)["photos"]) == 3
    chat.send("publish")
    assert bot.google.created == ["photo_01.jpg", "photo_02.jpg", "photo_03.jpg"]
    assert {bot.google.files[entry["id"]] for entry in bot.store.draft(key)["uploads"].values()} == {
        distinct_download(f"photo-{i}") for i in range(3)}


def test_full_message_lookup_recovers_three_photos_from_partial_long_poll(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    full = message(bot, [attachment(i) for i in range(3)])
    lookups = []

    def call(method, **kwargs):
        lookups.append((method, kwargs))
        return {"items": [full]}

    api = SimpleNamespace(call=call)
    bot.vk.full_photo_message = lambda incoming: VK.full_photo_message(api, incoming)
    bot.vk.download_photo = distinct_download
    bot.handle(message(bot, [attachment(0)]))
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 3
    assert lookups == [("messages.getByConversationMessageId", {"peer_id": 1, "conversation_message_ids": 100})]


def test_expanded_delivery_of_same_message_is_not_discarded(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    bot.vk.download_photo = distinct_download
    bot.handle(message(bot, [attachment(0)]))
    bot.handle(message(bot, [attachment(i) for i in range(3)]))
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 3


@pytest.mark.parametrize("count", [3, 9])
@pytest.mark.parametrize("resolved_ids", [False, True])
def test_resend_old_collapsed_batch_recovers_missing_photos_without_duplicate(tmp_path, project, count, resolved_ids):
    bot, chat = selected_bot(tmp_path, project)
    key, draft = bot.draft_for(bot.store.session(1), project)
    path = bot.store.root / "legacy.jpg"
    path.write_bytes(distinct_download("photo-0"))
    draft["photos"] = [{"id": "1_0", "path": "legacy.jpg"}]
    bot.store.save_draft(key, draft)
    bot.vk.download_photo = distinct_download
    bot.handle(message(bot, [attachment(i, 1, i + 1 if resolved_ids else 0) for i in range(count)]))
    draft = bot.store.draft(key)
    assert len(draft["photos"]) == count
    assert len({(bot.store.root / p["path"]).read_bytes() for p in draft["photos"]}) == count


def test_full_message_lookup_rejects_other_peer_and_does_not_drop_event_photos():
    incoming = {"id": 7, "conversation_message_id": 8, "from_id": 1, "peer_id": 1,
                "attachments": [attachment(1), attachment(2)]}
    other = {**incoming, "peer_id": 2}
    calls = []

    def call(method, **kwargs):
        calls.append(method)
        return {"items": [other]}

    actual = VK.full_photo_message(SimpleNamespace(call=call), incoming)
    assert actual["attachments"] == incoming["attachments"]
    assert actual["_photo_lookup_failed"]
    assert calls == ["messages.getByConversationMessageId", "messages.getById"]


def test_get_by_id_fallback_and_smaller_reply_cannot_remove_photos():
    incoming = {"id": 7, "conversation_message_id": 8, "from_id": 1, "peer_id": 1,
                "attachments": [attachment(1), attachment(2)]}
    smaller = {**incoming, "attachments": [attachment(1)]}
    def call(method, **kwargs):
        return {"items": [smaller if method.endswith("ConversationMessageId") else incoming]}
    actual = VK.full_photo_message(SimpleNamespace(call=call), incoming)
    assert actual["attachments"] == incoming["attachments"]
    assert not actual.get("_photo_lookup_failed")


def test_lookup_failure_is_visible_and_retry_can_get_missing_photos(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    bot.vk.download_photo = distinct_download
    def fail(method, **kwargs):
        raise ValueError("VK API error")
    api = SimpleNamespace(call=fail)
    bot.vk.full_photo_message = lambda incoming: VK.full_photo_message(api, incoming)
    partial = message(bot, [attachment(0)])
    bot.handle(partial)
    assert "Не удалось проверить полный список вложений" in bot.vk.messages[-1][0]
    full = {**partial, "attachments": [attachment(i) for i in range(3)]}
    api.call = lambda method, **kwargs: {"items": [full]}
    bot.handle(partial)
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert len(draft["photos"]) == 3
