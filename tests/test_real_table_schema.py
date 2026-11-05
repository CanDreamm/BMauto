"""Synthetic examples of the workbook layout; no uploaded client content is stored."""
import json
from datetime import date

import httpx
import pytest

from bmauto.core import append_materials, assignment_cell, table_layout, tasks_from_rows
from bmauto.google import Google

URL = "https://drive.google.com/drive/folders/materials"


def layout(header_row=2, extra_channel=False):
    headers = ["Неделя", "Дата", "День недели", "ВК Проект", "Телеграм Проект", "ВК Автор", "Инстаграм Автор"]
    if extra_channel:
        headers.append("ВК Другой автор ")
    rows = [["Контент-план — ноябрь 2026"]] + [[] for _ in range(header_row - 2)] + [headers]
    rows.append(["45", "06.11.2026", "Пятница", "Фото и текст по заданию", "Чужой канал E", "Чужой канал F", ""] + (["Другой канал"] if extra_channel else []))
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
    assert "Задание: Фото и текст по заданию" in tasks[0].description()
    assert "Чужой канал" not in tasks[0].description()


def test_trailing_blank_header_cells_are_ignored():
    rows = layout()
    rows[1].extend(["", " "])
    assert len(table_layout(rows)[1]) == 7


def client(rows):
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith("values:batchGetByDataFilter"):
            return httpx.Response(200, json={"valueRanges": [{"valueRange": {"values": rows}}]})
        if request.url.path.endswith(":batchUpdate"):
            for update in json.loads(request.content)["requests"]:
                cell = update["updateCells"]
                position = cell["range"]
                rows[position["startRowIndex"]][position["startColumnIndex"]] = cell["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
            return httpx.Response(200, json={})
        raise AssertionError("Unexpected Google API call")
    class Credentials:
        valid = True
        token = "fake"
    google = Google.__new__(Google)
    google.credentials = Credentials()
    google.http = httpx.Client(transport=httpx.MockTransport(handle))
    project = {"id": "test", "name": "Project", "spreadsheet_id": "book", "worksheet": "Ноябрь", "_sheet_id": 0}
    selected = tasks_from_rows(rows, project, date(2026, 11, 5))[0]
    project["_expected_fields"] = selected.fields
    project["_expected_assignment"] = selected.assignment
    return google, project, calls


@pytest.mark.parametrize("header_row,extra_channel", [(2, False), (3, True)])
def test_link_is_appended_inside_D_without_changing_headers_or_other_cells(header_row, extra_channel):
    rows = layout(header_row, extra_channel)
    original = json.loads(json.dumps(rows))
    google, project, calls = client(rows)
    google.set_link(project, header_row + 1, table_layout(rows)[1], URL)
    requests = json.loads(calls[-1].content)["requests"]
    assert len(requests) == 1
    cell = requests[0]["updateCells"]
    assert cell["range"] == {"sheetId": 0, "startRowIndex": header_row, "endRowIndex": header_row + 1, "startColumnIndex": 3, "endColumnIndex": 4}
    assert rows[header_row][3] == append_materials(original[header_row][3], URL)
    assert rows[:header_row] == original[:header_row]
    assert rows[header_row][:3] == original[header_row][:3]
    assert rows[header_row][4:] == original[header_row][4:]


def test_retry_same_link_does_not_duplicate_or_rewrite_cell():
    rows = layout()
    google, project, calls = client(rows)
    headers = table_layout(rows)[1]
    google.set_link(project, 3, headers, URL)
    google.set_link(project, 3, headers, URL)
    assert rows[2][3].count(URL) == 1
    assert sum(r.url.path.endswith(":batchUpdate") for r in calls) == 1
    selected = tasks_from_rows(rows, project, date(2026, 11, 5))[0]
    assert selected.assignment == "Фото и текст по заданию" and selected.material_url == URL
    assert URL not in selected.description()


def test_preexisting_separate_link_column_is_left_untouched():
    rows = layout()
    rows[1].append("Ссылка")
    rows[2].append("Другая информация")
    google, project, calls = client(rows)
    google.set_link(project, 3, rows[1], URL)
    assert rows[2][-1] == "Другая информация"
    assert rows[2][3].endswith(URL)


def test_mid_upload_task_edit_blocks_publication():
    rows = layout()
    google, project, calls = client(rows)
    rows[2][3] = "Задание уже изменено"
    with pytest.raises(ValueError, match="во время отправки"):
        google.set_link(project, 3, rows[1], URL)
    assert not any(r.url.path.endswith(":batchUpdate") for r in calls)


def test_other_channel_edits_do_not_change_D_publication():
    rows = layout()
    google, project, calls = client(rows)
    rows[2][4] = "Изменения другого канала"
    google.set_link(project, 3, rows[1], URL)
    assert rows[2][4] == "Изменения другого канала"
    assert rows[2][3].endswith(URL)


def test_different_existing_material_link_is_not_overwritten():
    rows = layout()
    rows[2][3] = append_materials(rows[2][3], "https://drive.google.com/drive/folders/other")
    google, project, calls = client(rows)
    with pytest.raises(ValueError, match="перезапись"):
        google.set_link(project, 3, rows[1], URL)
    assert not any(r.url.path.endswith(":batchUpdate") for r in calls)


def test_source_photo_link_in_description_is_preserved():
    source = "Фото из этой папки:\nhttps://drive.google.com/drive/folders/source"
    updated = append_materials(source, URL)
    assert assignment_cell(updated) == (source, URL)
    assert updated.startswith(source + "\n\n")
