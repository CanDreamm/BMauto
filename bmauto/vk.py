import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

LEGACY_PHOTO_VERSION = "5.131"


class VKAPIError(ValueError):
    def __init__(self, method, code, detail):
        self.method, self.code = method, code
        super().__init__(f"Ошибка VK API {code} ({method}): {detail}")


def photo_size(photo):
    sizes = photo.get("sizes") or []
    if photo.get("orig_photo", {}).get("url"):
        sizes = [*sizes, photo["orig_photo"]]
    if not sizes:
        raise ValueError("ВК не передал адрес изображения")
    return max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))


def photo_identity(photo):
    owner, number = photo.get("owner_id"), photo.get("id")
    if isinstance(owner, int) and owner != 0 and isinstance(number, int) and number > 0:
        return f"{owner}_{number}"
    # Temporary VK photos can all have owner_id/id=0. Never collapse them by ID.
    source = photo_size(photo)["url"]
    return "source_" + hashlib.sha256(source.encode()).hexdigest()


def photo_format(content):
    if content.startswith(b"\xff\xd8"):
        return "jpg", "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ValueError("Прикрепите фото JPEG, PNG или WebP")


def button_color(label, payload):
    if label in {"Назад", "Меню проекта", "Настройки", "Все проекты"}:
        return "primary"
    if label == "Создать проект":
        return "positive"
    return payload.get("_color", "secondary")


def back_footer(back, action=None):
    return [("Назад", back)] + ([action] if action else [])


def keyboard(buttons):
    rows = [item if isinstance(item, list) else [item] for item in buttons]
    def action(label, payload):
        return {"type": "callback", "label": label[:40],
                "payload": json.dumps({k: v for k, v in payload.items() if k not in {"_color", "_callback"}})}
    return {"inline": False, "one_time": False, "buttons": [
        [{"action": action(label, payload), "color": button_color(label, payload)} for label, payload in row]
        for row in rows
    ]}



class VK:
    def __init__(self, token, group_id):
        self.token, self.group_id = token, group_id
        self.http = httpx.Client(timeout=90)
        self.store = None
        self._response = None
        self._callback = None

    def call(self, method, **kwargs):
        response = self.http.post(f"https://api.vk.com/method/{method}",
                                  data={"access_token": self.token, "v": "5.199", **kwargs})
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            error = body["error"]
            detail = str(error.get("error_msg") or "ВК отклонил запрос")
            # Never include request_params, tokens, signed URLs or upload payloads.
            if self.token:
                detail = detail.replace(self.token, "[скрыто]")
            detail = re.sub(r"https?://\S+", "[ссылка]", detail)
            detail = re.sub(r"[A-Za-z0-9_./+=-]{48,}", "[данные скрыты]", detail)
            raise VKAPIError(method, error.get("error_code"), detail[:350])
        return body["response"]

    def bind_store(self, store):
        self.store = store

    def begin_response(self, peer):
        self._response = {"peer": peer, "messages": [], "buttons": None}

    def send(self, peer, message, buttons=None, attachments=None, transient=False):
        frame = getattr(self, "_response", None)
        if frame is not None and frame["peer"] == peer and not transient and not attachments:
            frame["messages"].append(message)
            if buttons is not None:
                frame["buttons"] = buttons
            return []
        return self._send_messages(peer, message, buttons, attachments)

    def _send_messages(self, peer, message, buttons=None, attachments=None):
        sent = []
        chunks = [message[i:i+3500] for i in range(0, len(message), 3500)] or ["—"]
        for i, chunk in enumerate(chunks):
            args = {"peer_id": peer, "message": chunk, "random_id": secrets.randbelow(2**31 - 1) + 1}
            if attachments and i == 0:
                args["attachment"] = ",".join(attachments)
            if buttons is not None and i == len(chunks) - 1:
                args["keyboard"] = json.dumps(keyboard(buttons), ensure_ascii=False)
            if getattr(self, "store", None) is not None:
                args["peer_ids"] = str(args.pop("peer_id"))
            try:
                result = self.call("messages.send", **args)
            except VKAPIError as exc:
                if exc.code != 100 or not attachments or i != 0:
                    raise
                # Keep random_id for a safe retry of the same message.
                result = self.call("messages.send", **args, v=LEGACY_PHOTO_VERSION)
            records = result if isinstance(result, list) else [{"message_id": result}]
            for record in records:
                if not isinstance(record, dict) or record.get("error") or record.get("peer_id", peer) != peer:
                    if isinstance(record, dict) and record.get("error"):
                        raise ValueError("ВК не отправил сообщение")
                    continue
                reference = self.message_reference(record)
                if reference:
                    sent.append(reference)
                    if getattr(self, "store", None) is not None:
                        self.store.track_chat_message(peer, reference)
        return sent

    @staticmethod
    def message_reference(message):
        cmid = message.get("conversation_message_id")
        number = message.get("message_id", message.get("id"))
        if isinstance(cmid, int) and cmid > 0:
            return {"key": f"c:{cmid}", "cmid": cmid}
        if isinstance(number, int) and number > 0:
            return {"key": f"m:{number}", "id": number}
        return None

    def delete_message(self, peer, message, incoming=False):
        args = {"peer_id": peer, "cmids": str(message["cmid"])} if message.get("cmid") else {"message_ids": str(message["id"])}
        def deleted(result):
            if result is True or result == 1:
                return True
            if isinstance(result, dict):
                return any(deleted(value) for value in result.values())
            if isinstance(result, list):
                return any(deleted(value.get("response", 0)) for value in result if isinstance(value, dict))
            return False
        try:
            if deleted(self.call("messages.delete", delete_for_all=1, **args)):
                return True
        except (VKAPIError, httpx.HTTPError):
            pass
        if incoming:
            try:
                # VK may only allow deleting incoming messages on the community side.
                return deleted(self.call("messages.delete", delete_for_all=0, **args))
            except (VKAPIError, httpx.HTTPError):
                pass
        print("ВК не разрешил удалить одно из новых сообщений.", flush=True)
        return False

    def end_response(self, peer, incoming=None):
        frame, self._response = self._response, None
        if frame is None or not frame["messages"]:
            return True
        try:
            current = self._send_messages(peer, "\n\n".join(frame["messages"]), frame["buttons"])
        except (ValueError, httpx.HTTPError):
            print("Не удалось отправить текущий экран ВК. Предыдущие сообщения сохранены.", flush=True)
            return False
        if self.store is None or not current:
            return True
        keep = {item["key"] for item in current}
        for previous in self.store.chat_messages(peer):
            if previous["key"] not in keep and self.delete_message(peer, previous):
                self.store.forget_chat_message(peer, previous["key"])
        if incoming is not None:
            reference = self.message_reference(incoming)
            if reference:
                self.delete_message(peer, reference, incoming=True)
        return True

    def open_callback_link(self, peer, url):
        callback = getattr(self, "_callback", None)
        if callback is None:
            self.send(peer, url)
            return
        self.call("messages.sendMessageEventAnswer", event_id=callback["event_id"],
                  user_id=callback["user_id"], peer_id=peer,
                  event_data=json.dumps({"type": "open_link", "link": url}))
        self._callback_answered = True

    def handle_callback(self, callback, handler):
        self._callback, self._callback_answered = callback, False
        payload = callback.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        def acknowledge():
            if not self._callback_answered:
                try:
                    self.call("messages.sendMessageEventAnswer", event_id=callback["event_id"],
                              user_id=callback["user_id"], peer_id=callback["peer_id"])
                    self._callback_answered = True
                except (VKAPIError, httpx.HTTPError):
                    print("Не удалось подтвердить нажатие кнопки ВК.", flush=True)
        try:
            if payload.get("a") != "google_open":
                acknowledge()
            if payload.get("a") != "noop":
                handler({"id": 0, "from_id": callback["user_id"], "peer_id": callback["peer_id"],
                         "text": "", "payload": json.dumps(payload), "_callback_event_id": callback["event_id"]})
        finally:
            acknowledge()
            self._callback = None

    def enable_callbacks(self):
        try:
            settings = self.call("groups.getLongPollSettings", group_id=self.group_id)
            events = settings.get("events", {})
            if not settings.get("is_enabled") or not events.get("message_new") or not events.get("message_event") or settings.get("api_version") != "5.199":
                options = {key: int(value in (True, 1, "1")) for key, value in events.items()}
                options.update(message_new=1, message_event=1)
                self.call("groups.setLongPollSettings", group_id=self.group_id, enabled=1,
                          api_version="5.199", **options)
        except VKAPIError:
            print("Не удалось включить callback-события ВК. Включите message_event в настройках Long Poll сообщества.", flush=True)

    def upload_draft_photo(self, peer, path, mime, version=None):
        options = {"v": version} if version else {}
        server = self.call("photos.getMessagesUploadServer", peer_id=peer, **options)
        url = server["upload_url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not any((parsed.hostname or "") == host or (parsed.hostname or "").endswith("." + host)
                                                for host in ("vk.com", "vk.ru", "userapi.com", "vkuserphoto.ru")):
            raise ValueError("ВК вернул неподдерживаемый адрес загрузки фото")
        with Path(path).open("rb") as photo:
            response = self.http.post(url, files={"photo": (Path(path).name, photo, mime)})
        response.raise_for_status()
        uploaded = response.json()
        payload = uploaded.get("photo")
        if not payload or payload == "[]":
            raise ValueError("ВК не принял изображение при загрузке предпросмотра")
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, separators=(",", ":"))
        saved = self.call("photos.saveMessagesPhoto", server=uploaded["server"], photo=payload, hash=uploaded["hash"], **options)
        if not saved:
            raise ValueError("ВК не сохранил фото для предпросмотра. Повторите просмотр черновика")
        item = saved[0]
        if not item.get("owner_id") or not item.get("id"):
            raise VKAPIError("photos.saveMessagesPhoto", 100, "ВК вернул временный ID фото, непригодный для отправки")
        reference = f"photo{item['owner_id']}_{item['id']}"
        if item.get("access_key"):
            reference += "_" + item["access_key"]
        return reference

    def send_draft(self, peer, text, photos, buttons):
        for path, _ in photos:
            if not Path(path).is_file():
                raise ValueError("Не найден сохранённый файл фото черновика. Загрузите это фото повторно")
        attachments = []
        try:
            for path, mime in photos:
                try:
                    attachment = self.upload_draft_photo(peer, path, mime)
                except VKAPIError as exc:
                    if exc.code != 100:
                        raise
                    attachment = self.upload_draft_photo(peer, path, mime, LEGACY_PHOTO_VERSION)
                attachments.append(attachment)
        except (VKAPIError, ValueError, httpx.HTTPError):
            self.send(peer, text, buttons)
            raise
        self.send(peer, text, buttons, attachments=attachments)

    def full_photo_message(self, message):
        """Long Poll can supply incomplete photo metadata; re-read the message."""
        lookups = []
        if message.get("conversation_message_id"):
            lookups.append(("messages.getByConversationMessageId", {
                "peer_id": message["peer_id"], "conversation_message_ids": message["conversation_message_id"]}))
        if message.get("id", 0) > 0:
            lookups.append(("messages.getById", {"message_ids": message["id"]}))
        incoming_count = sum(a.get("type") == "photo" for a in message.get("attachments", []))
        for method, arguments in lookups:
            try:
                result = self.call(method, **arguments)
                for item in result.get("items", []):
                    if item.get("peer_id") != message["peer_id"] or item.get("from_id") != message["from_id"]:
                        continue
                    same = (item.get("conversation_message_id") == message.get("conversation_message_id")
                            if message.get("conversation_message_id") else item.get("id") == message.get("id"))
                    count = sum(a.get("type") == "photo" for a in item.get("attachments", []))
                    if same and count >= incoming_count:
                        return {**message, "attachments": item["attachments"]}
            except (httpx.HTTPError, ValueError, KeyError):
                # API errors and signed image URLs must not be logged verbatim.
                continue
        if lookups:
            print("Не удалось перечитать фотографии сообщения ВК; используются вложения Long Poll.", flush=True)
            return {**message, "_photo_lookup_failed": True}
        return message

    def resolve_user(self, value):
        value = value.strip()
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            if parsed.hostname not in {"vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"} or parsed.scheme != "https":
                raise ValueError("Отправьте ID ВК или HTTPS-ссылку на профиль vk.com")
            value = parsed.path.strip("/")
        value = value.removeprefix("@")
        numeric = re.fullmatch(r"(?:id)?([0-9]+)", value)
        if numeric:
            number = int(numeric.group(1))
            if 0 < number < 2**63:
                return number
            raise ValueError("ID пользователя должен быть положительным числом")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,63}", value):
            raise ValueError("Отправьте числовой ID ВК или ссылку на профиль пользователя")
        try:
            users = self.call("users.get", user_ids=value)
        except ValueError as exc:
            raise ValueError("Не удалось определить профиль ВК. Отправьте числовой ID пользователя") from exc
        if not users or users[0].get("deactivated") or not isinstance(users[0].get("id"), int) or users[0]["id"] <= 0:
            raise ValueError("Профиль пользователя ВК не найден")
        return users[0]["id"]

    def download_photo(self, url):
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or not any(host == d or host.endswith("." + d) for d in ("userapi.com", "vk.com", "vkuserphoto.ru")):
            raise ValueError("Неподдерживаемый адрес фото ВК")
        # Bound memory/disk usage; no redirects to arbitrary destinations.
        data = bytearray()
        with self.http.stream("GET", url) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                raise ValueError("ВК вернул не изображение")
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 25 * 1024 * 1024:
                    raise ValueError("Фото превышает 25 МБ")
        photo_format(data)
        return bytes(data)

    def run(self, handler, maintenance=None):
        server = None
        while True:
            try:
                if maintenance is not None:
                    maintenance()
                if server is None:
                    self.enable_callbacks()
                    server = self.call("groups.getLongPollServer", group_id=self.group_id)
                response = self.http.get(server["server"], params={"act": "a_check", "key": server["key"], "ts": server["ts"], "wait": 25})
                response.raise_for_status()
                body = response.json()
                if body.get("failed"):
                    if body["failed"] == 1:
                        server["ts"] = body["ts"]
                    else:
                        server = None
                    continue
                for event in body.get("updates", []):
                    if event["type"] == "message_new":
                        handler(event["object"]["message"])
                    elif event["type"] == "message_event":
                        self.handle_callback(event["object"], handler)
                server["ts"] = body["ts"]
            except (httpx.HTTPError, ValueError, KeyError):
                # Do not log HTTP exceptions: request URLs may contain Long Poll keys.
                print("Ошибка подключения ВК. Повтор через 5 секунд.", flush=True)
                server = None
                time.sleep(5)
