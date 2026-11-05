import json
from datetime import date, datetime

import httpx
import pytest

from bmauto.calendar import load_tasks
from bmauto.core import Store
from bmauto.google import Google, publish
from bmauto.main import Bot
from bmauto.projects import validate_field
from test_projects import Conversation
from test_workflow import FakeGoogle, FakeLLM, FakeVK


HEADERS = ["Дата", "День недели", "Задание", "Ссылка"]


class MonthlyGoogle(FakeGoogle):
    def __init__(self, tables):
        super().__init__()
        self.tables = {sheet_id: [list(HEADERS)] + [[day, "Среда", "Текст", ""] for day in days]
                       for sheet_id, _, days in tables}
        self.sheets = [{"sheetId": sheet_id, "title": title} for sheet_id, title, _ in tables]
        self.reads, self.writes = [], []

    def worksheets(self, project):
        return self.sheets

    def rows(self, project):
        sheet_id = project["_sheet_id"]
        self.reads.append(sheet_id)
        return self.tables[sheet_id]

    def set_link(self, project, row, headers, url):
        self.writes.append((project["_sheet_id"], row, url))
        self.tables[project["_sheet_id"]][row - 1][3] = url


@pytest.fixture
def monthly_project(project):
    return {**project, "worksheet": "Авто"}


def test_current_month_only_before_rollover_window(monthly_project):
    google = MonthlyGoogle([(10, "Ноябрь", ["06.11.2026", "30.11.2026"]), (11, "Декабрь", ["01.12.2026"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 11, 5))
    assert [t["day"] for t in tasks] == ["2026-11-06", "2026-11-30"]
    assert google.reads == [10] and not warnings


def test_last_seven_days_include_next_month(monthly_project):
    google = MonthlyGoogle([(10, "Ноябрь", ["24.11.2026", "30.11.2026"]), (11, "Декабрь", ["01.12.2026", "10.12.2026"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 11, 24))
    assert [t["day"] for t in tasks] == ["2026-11-30", "2026-12-01", "2026-12-10"]
    assert [t["sheet_id"] for t in tasks] == [10, 11, 11]
    assert not warnings


def test_empty_current_month_moves_to_next_even_before_window(monthly_project):
    google = MonthlyGoogle([(0, "ноябрь", ["01.11.2026"]), (1, "ДЕКАБРЬ", ["01.12.2026"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 11, 5))
    assert len(tasks) == 1 and tasks[0]["worksheet"] == "ДЕКАБРЬ"
    assert google.reads == [0, 1] and not warnings


def test_december_to_january_filters_correct_year_and_month(monthly_project):
    google = MonthlyGoogle([(0, "Декабрь", ["30.12.2026", "30.12.2025", "15.01.2027"]),
                            (1, "Январь", ["01.01.2026", "01.01.2027", "02.02.2027"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 12, 29))
    assert [t["day"] for t in tasks] == ["2026-12-30", "2027-01-01"]
    assert not warnings


def test_existing_month_name_automatically_tracks_current_month(monthly_project):
    google = MonthlyGoogle([(0, "Ноябрь", ["30.11.2026"]), (1, "Декабрь", ["05.12.2026"])])
    tasks, _ = load_tasks(google, {**monthly_project, "worksheet": "Ноябрь"}, date(2026, 12, 1))
    assert [t["sheet_id"] for t in tasks] == [1]


def test_missing_next_tab_preserves_current_dates_and_reports_warning(monthly_project):
    google = MonthlyGoogle([(0, "Ноябрь", ["30.11.2026"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 11, 28))
    assert len(tasks) == 1
    assert warnings == ["Вкладка «Декабрь» не найдена"]


def test_missing_current_month_can_use_next(monthly_project):
    google = MonthlyGoogle([(0, "Декабрь", ["01.12.2026"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 11, 28))
    assert len(tasks) == 1 and "Ноябрь" in warnings[0]


def test_ambiguous_month_names_are_reported(monthly_project):
    google = MonthlyGoogle([(0, "Ноябрь", ["30.11.2026"]), (1, "ноябрь", ["29.11.2026"])])
    tasks, warnings = load_tasks(google, monthly_project, date(2026, 11, 5))
    assert not tasks
    assert any("неоднозначно" in warning for warning in warnings)


def test_rollover_is_automatic_and_ignores_old_settings(monthly_project):
    google = MonthlyGoogle([(0, "Ноябрь", ["30.11.2026"]), (1, "Декабрь", ["01.12.2026"])])
    tasks, _ = load_tasks(google, {**monthly_project, "month_rollover_days": 31}, date(2026, 11, 23))
    assert len(tasks) == 1
    tasks, _ = load_tasks(google, {**monthly_project, "month_rollover_days": 0}, date(2026, 11, 24))
    assert len(tasks) == 2


def test_publish_uses_original_sheet_id_after_rename(tmp_path, monthly_project):
    google = MonthlyGoogle([(0, "Ноябрь", ["30.11.2026"]), (1, "Декабрь", ["01.12.2026"])])
    tasks, _ = load_tasks(google, monthly_project, date(2026, 11, 28))
    task = tasks[1]
    google.sheets[1]["title"] = "Архив декабря"
    store = Store(tmp_path)
    draft = store.draft("december")
    draft["text"] = "Публикация"
    store.save_draft("december", draft)
    url = publish(google, store, "december", monthly_project, task)
    assert google.writes == [(1, 2, url)]
    assert google.tables[0][1][3] == ""
    assert google.tables[1][1][3] == url


def test_two_months_with_same_row_number_have_separate_shared_drafts(tmp_path, monthly_project, monkeypatch):
    google = MonthlyGoogle([(0, "Ноябрь", ["30.11.2026"]), (1, "Декабрь", ["01.12.2026"])])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 28, tzinfo=tz)
    monkeypatch.setattr("bmauto.main.datetime", Clock)
    bot = Bot({"projects": [monthly_project]}, Store(tmp_path), FakeVK(), google, FakeLLM(), {1})
    chat = Conversation(bot)
    chat.send("project", p="test")
    assert len(bot.store.session(1)["tasks"]) == 2
    chat.send("task", i=0)
    key_a, _ = bot.draft_for(bot.store.session(1), monthly_project)
    chat.send("project_home")
    chat.send("task", i=1)
    key_b, _ = bot.draft_for(bot.store.session(1), monthly_project)
    assert key_a != key_b
    draft = bot.store.draft(key_b)
    draft["text"] = "Декабрь"
    bot.store.save_draft(key_b, draft)
    chat.send("publish")
    assert google.writes[0][0] == 1


def test_google_sheet_id_reads_and_writes_survive_title_changes(monthly_project):
    seen = []
    def handle(request):
        seen.append(request)
        if "batchGetByDataFilter" in request.url.path:
            return httpx.Response(200, json={"valueRanges": [{"valueRange": {"values": [HEADERS]}}]})
        return httpx.Response(200, json={})
    class Credentials:
        valid = True
        token = "fake"
    google = Google.__new__(Google)
    google.credentials = Credentials()
    google.http = httpx.Client(transport=httpx.MockTransport(handle))
    source = {**monthly_project, "_sheet_id": 0}
    assert google.rows(source) == [HEADERS]
    google.set_link(source, 5, HEADERS, "https://example.test/folder")
    assert json.loads(seen[0].content)["dataFilters"] == [{"gridRange": {"sheetId": 0}}]
    update = json.loads(seen[1].content)["requests"][0]["updateCells"]
    assert update["range"] == {"sheetId": 0, "startRowIndex": 4, "endRowIndex": 5, "startColumnIndex": 3, "endColumnIndex": 4}
    assert update["rows"][0]["values"][0]["userEnteredValue"]["stringValue"] == "https://example.test/folder"


@pytest.mark.parametrize("value", ["-1", "32", "три", True])
def test_invalid_rollover_window_is_rejected(value):
    with pytest.raises(ValueError):
        validate_field("month_rollover_days", value)
