"""Synthetic examples of the workbook layout; no uploaded client content is stored."""
import json
from datetime import date

import httpx
import pytest

from bmauto.core import table_layout, tasks_from_rows
from bmauto.google import Google


def layout(header_row=2, extra_channel=False):
    headers = ["Неделя", "Дата", "День недели", "ВК Проект", "Телеграм Проект", "ВК Автор", "Инстаграм Автор"]
    if extra_channel:
        headers.append("ВК Другой автор ")
    rows = [["Контент-план — ноябрь 2026"]] + [[] for _ in range(header_row - 2)] + [headers]
    rows.append(["45", "06.11.2026", "Пятница", "Фото и текст по заданию", "", "", ""] + (["Другой канал"] if extra_channel else []))
    return rows


@pytest.mark.parametrize("header_row", [2, 3])
def test_detects_actual_header_row_without_link_column(header_row):
    rows = layout(header_row)
    rows.extend([["", "07.11.2026", "Суббота"], [], ["", "", "", "Заметка без даты"]])
    detected, headers = table_layout(rows)
    tasks = tasks_from_rows(rows, {}, date(2026, 11, 5))
    assert detected == header_row
    assert len(headers) == 7 and "Ссылка" not in headers
    assert [(t.row, t.day.isoformat()) for t in tasks] == [(header_row + 1, "2026-11-06"), (header_row + 2, "2026-11-07")]
    assert tasks[0].header_row == header_row
    assert "ВК Проект: Фото и текст по заданию" in tasks[0].description()


def test_trailing_blank_header_cells_are_ignored():
    rows = layout()
    rows[1].extend(["", " "])
    assert len(table_layout(rows)[1]) == 7


def client(rows, header_row=2, width=8):
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith("values:batchGetByDataFilter"):
            return httpx.Response(200, json={"valueRanges": [{"valueRange": {"values": rows}}]})
        if request.method == "GET":
            return httpx.Response(200, json={"sheets": [{"properties": {"sheetId": 0, "title": "Ноябрь", "gridProperties": {"columnCount": width}}}]})
        return httpx.Response(200, json={})
    class Credentials:
        valid = True
        token = "fake"
    google = Google.__new__(Google)
    google.credentials = Credentials()
    google.http = httpx.Client(transport=httpx.MockTransport(handle))
    project = {"id": "test", "name": "Project", "spreadsheet_id": "book", "worksheet": "Ноябрь", "_sheet_id": 0}
    project["_expected_fields"] = tasks_from_rows(rows, project, date(2026, 11, 5))[0].fields
    return google, project, calls


@pytest.mark.parametrize("header_row,extra_channel,width,column", [(2, False, 8, 7), (3, True, 8, 8)])
def test_creates_link_header_and_value_atomically_to_the_right(header_row, extra_channel, width, column):
    rows = layout(header_row, extra_channel)
    original = json.loads(json.dumps(rows))
    google, project, calls = client(rows, header_row, width)
    headers = table_layout(rows)[1]
    google.set_link(project, header_row + 1, headers, "https://example.test/materials")
    requests = json.loads(calls[-1].content)["requests"]
    cells = [r["updateCells"] for r in requests if "updateCells" in r]
    assert [c["range"]["startRowIndex"] for c in cells] == [header_row - 1, header_row]
    assert all(c["range"]["startColumnIndex"] == column and c["range"]["endColumnIndex"] == column + 1 for c in cells)
    assert [c["rows"][0]["values"][0]["userEnteredValue"]["stringValue"] for c in cells] == ["Ссылка", "https://example.test/materials"]
    if column == width:
        assert requests[0] == {"appendDimension": {"sheetId": 0, "dimension": "COLUMNS", "length": 1}}
    assert rows == original


def test_reuses_link_column_added_by_another_operator():
    rows = layout()
    google, project, calls = client(rows)
    old_headers = list(rows[1])
    rows[1].append("Ссылка")
    google.set_link(project, 3, old_headers, "https://example.test/materials")
    requests = json.loads(calls[-1].content)["requests"]
    assert len(requests) == 1 and requests[0]["updateCells"]["range"]["startRowIndex"] == 2


def test_unlabelled_data_cannot_be_overwritten_by_link():
    rows = layout()
    rows[2].append("Не перезаписывать")
    google, project, calls = client(rows)
    with pytest.raises(ValueError):
        google.set_link(project, 3, rows[1], "https://example.test/materials")
    assert not any(r.url.path.endswith(":batchUpdate") for r in calls)


def test_mid_upload_task_edit_blocks_link_creation():
    rows = layout()
    google, project, calls = client(rows)
    rows[2][3] = "Задание уже изменено"
    with pytest.raises(ValueError, match="во время отправки"):
        google.set_link(project, 3, rows[1], "https://example.test/materials")
    assert not any(r.url.path.endswith(":batchUpdate") for r in calls)
