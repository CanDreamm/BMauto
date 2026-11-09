import json
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx


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


def keyboard(buttons):
    rows = [item if isinstance(item, list) else [item] for item in buttons]
    def action(label, payload):
        if "open_link" in payload:
            return {"type": "open_link", "label": label[:40], "link": payload["open_link"]}
        return {"type": "text", "label": label[:40], "payload": json.dumps({k: v for k, v in payload.items() if k != "_color"})}
    return {"inline": False, "one_time": False, "buttons": [
        [({"action": action(label, payload)} if "open_link" in payload else {"action": action(label, payload), "color": button_color(label, payload)})
         for label, payload in row] for row in rows
    ]}


class VK:
    def __init__(self, token, group_id):
        self.token, self.group_id = token, group_id
        self.http = httpx.Client(timeout=90)

    def call(self, method, **kwargs):
        response = self.http.post(f"https://api.vk.com/method/{method}",
                                  data={"access_token": self.token, "v": "5.199", **kwargs})
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise ValueError(f"Ошибка VK API {body['error'].get('error_code')}")
        return body["response"]

    def send(self, peer, message, buttons=None):
        chunks = [message[i:i+3500] for i in range(0, len(message), 3500)] or ["—"]
        for i, chunk in enumerate(chunks):
            args = {"peer_id": peer, "message": chunk, "random_id": secrets.randbelow(2**31 - 1) + 1}
            if buttons is not None and i == len(chunks) - 1:
                args["keyboard"] = json.dumps(keyboard(buttons), ensure_ascii=False)
            self.call("messages.send", **args)

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

    def run(self, handler):
        server = None
        while True:
            try:
                if server is None:
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
                server["ts"] = body["ts"]
            except (httpx.HTTPError, ValueError, KeyError):
                # Do not log HTTP exceptions: request URLs may contain Long Poll keys.
                print("Ошибка подключения ВК. Повтор через 5 секунд.", flush=True)
                server = None
                time.sleep(5)
