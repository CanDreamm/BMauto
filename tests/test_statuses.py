from copy import deepcopy
from datetime import date, datetime

import pytest

from bmauto.core import Store, append_materials, tasks_from_rows
from bmauto.google import publish
from bmauto.statuses import APPROVED, COLORS, EXPIRED, SENT, StatusSheets, is_our_rule, publication_status, PLANNED
from bmauto.vk import keyboard
from test_draft_editing import selected_bot
from test_projects import Conversation, actions
from test_workflow import FakeGoogle, task

URL = "https://drive.google.com/drive/folders/materials"


class Sheets(StatusSheets):
    """Stateful API double: retain formulas, merge ranges and conditional rules."""
    def __init__(self, header_row=2):
        self.locale = "en_US"
        self.tables = {0: [["План"]] + [[] for _ in range(header_row - 2)] + [
            ["Неделя", "Дата", "День недели", "Задание", "Другой канал"],
            ["45", "06.11.2026", "Пятница", "Задание", "Чужой текст"],
            [APPROVED, "07.11.2026", "Суббота", append_materials("Текст", URL), "Не менять"],
            ["", "", "", "Примечание без даты"]]}
        self.meta = {0: {"properties": {"sheetId": 0}, "merges": [
            {"sheetId": 0, "startRowIndex": header_row, "endRowIndex": header_row + 2,
             "startColumnIndex": 0, "endColumnIndex": 1}], "conditionalFormats": [
            {"ranges": [{"sheetId": 0, "startColumnIndex": 4, "endColumnIndex": 5}],
             "booleanRule": {"condition": {"type": "NOT_BLANK"}, "format": {"textFormat": {"bold": True}}}}]}}
        self.writes = []

    def request(self, method, path, **kwargs):
        if method == "GET":
            return {"properties": {"locale": self.locale}, "sheets": deepcopy(list(self.meta.values()))}
        body = kwargs["json"]
        if path.endswith("values:batchGetByDataFilter"):
            return {"valueRanges": [{"dataFilters": [data_filter], "valueRange": {
                "values": deepcopy(self.tables[data_filter["gridRange"]["sheetId"]])}}
                for data_filter in reversed(body["dataFilters"])]}
        assert path.endswith(":batchUpdate")
        self.writes.append(deepcopy(body["requests"]))
        for request in body["requests"]:
            if "updateCells" in request:
                update = request["updateCells"]
                cell = update["range"]
                assert cell["startColumnIndex"] == 0 and cell["endColumnIndex"] == 1
                value = update["rows"][0]["values"][0]["userEnteredValue"]
                self.tables[cell["sheetId"]][cell["startRowIndex"]][0] = next(iter(value.values()))
            elif "unmergeCells" in request:
                merge = request["unmergeCells"]["range"]
                self.meta[merge["sheetId"]]["merges"].remove(merge)
            elif "deleteConditionalFormatRule" in request:
                rule = request["deleteConditionalFormatRule"]
                self.meta[rule["sheetId"]]["conditionalFormats"].pop(rule["index"])
            else:
                rule = request["addConditionalFormatRule"]
                sid = rule["rule"]["ranges"][0]["sheetId"]
                self.meta[sid]["conditionalFormats"].insert(rule["index"], rule["rule"])
        return {}

    def rows(self, project):
        return self.tables[project["_sheet_id"]]

    def worksheets(self, project):
        return [{"sheetId": 0, "title": "Ноябрь"}]


@pytest.mark.parametrize("header", [2, 3])
def test_install_live_statuses_only_in_A_preserves_approvals_and_other_cells(project, header):
    google = Sheets(header)
    before = deepcopy(google.tables[0])
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    rows = google.tables[0]
    assert rows[header - 1][0] == "Статус"
    assert rows[header][0] == PLANNED
    assert rows[header + 1][0] == APPROVED
    assert rows[header + 2] == before[header + 2]
    assert [row[1:] for row in rows] == [row[1:] for row in before]
    assert not google.meta[0]["merges"]
    rules = google.meta[0]["conditionalFormats"]
    assert len(rules) == 5 and len([r for r in rules if is_our_rule(r)]) == 4
    assert rules[-1]["ranges"][0]["startColumnIndex"] == 4
    for rule in rules[:-1]:
        assert rule["ranges"][0]["startColumnIndex"] == 0
        assert rule["ranges"][0]["endColumnIndex"] == 1
    assert rules[0]["booleanRule"]["format"]["backgroundColor"] == COLORS[EXPIRED]
    assert rules[1]["booleanRule"]["format"]["backgroundColor"] == COLORS[SENT]
    assert rules[2]["booleanRule"]["format"]["backgroundColor"] == COLORS[APPROVED]
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert len(google.writes) == 1  # Existing formulas and rules are not rewritten.


def test_status_priority_preserves_approval_and_detects_expired_and_sent():
    today = date(2026, 11, 9)
    assert publication_status(date(2026, 11, 8), "", APPROVED, today) == APPROVED
    assert publication_status(date(2026, 11, 8), append_materials("Текст", URL), SENT, today) == EXPIRED
    assert publication_status(date(2026, 11, 10), append_materials("Текст", URL), "#ERROR!", today) == SENT
    assert publication_status(today, "Текст", "#ERROR!", today) == PLANNED


@pytest.mark.parametrize("value", [46332, "06.01.2030", "2030-01-06", "06/01/2030"])
def test_formatted_dates_and_native_serial_dates_get_statuses(project, value):
    google = Sheets()
    google.tables[0][2][1] = value
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert google.tables[0][2][0] == PLANNED


def test_settings_footer_pairs_back_and_delete(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    chat.send("settings")
    rows = keyboard(bot.vk.messages[-1][1])["buttons"]
    assert [row[0]["action"]["label"] for row in rows] == [
        "Подключить Google", "Google Таблица", "Концепция", "Промпт", "Участники", "Назад"]
    assert rows[-1][0]["color"] == "primary" and rows[-1][1]["color"] == "negative"
    assert rows[-1][1]["action"]["label"] == "Удалить проект"


def test_sync_all_months_by_sheet_id_and_replaces_old_owned_rules(project):
    google = Sheets()
    google.tables[8] = deepcopy(google.tables[0])
    google.meta[8] = {"properties": {"sheetId": 8}, "conditionalFormats": []}
    old = deepcopy(google.meta[0]["conditionalFormats"][0])
    old["booleanRule"]["condition"] = {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=N("BMauto:status:v1")=0'}]}
    google.meta[0]["conditionalFormats"].insert(0, old)
    google.sync_statuses(project, [{"sheetId": 0, "title": "ноябрь"}, {"sheetId": 8, "title": "Октябрь"}])
    assert google.tables[0][2][0] == google.tables[8][2][0] == EXPIRED
    assert len(google.meta[0]["conditionalFormats"]) == 5
    assert len(google.meta[8]["conditionalFormats"]) == 4


def selected(google):
    source = {"spreadsheet_id": "book", "_sheet_id": 0}
    row = google.tables[0][2]
    row[3] = append_materials(row[3], URL)
    t = tasks_from_rows(google.tables[0], source, date(2026, 11, 1))[0]
    return source, {"row": t.row, "day": t.day.isoformat(), "fields": t.fields,
                    "assignment": t.assignment, "sheet_id": 0, "source_spreadsheet_id": "book"}


def test_approve_validates_source_and_changes_only_A_then_sent_resets_approval():
    google = Sheets()
    project, chosen = selected(google)
    before = deepcopy(google.tables[0])
    google.set_status(project, chosen, APPROVED, URL)
    assert google.tables[0][2] == [APPROVED] + before[2][1:]
    google.set_status(project, chosen, SENT, URL)
    assert google.tables[0][2][0] == publication_status(date(2026, 11, 6), before[2][3], "", datetime.now().date())
    assert google.tables[0][2][1:] == before[2][1:]


@pytest.mark.parametrize("change", ["date", "assignment", "link", "book", "row"])
def test_stale_approval_never_marks_another_task(change):
    google = Sheets()
    project, chosen = selected(google)
    if change == "date":
        google.tables[0][2][1] = "08.11.2026"
    elif change == "assignment":
        google.tables[0][2][3] = append_materials("Другое задание", URL)
    elif change == "link":
        google.tables[0][2][3] = append_materials("Задание", URL + "new")
    elif change == "book":
        project["spreadsheet_id"] = "different"
    else:
        chosen["row"] = 99
    with pytest.raises(ValueError):
        google.set_status(project, chosen, APPROVED, URL)
    assert not google.writes


def test_menu_order_and_publication_approval_edit_cycle(tmp_path, project):
    bot, chat = selected_bot(tmp_path, project)
    assert "approve" not in actions(bot)
    chat.send("edit_text")
    chat.send(text="Текст публикации")
    rows = keyboard(bot.vk.messages[-1][1])["buttons"]
    labels = [[b["action"]["label"] for b in row] for row in rows]
    assert labels[0] == ["Посмотреть черновик"]
    assert labels[1] == ["Редактировать текст", "Сгенерировать заново"]
    assert labels[-1] == ["Назад", "Отправить в таблицу"]
    chat.send("publish")
    assert bot.google.table[2][0] == SENT
    assert "approve" in actions(bot)
    rows = keyboard(bot.vk.messages[-1][1])["buttons"]
    assert rows[-1][1]["color"] == "positive"
    chat.send("approve")
    assert bot.google.table[2][0] == APPROVED and "approve" not in actions(bot)
    chat.send("edit_text")
    chat.send(text="Исправленный текст")
    assert "approve" not in actions(bot) and "publish" in actions(bot)
    chat.send("approve")  # Stale button cannot approve unsaved changes.
    assert bot.google.table[2][0] == APPROVED
    chat.send("publish")
    assert bot.google.table[2][0] == SENT and "approve" in actions(bot)
    chat.send("approve")
    assert bot.google.table[2][0] == APPROVED


def test_failed_status_update_keeps_publication_retryable_without_duplicate_uploads(tmp_path, project):
    google, store = FakeGoogle(), Store(tmp_path)
    chosen = task(google, project)
    draft = store.draft("draft")
    draft["text"] = "Текст"
    store.save_draft("draft", draft)
    update = google.set_status
    google.set_status = lambda *args: (_ for _ in ()).throw(RuntimeError("temporary failure"))
    with pytest.raises(RuntimeError):
        publish(google, store, "draft", project, chosen)
    assert not store.draft("draft")["published"]
    google.set_status = update
    publish(google, store, "draft", project, chosen)
    assert google.created == ["publication.docx"]
    assert store.draft("draft")["published"] and google.table[2][0] == SENT


def test_member_can_approve_and_removed_member_cannot(tmp_path, project):
    bot, owner = selected_bot(tmp_path, project)
    owner.send("edit_text")
    owner.send(text="Текст")
    owner.send("publish")
    bot.store.add_member(project["id"], 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    member.send("project", p=project["id"])
    member.send("task", i=0)
    member.send("approve")
    assert bot.google.table[2][0] == APPROVED
    bot.google.table[2][0] = SENT
    bot.store.remove_member(project["id"], 1, 3, bot.allowed)
    member.send("approve")
    assert bot.google.table[2][0] == SENT


def test_approval_writes_green_fill_and_installs_rules_above_existing_formats():
    google = Sheets()
    project, chosen = selected(google)
    original_other_rule = deepcopy(google.meta[0]['conditionalFormats'][0])
    google.set_status(project, chosen, APPROVED, URL)
    update = google.writes[-1][-1]['updateCells']
    cell = update['rows'][0]['values'][0]
    assert cell['userEnteredValue'] == {'stringValue': APPROVED}
    assert cell['userEnteredFormat']['backgroundColorStyle']['rgbColor'] == COLORS[APPROVED]
    assert 'userEnteredFormat.backgroundColorStyle' in update['fields']
    assert update['range']['startColumnIndex'] == 0 and update['range']['endColumnIndex'] == 1
    rules = google.meta[0]['conditionalFormats']
    assert rules[2]['booleanRule']['condition'] == {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': APPROVED}]}
    assert rules[2]['booleanRule']['format']['backgroundColor'] == COLORS[APPROVED]
    assert rules[-1] == original_other_rule


def test_russian_locale_does_not_require_any_formulas(project):
    google = Sheets()
    google.locale = 'ru_RU'
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert google.tables[0][2][0] == PLANNED
    assert not google.tables[0][2][0].startswith('=')
    assert all(rule['booleanRule']['condition']['type'] == 'TEXT_EQ'
               for rule in google.meta[0]['conditionalFormats'][:4])
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert len(google.writes) == 1


def test_old_or_displaced_color_rules_are_repaired_without_touching_other_channels(project):
    google = Sheets()
    old = {'ranges': [{'sheetId': 0, 'startColumnIndex': 0, 'endColumnIndex': 1}],
           'booleanRule': {'condition': {'type': 'CUSTOM_FORMULA', 'values': [
               {'userEnteredValue': '=N("BMauto:status:v1")=0'}]}, 'format': {'backgroundColor': COLORS[SENT]}}}
    google.meta[0]['conditionalFormats'].append(old)
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert len(google.meta[0]['conditionalFormats']) == 5
    assert all(rule['booleanRule']['condition']['type'] == 'TEXT_EQ'
               for rule in google.meta[0]['conditionalFormats'][:4])


def test_sending_materials_writes_literal_status_and_yellow_fill():
    google = Sheets()
    google.tables[0][2][1] = '06.11.2099'
    google.tables[0][3][1] = '07.11.2099'
    project, chosen = selected(google)
    google.set_status(project, chosen, SENT, URL)
    cell = google.writes[-1][-1]['updateCells']['rows'][0]['values'][0]
    assert cell['userEnteredValue'] == {'stringValue': SENT}
    assert cell['userEnteredFormat']['backgroundColorStyle']['rgbColor'] == COLORS[SENT]
    assert google.tables[0][2][0] == SENT


def test_broken_formulas_are_replaced_without_changing_existing_approval(project):
    google = Sheets()
    google.tables[0][2][0] = '#ERROR!'
    google.tables[0][2][3] = append_materials('Текст', URL)
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert google.tables[0][2][0] == SENT
    assert google.tables[0][3][0] == APPROVED
    cell_updates = [request['updateCells'] for request in google.writes[-1] if 'updateCells' in request]
    assert all('formulaValue' not in update['rows'][0]['values'][0]['userEnteredValue'] for update in cell_updates)


def test_background_refresh_runs_at_startup_and_after_date_changes(tmp_path, project, monkeypatch):
    import bmauto.main as main
    bot, chat = selected_bot(tmp_path, project)
    calls = []
    bot.google.sync_statuses = lambda project, sheets, today=None: calls.append(today)
    class Clock(datetime):
        day = 5
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, cls.day, tzinfo=tz)
    monkeypatch.setattr(main, 'datetime', Clock)
    bot.refresh_statuses()
    bot.refresh_statuses()
    assert calls == [date(2026, 11, 5)]
    Clock.day = 6
    bot._status_retry_at = 0
    bot.refresh_statuses()
    assert calls == [date(2026, 11, 5), date(2026, 11, 6)]
