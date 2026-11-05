import io
from urllib.parse import quote

import google.auth
import httpx
from docx import Document
from google.auth.transport.requests import Request

from .core import DEFAULT_COLUMNS, append_materials, assignment_cell, assignment_from_task, table_layout

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
                raise ValueError("Google ещё не подключён. Откройте настройки проекта и подключите Google")
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
        project = {**DEFAULT_COLUMNS, **project}
        rows = self.rows(project)
        header_row, current_headers = table_layout(rows, project)
        if row <= header_row or row > len(rows) or len(current_headers) < 4:
            raise ValueError("Строка задания изменилась. Откройте дату заново")
        values = rows[row - 1]
        fields = dict(zip(current_headers, list(values) + [""] * len(current_headers)))
        expected = project.get("_expected_fields", {})
        for key in (project["date_column"], project["weekday_column"]):
            if key in expected and fields.get(key, "") != expected[key]:
                raise ValueError("Дата задания изменилась во время отправки. Откройте дату заново")
        original = str(values[3]) if len(values) > 3 else ""
        description, _ = assignment_cell(original)
        if "_expected_assignment" in project and description != project["_expected_assignment"]:
            raise ValueError("Задание в D изменилось во время отправки. Откройте дату заново")
        updated = append_materials(original, url)
        if updated == original:
            return
        if "_sheet_id" in project:
            self.request("POST", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}:batchUpdate", json={"requests": [{"updateCells": {
                "range": {"sheetId": project["_sheet_id"], "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": 3, "endColumnIndex": 4},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": updated}}]}], "fields": "userEnteredValue"}}]})
            return
        sheet = "'" + project["worksheet"].replace("'", "''") + "'"
        cell = quote(f"{sheet}!D{row}", safe="")
        self.request("PUT", f"sheets/v4/spreadsheets/{project['spreadsheet_id']}/values/{cell}",
                     params={"valueInputOption": "RAW"}, json={"values": [[updated]]})

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

    def folder(self, file_id, parent, name, properties=None):
        if not self.exists(file_id):
            metadata = {"id": file_id, "name": name, "parents": [parent], "mimeType": "application/vnd.google-apps.folder"}
            if properties:
                metadata["appProperties"] = properties
            self.request("POST", "drive/v3/files", params={"supportsAllDrives": "true"},
                         json=metadata)

    def project_folder(self, store, project):
        if not hasattr(self, "_drive_account"):
            self._drive_account = self.request("GET", "drive/v3/about", params={"fields": "user(permissionId)"}).get("user", {}).get("permissionId")
        if not self._drive_account:
            raise ValueError("Не удалось определить аккаунт Google Диска. Подключите Google заново")
        root = self.managed_folder(store, self._drive_account, "root", "BMauto", "BMauto")
        return self.managed_folder(store, self._drive_account, root, "project:" + project["id"], project["name"],
                                   {"bmautoProject": project["id"]})

    def managed_folder(self, store, account, parent, location, name, properties=None):
        cached = store.drive_location(account, parent, location)
        if cached and not cached["ready"]:
            self.folder(cached["id"], parent, name, properties)
            store.save_drive_location(account, parent, location, cached["id"], ready=True)
            return cached["id"]
        if cached:
            try:
                info = self.request("GET", f"drive/v3/files/{cached['id']}", params={"fields": "id,name,parents,trashed"})
                if not info.get("trashed") and info.get("name") == name and (parent == "root" or parent in info.get("parents", [])):
                    return cached["id"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        def escape(value):
            return str(value).replace("\\", "\\\\").replace("'", "\\'")
        query = f"trashed=false and mimeType='application/vnd.google-apps.folder' and '{escape(parent)}' in parents and name='{escape(name)}'"
        for key, value in (properties or {}).items():
            query += f" and appProperties has {{ key='{escape(key)}' and value='{escape(value)}' }}"
        found = self.request("GET", "drive/v3/files", params={"q": query, "fields": "files(id)", "pageSize": 1, "orderBy": "createdTime"}).get("files", [])
        if found:
            file_id = found[0]["id"]
            store.save_drive_location(account, parent, location, file_id, ready=True)
            return file_id
        file_id = self.new_id()
        store.save_drive_location(account, parent, location, file_id)
        self.folder(file_id, parent, name, properties)
        store.save_drive_location(account, parent, location, file_id, ready=True)
        return file_id

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
    project = {**DEFAULT_COLUMNS, **project}
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
    header_row, headers = table_layout(rows, project)
    project["_expected_fields"] = task["fields"]
    project["_expected_assignment"] = assignment_from_task(task)
    row = task["row"]
    if row <= header_row or row > len(rows) or len(headers) < 4:
        raise ValueError("Строка задания изменилась. Откройте дату заново")
    current = dict(zip(headers, rows[row - 1] + [""] * len(headers)))
    expected = task["fields"]
    current_description, existing = assignment_cell(rows[row - 1][3] if len(rows[row - 1]) > 3 else "")
    if (current_description != project["_expected_assignment"] or
        any(current.get(key, "") != expected.get(key, "") for key in (project["date_column"], project["weekday_column"]))):
        raise ValueError("Задание в таблице изменилось. Откройте список дат заново")
    url = f"https://drive.google.com/drive/folders/{draft['folder']}" if draft["folder"] else None
    if existing and existing != url:
        raise ValueError("У задания уже есть другая ссылка; перезапись запрещена")
    if draft["published"]:
        google.share(draft["folder"])
        google.set_link(project, row, headers, url)
        return url
    if not draft["folder"] or not google.exists(draft["folder"]):
        draft["parent"] = draft.get("parent") or google.project_folder(store, project)
        draft["folder"] = draft["folder"] or google.new_id()
        store.save_draft(key, draft)
        google.folder(draft["folder"], draft["parent"], task["day"])
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
    google.share(draft["folder"])
    url = f"https://drive.google.com/drive/folders/{draft['folder']}"
    google.set_link(project, row, headers, url)
    draft["published"] = True
    store.save_draft(key, draft)
    return url
