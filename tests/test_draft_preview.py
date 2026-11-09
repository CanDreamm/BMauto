import json

import httpx
import pytest

from bmauto.vk import VK
from test_draft_editing import selected_bot, send_photos
from test_projects import actions


@pytest.mark.parametrize("with_text", [False, True])
def test_preview_sends_every_saved_photo_and_actual_text(tmp_path, project, with_text):
    bot, chat = selected_bot(tmp_path, project)
    if with_text:
        chat.send("edit_text")
        chat.send(text="Полный текст публикации\nВторая строка")
    send_photos(bot, 1, 2, 3, 4, 5)
    key, draft = bot.draft_for(bot.store.session(1), project)
    chat.send("preview")
    user, text, photos = bot.vk.previews[-1]
    assert user == 1
    assert text == ("Полный текст публикации\nВторая строка" if with_text else "Текст пока не создан")
    assert [path for path, _ in photos] == [bot.store.root / photo["path"] for photo in draft["photos"]]
    assert len(photos) == 5 and all(path.is_file() for path, _ in photos)
    assert "preview" in actions(bot) and "publish" in actions(bot)
    assert bot.store.draft(key) == draft


def test_vk_uploads_all_nine_photos_and_sends_full_long_text_once(tmp_path):
    vk = VK("test", 1)
    calls, uploads, paths = [], [], []
    for i in range(9):
        path = tmp_path / f"photo_{i}.jpg"
        path.write_bytes(b"\xff\xd8" + str(i).encode())
        paths.append((path, "image/jpeg"))

    def respond(request):
        uploads.append(request.read())
        return httpx.Response(200, json={"server": 1, "photo": "uploaded", "hash": "test"})

    def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "photos.getMessagesUploadServer":
            assert kwargs["peer_id"] == 42
            return {"upload_url": "https://pu.vk.com/upload"}
        if method == "photos.saveMessagesPhoto":
            assert kwargs == {"server": 1, "photo": "uploaded", "hash": "test"}
            return [{"owner_id": -1, "id": len(uploads), "access_key": "key"}]
        assert method == "messages.send"
        return 1

    vk.call = call
    vk.http = httpx.Client(transport=httpx.MockTransport(respond))
    text = "Текст публикации\n" * 600
    buttons = [("Назад", {"a": "project_home"})]
    vk.send_draft(42, text, paths, buttons)
    assert len(uploads) == 9
    for i, uploaded in enumerate(uploads):
        assert f'filename="photo_{i}.jpg"'.encode() in uploaded
        assert b"Content-Type: image/jpeg" in uploaded
        assert b"\xff\xd8" + str(i).encode() in uploaded
    messages = [kwargs for method, kwargs in calls if method == "messages.send"]
    assert "".join(item["message"] for item in messages) == text
    assert messages[0]["attachment"].split(",") == [f"photo-1_{i}_key" for i in range(1, 10)]
    assert all("attachment" not in item for item in messages[1:])
    assert all("keyboard" not in item for item in messages[:-1])
    assert json.loads(messages[-1]["keyboard"])["buttons"][0][0]["action"]["label"] == "Назад"


def test_text_only_preview_does_not_upload_photos():
    vk = VK("test", 1)
    calls = []
    vk.call = lambda method, **kwargs: calls.append((method, kwargs))
    vk.send_draft(42, "Только текст", [], [])
    assert len(calls) == 1 and calls[0][0] == "messages.send"
    assert calls[0][1]["message"] == "Только текст"
    assert "attachment" not in calls[0][1]


def test_missing_local_photo_is_reported_before_upload(tmp_path):
    vk = VK("test", 1)
    vk.call = lambda *args, **kwargs: pytest.fail("Missing files must be checked before VK calls")
    with pytest.raises(ValueError, match="Не найден сохранённый файл"):
        vk.send_draft(42, "Текст", [(tmp_path / "missing.jpg", "image/jpeg")], [])


def test_vk_error_identifies_method_and_reason_without_token_or_request_params():
    from bmauto.vk import VKAPIError
    token = 'private-token-for-test'
    vk = VK(token, 1)
    vk.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'error': {'error_code': 100, 'error_msg': 'Invalid photo: '+token+' https://pu.vk.com/upload?key=secret',
                  'request_params': [{'key': 'access_token', 'value': token}]}})))
    with pytest.raises(VKAPIError) as caught:
        vk.call('photos.saveMessagesPhoto', photo='invalid')
    error = caught.value
    assert error.code == 100 and error.method == 'photos.saveMessagesPhoto'
    assert 'Invalid photo' in str(error)
    assert token not in str(error) and 'key=secret' not in str(error) and 'request_params' not in str(error)


@pytest.mark.parametrize('failure', ['zero_id', 'api_error'])
def test_photo_upload_uses_compatible_version_when_modern_api_rejects_photo(tmp_path, failure):
    from bmauto.vk import LEGACY_PHOTO_VERSION, VKAPIError
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'\xff\xd8image')
    vk = VK('test', 1)
    calls = []
    vk.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        'server': 1, 'photo': 'uploaded', 'hash': 'test'})))

    def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == 'photos.getMessagesUploadServer':
            return {'upload_url': 'https://pu.vk.com/upload'}
        if method == 'photos.saveMessagesPhoto':
            if kwargs.get('v') != LEGACY_PHOTO_VERSION:
                if failure == 'api_error':
                    raise VKAPIError(method, 100, 'Invalid photo')
                return [{'owner_id': 0, 'id': 0}]
            return [{'owner_id': -1, 'id': 42}]
        assert method == 'messages.send'

    vk.call = call
    vk.send_draft(42, 'Текст', [(path, 'image/jpeg')], [])
    assert calls[-1][1]['attachment'] == 'photo-1_42'
    assert any(method == 'photos.saveMessagesPhoto' and args.get('v') == LEGACY_PHOTO_VERSION for method, args in calls)


def test_attachment_message_retry_keeps_random_id():
    from bmauto.vk import LEGACY_PHOTO_VERSION, VKAPIError
    vk = VK('test', 1)
    calls = []
    def call(method, **kwargs):
        calls.append(kwargs)
        if kwargs.get('v') != LEGACY_PHOTO_VERSION:
            raise VKAPIError(method, 100, 'Invalid attachment')
    vk.call = call
    vk.send(42, 'Текст', attachments=['photo-1_42'])
    assert len(calls) == 2 and calls[0]['random_id'] == calls[1]['random_id']
    assert calls[1]['v'] == LEGACY_PHOTO_VERSION


def test_failed_photo_upload_still_sends_text_and_preserves_detailed_error(tmp_path):
    from bmauto.vk import VKAPIError
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'\xff\xd8image')
    vk = VK('test', 1)
    sent = []
    def upload(*args):
        raise VKAPIError('photos.getMessagesUploadServer', 100, 'Invalid peer_id')
    vk.upload_draft_photo = upload
    vk.call = lambda method, **kwargs: sent.append((method, kwargs))
    with pytest.raises(VKAPIError, match='photos.getMessagesUploadServer'):
        vk.send_draft(42, 'Полный текст черновика', [(path, 'image/jpeg')], [])
    assert sent[0][0] == 'messages.send'
    assert sent[0][1]['message'] == 'Полный текст черновика'
    assert 'attachment' not in sent[0][1]
