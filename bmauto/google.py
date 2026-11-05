import io
from urllib.parse import quote

import google.auth
import httpx
from docx import Document
from google.auth.transport.requests import Request

from .core import column_letter

SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]


class Google:
    def __init__(self, credentials_file):
        self.credentials_file = credentials_file
        self.credentials = None
        self.http = httpx.Client(timeout=90)

    def request(self, method, path, **kwargs):
        if self.credentials is None:
            from pathlib import Path
            if not Path(self.credentials_file).is_file():
                raise ValueError("Google ещё не подключён. Настройте файл авторизации GOOGLE_CREDENTIALS_FILE на сервере. Настройки проектов доступны без Google")
            self.credentials, _ = google.auth.load_credentials_from_file(self.credentials_file, scopes=SCOPES)
        if not self.credentials.valid:
            self.credentials.refresh(Request())
        url = ("https://sheets.googleapis.com/" + path.removeprefix("sheets/")
               if path.startswith("sheets/") else "https://www.googleapis.com/" + path)
        response = self.http.request(method, url,
                                     headers={"Authorization": f"Bearer {self.credentials.token}"}, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def rows(self, project):
        if "_sheet_id" in project:
            result = self.request("POST", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}/values:batchGetByDataFilter",
                                  json={"dataFilters": [{"gridRange": {"sheetId": project["_sheet_id"]}}], "valueRenderOption": "FORMATTED_VALUE"})
            ranges = result.get("valueRanges", [])
            return ranges[0].get("valueRange", {}).get("values", []) if ranges else []
        sheet = "'" + project["worksheet"].replace("'", "''") + "'"
        return self.request("GET", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}/values/{quote(sheet, safe='')}").get("values", [])

    def set_link(self, project, row, headers, url):
        if "_sheet_id" in project:
            column = headers.index(project["link_column"])
            self.request("POST", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}:batchUpdate", json={"requests": [{"updateCells": {
                "range": {"sheetId": project["_sheet_id"], "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": column, "endColumnIndex": column + 1},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": url}}]}], "fields": "userEnteredValue"}}]})
            return
        column = column_letter(headers.index(project["link_column"]) + 1)
        sheet = "'" + project["worksheet"].replace("'", "''") + "'"
        cell = quote(f"{sheet}!{column}{row}", safe="")
        self.request("PUT", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}/values/{cell}",
                     params={"valueInputOption": "RAW"}, json={"values": [[url]]})

    def worksheets(self, project):
        result = self.request("GET", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}", params={"fields": "sheets(properties(sheetId,title))"})
        return [sheet["properties"] for sheet in result.get("sheets", [])]

    def new_id(self):
        return self.request("GET", "drive/v3/files/generateIds", params={"count": 1})["ids"][0]

    def exists(self, file_id):
        try:
            self.request("GET", f"drive/v3/files/{file_id}", params={"fields": "id", "supportsAllDrives": "true"})
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise

    def folder(self, file_id, parent, name):
        if not self.exists(file_id):
            self.request("POST", "drive/v3/files", params={"supportsAllDrives": "true"},
                         json={"id": file_id, "name": name, "parents": [parent],
                               "mimeType": "application/vnd.google-apps.folder"})

    def upload(self, file_id, parent, name, content, mime):
        if self.exists(file_id):
            return
        # Create metadata with a preallocated ID, then upload bytes. An interrupted upload
        # is resumed by checking size rather than treating metadata as completion.
        self.request("POST", "drive/v3/files", params={"supportsAllDrives": "true"},
                     json={"id": file_id, "name": name, "parents": [parent], "mimeType": mime})
        self.upload_content(file_id, content, mime)

    def upload_content(self, file_id, content, mime):
        self.request("PATCH", f"upload/drive/v3/files/{file_id}",
                     params={"uploadType": "media", "supportsAllDrives": "true"}, content=content)

    def share(self, folder):
        permissions = self.request("GET", f"drive/v3/files/{folder}/permissions",
                                   params={"supportsAllDrives": "true"}).get("permissions", [])
        if not any(p["type"] == "anyone" and p["role"] == "reader" for p in permissions):
            self.request("POST", f"drive/v3/files/{folder}/permissions",
                         params={"supportsAllDrives": "true"}, json={"type": "anyone", "role": "reader"})


def word_document(text):
    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def publish(google, store, key, project, task):
    draft = store.draft(key)
    if not draft["text"].strip():
        raise ValueError("Сначала сохраните текст публикации")
    if task.get("source_spreadsheet_id", project["spreadsheet_id"]) != project["spreadsheet_id"]:
        raise ValueError("Таблица проекта изменилась. Откройте задание заново")
    project = {**project, "worksheet": task.get("worksheet", project.get("worksheet", "Авто"))}
    if "sheet_id" in task:
        project["_sheet_id"] = task["sheet_id"]
    rows = google.rows(project)
    if not rows:
        raise ValueError("Исходная вкладка задания пуста. Откройте список дат заново")
    headers = [str(h).strip() for h in rows[0]]
    row = task["row"]
    current = dict(zip(headers, rows[row - 1] + [""] * len(headers))) if row <= len(rows) else {}
    expected = task["fields"]
    if any(current.get(k, "") != v for k, v in expected.items() if k != project["link_column"]):
        raise ValueError("Задание в таблице изменилось. Откройте список дат заново")
    url = f"https://drive.google.com/drive/folders/{draft['folder']}" if draft["folder"] else None
    existing = current.get(project["link_column"], "").strip()
    if existing and existing != url:
        raise ValueError("В этой строке уже есть другая ссылка; перезапись запрещена")
    if draft["published"]:
        return url
    if not draft["folder"]:
        draft["folder"] = google.new_id()
        store.save_draft(key, draft)
    google.folder(draft["folder"], project["drive_parent_id"], task["day"])
    files = [("publication.docx", word_document(draft["text"]),
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document")]
    for i, photo in enumerate(draft["photos"], 1):
        files.append((f"photo_{i:02}.jpg", (store.root / photo["path"]).read_bytes(), "image/jpeg"))
    for name, content, mime in files:
        entry = draft["uploads"].get(name)
        if entry is None:
            entry = {"id": google.new_id(), "done": False}
            draft["uploads"][name] = entry
            store.save_draft(key, draft)
        if not entry["done"]:
            if google.exists(entry["id"]):
                google.upload_content(entry["id"], content, mime)
            else:
                google.upload(entry["id"], draft["folder"], name, content, mime)
            entry["done"] = True
            store.save_draft(key, draft)
    if project["public_read"]:
        google.share(draft["folder"])
    url = f"https://drive.google.com/drive/folders/{draft['folder']}"
    google.set_link(project, row, headers, url)
    draft["published"] = True
    store.save_draft(key, draft)
    return url
