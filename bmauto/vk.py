import json
import secrets
import time
from urllib.parse import urlsplit

import httpx


def keyboard(buttons):
    return {"inline": False, "one_time": False, "buttons": [
        [{"action": {"type": "text", "label": label[:40], "payload": json.dumps(payload)}, "color": "secondary"}]
        for label, payload in buttons
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
        if not data.startswith(b"\xff\xd8"):
            raise ValueError("Прикрепите фото в формате JPEG")
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
