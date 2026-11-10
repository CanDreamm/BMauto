import os
import time

import httpx


class GenAPI:
    """GenAPI network adapter. Confirm model slug/prices against current catalog."""

    def __init__(self):
        self.key = os.getenv("GENAPI_API_KEY", "")
        self.model = os.getenv("GENAPI_MODEL", "gpt-4o-mini")
        self.http = httpx.Client(base_url="https://api.gen-api.ru", timeout=90)

    def generate(self, project, task, supplement):
        if not self.key:
            raise ValueError("Не настроен GENAPI_API_KEY")
        messages = [
            {"role": "system", "content": project["prompt"] + "\n\n Концепция проекта: " + project["concept"]},
            {"role": "user", "content": "Задание: " + task + "\n\nДополнение:\n" + supplement},
        ]
        headers = {"Authorization": f"Bearer {self.key}"}
        response = self.http.post(f"/api/v1/networks/{self.model}", headers=headers,
                                  json={"is_sync": False, "messages": messages})
        response.raise_for_status()
        body = response.json()
        request_id = body.get("request_id")
        if not request_id:
            raise ValueError("GenAPI не вернул request_id; проверьте контракт выбранной модели")
        for _ in range(60):
            response = self.http.get(f"/api/v1/request/get/{request_id}", headers=headers)
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "success":
                return extract_text(result.get("result"))
            if result.get("status") in {"failed", "error"}:
                raise ValueError("GenAPI сообщил об ошибке генерации")
            time.sleep(2)
        raise ValueError(f"GenAPI не завершил запрос {request_id}. Проверьте его статус перед повторной платной генерацией")


def extract_text(result):
    if isinstance(result, str) and result.strip():
        return result
    if isinstance(result, list) and result:
        return extract_text(result[0])
    if isinstance(result, dict):
        for key in ("content", "text", "message", "choices"):
            if key in result:
                return extract_text(result[key])
    raise ValueError("Неизвестный формат ответа GenAPI; требуется сверка с документацией модели")
