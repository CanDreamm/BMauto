from copy import deepcopy
from datetime import date, datetime

import pytest

from bmauto.core import Store, append_materials, tasks_from_rows
from bmauto.google import publish
from bmauto.statuses import APPROVED, COLORS, NO_PUBLICATION, NOT_PLANNED, PUBLISHED, PLANNED, StatusSheets, is_our_rule, publication_status, EMPTY
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
                value = update["rows"][0]["values"][0].get("userEnteredValue", {})
                self.tables[cell["sheetId"]][cell["startRowIndex"]][0] = next(iter(value.values()), "")
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
    assert rows[header][0] == EMPTY
    assert rows[header + 1][0] == APPROVED
    assert rows[header + 2] == before[header + 2]
    assert [row[1:] for row in rows] == [row[1:] for row in before]
    assert not google.meta[0]["merges"]
    rules = google.meta[0]["conditionalFormats"]
    assert len(rules) == 7 and len([r for r in rules if is_our_rule(r)]) == 6
    assert rules[-1]["ranges"][0]["startColumnIndex"] == 4
    for rule in rules[:-1]:
        assert rule["ranges"][0]["startColumnIndex"] == 0
        assert rule["ranges"][0]["endColumnIndex"] == 1
    assert rules[0]["booleanRule"]["format"]["backgroundColor"] == COLORS[NO_PUBLICATION]
    assert rules[1]["booleanRule"]["format"]["backgroundColor"] == COLORS[PLANNED]
    assert rules[2]["booleanRule"]["format"]["backgroundColor"] == COLORS[APPROVED]
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert len(google.writes) == 1  # Existing formulas and rules are not rewritten.


def test_status_priority_preserves_approval_and_detects_expired_and_sent():
    today = date(2026, 11, 9)
    assert publication_status(date(2026, 11, 8), "", APPROVED, today) == NO_PUBLICATION
    assert publication_status(date(2026, 11, 8), append_materials("Текст", URL), PLANNED, today) == PUBLISHED
    assert publication_status(date(2026, 11, 10), append_materials("Текст", URL), "#ERROR!", today) == PLANNED
    assert publication_status(today, "Текст", "#ERROR!", today) == EMPTY


@pytest.mark.parametrize("value", [46332, "06.01.2030", "2030-01-06", "06/01/2030"])
def test_formatted_dates_and_native_serial_dates_get_statuses(project, value):
    google = Sheets()
    google.tables[0][2][1] = value
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert google.tables[0][2][0] == EMPTY


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
    assert google.tables[0][2][0] == google.tables[8][2][0] == NOT_PLANNED
    assert len(google.meta[0]["conditionalFormats"]) == 7
    assert len(google.meta[8]["conditionalFormats"]) == 6


def selected(google):
    google.tables[0][2][1] = "06.11.2099"
    google.tables[0][3][1] = "07.11.2099"
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
    google.set_status(project, chosen, PLANNED, URL)
    assert google.tables[0][2][0] == publication_status(date.fromisoformat(chosen["day"]), before[2][3], "", datetime.now().date())
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
    assert bot.google.table[2][0] == PLANNED
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
    assert bot.google.table[2][0] == PLANNED and "approve" in actions(bot)
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
    assert store.draft("draft")["published"] and google.table[2][0] == PLANNED


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
    bot.google.table[2][0] = PLANNED
    bot.store.remove_member(project["id"], 1, 3, bot.allowed)
    member.send("approve")
    assert bot.google.table[2][0] == PLANNED


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
    assert google.tables[0][2][0] == EMPTY
    assert not google.tables[0][2][0].startswith('=')
    assert all(rule['booleanRule']['condition']['type'] == 'TEXT_EQ'
               for rule in google.meta[0]['conditionalFormats'][:3])
    assert google.meta[0]['conditionalFormats'][3]['booleanRule']['condition'] == {'type': 'BLANK'}
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert len(google.writes) == 1


def test_old_or_displaced_color_rules_are_repaired_without_touching_other_channels(project):
    google = Sheets()
    old = {'ranges': [{'sheetId': 0, 'startColumnIndex': 0, 'endColumnIndex': 1}],
           'booleanRule': {'condition': {'type': 'CUSTOM_FORMULA', 'values': [
               {'userEnteredValue': '=N("BMauto:status:v1")=0'}]}, 'format': {'backgroundColor': COLORS[PLANNED]}}}
    google.meta[0]['conditionalFormats'].append(old)
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert len(google.meta[0]['conditionalFormats']) == 7
    assert all(rule['booleanRule']['condition']['type'] == 'TEXT_EQ'
               for rule in google.meta[0]['conditionalFormats'][:3])
    assert google.meta[0]['conditionalFormats'][3]['booleanRule']['condition'] == {'type': 'BLANK'}


def test_sending_materials_writes_literal_status_and_yellow_fill():
    google = Sheets()
    google.tables[0][2][1] = '06.11.2099'
    google.tables[0][3][1] = '07.11.2099'
    project, chosen = selected(google)
    google.set_status(project, chosen, PLANNED, URL)
    cell = google.writes[-1][-1]['updateCells']['rows'][0]['values'][0]
    assert cell['userEnteredValue'] == {'stringValue': 'Запланировано'}
    assert cell['userEnteredFormat']['backgroundColorStyle']['rgbColor'] == COLORS[PLANNED]
    assert google.tables[0][2][0] == PLANNED


def test_broken_formulas_are_replaced_without_changing_existing_approval(project):
    google = Sheets()
    google.tables[0][2][0] = '#ERROR!'
    google.tables[0][2][3] = append_materials('Текст', URL)
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert google.tables[0][2][0] == PLANNED
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


def test_old_planned_without_link_is_cleared_and_linked_rows_become_planned(project):
    google = Sheets()
    google.tables[0][2][0] = PLANNED
    google.tables[0].append(['Материалы отправлены', '10.11.2026', 'Вторник', append_materials('Задание', URL), 'Другой канал'])
    old_rule = {'ranges': [{'sheetId': 0, 'startRowIndex': 2, 'startColumnIndex': 0, 'endColumnIndex': 1}],
                'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': PLANNED}]},
                                'format': {'backgroundColor': COLORS[EMPTY]}}}
    google.meta[0]['conditionalFormats'].append(old_rule)
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 5))
    assert google.tables[0][2][0] == ''
    assert google.tables[0][3][0] == APPROVED
    assert google.tables[0][-1][0] == PLANNED
    clearing = next(request['updateCells'] for request in google.writes[-1]
                    if 'updateCells' in request and request['updateCells']['range']['startRowIndex'] == 2)
    assert 'userEnteredValue' not in clearing['rows'][0]['values'][0]
    assert 'userEnteredValue' in clearing['fields']
    assert clearing['rows'][0]['values'][0]['userEnteredFormat']['backgroundColorStyle']['rgbColor'] == COLORS[EMPTY]
    assert len(google.meta[0]['conditionalFormats']) == 7
    assert google.meta[0]['conditionalFormats'][1]['booleanRule']['condition']['values'] == [{'userEnteredValue': PLANNED}]
    assert google.meta[0]['conditionalFormats'][1]['booleanRule']['format']['backgroundColor'] == COLORS[PLANNED]
    assert publication_status(date(2026, 11, 1), 'Задание без ссылки', PLANNED, date(2026, 11, 5)) == NOT_PLANNED


def test_empty_past_rows_turn_gray_and_future_linked_rows_are_planned(project):
    google = Sheets()
    headers = google.tables[0][:2]
    dates = [(date(2026, 11, day) - date(1899, 12, 30)).days for day in range(1, 6)] + ['06.11.2026']
    google.tables[0] = headers + [[PLANNED, day, 'День', '', ''] for day in dates] + [
        [PLANNED, '10.11.2026', 'Вторник', 'Задание без ссылки', ''],
        ['Материалы отправлены', '11.11.2026', 'Среда', append_materials('Задание', URL), ''],
        [APPROVED, '07.11.2026', 'Суббота', append_materials('Согласовано', URL), '']]
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 9))
    assert [row[0] for row in google.tables[0][2:8]] == ['Нет публикации'] * 6
    assert google.tables[0][8][0] == ''
    assert google.tables[0][9][0] == 'Запланировано'
    assert google.tables[0][10][0] == 'Вышло'
    cells = [request['updateCells'] for request in google.writes[-1] if 'updateCells' in request]
    for update in cells:
        if 2 <= update['range']['startRowIndex'] < 8:
            assert update['rows'][0]['values'][0]['userEnteredFormat']['backgroundColorStyle']['rgbColor'] == COLORS[NO_PUBLICATION]


def test_wrong_status_readback_does_not_report_success():
    google = Sheets()
    google.tables[0][2][1] = '06.11.2099'
    google.tables[0][3][1] = '07.11.2099'
    project, chosen = selected(google)
    original = google.request
    def overwrite(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == 'POST' and path.endswith(':batchUpdate'):
            google.tables[0][2][0] = 'Материалы отправлены'
        return result
    google.request = overwrite
    with pytest.raises(ValueError, match='Статус после записи не совпадает'):
        google.set_status(project, chosen, PLANNED, URL)


@pytest.mark.parametrize('description,current,expected', [
    ('', 'Дата прошла', 'Нет публикации'),
    ('  \n ', '', 'Нет публикации'),
    (append_materials('', URL), '', 'Нет публикации'),
    ('Задание', 'Дата прошла', 'Не запланировано'),
    ('Задание', 'Согласовано', 'Не запланировано'),
    (append_materials('Задание', URL), 'Запланировано', 'Вышло'),
    (append_materials('Задание', URL), 'Согласовано', 'Вышло'),
])
def test_past_status_uses_assignment_and_material_link(description, current, expected):
    assert publication_status(date(2026, 11, 8), description, current, date(2026, 11, 9)) == expected


def test_existing_old_dates_are_reclassified_and_future_statuses_preserved(project):
    google = Sheets()
    google.tables[0] = google.tables[0][:2] + [
        ['Дата прошла', '01.11.2026', 'День', '', 'Чужой канал'],
        ['Дата прошла', '02.11.2026', 'День', 'Задание', 'Чужой канал'],
        ['Согласовано', '03.11.2026', 'День', append_materials('Задание', URL), 'Чужой канал'],
        ['', '10.11.2026', 'День', 'Задание', 'Чужой канал'],
        ['Запланировано', '11.11.2026', 'День', append_materials('Задание', URL), 'Чужой канал'],
        ['Согласовано', '12.11.2026', 'День', append_materials('Задание', URL), 'Чужой канал']]
    before = deepcopy(google.tables[0])
    google.meta[0]['conditionalFormats'].append({
        'ranges': [{'sheetId': 0, 'startRowIndex': 2, 'startColumnIndex': 0, 'endColumnIndex': 1}],
        'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': 'Дата прошла'}]},
                        'format': {'backgroundColor': COLORS[NO_PUBLICATION]}}})
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 9))
    assert [row[0] for row in google.tables[0][2:]] == [
        'Нет публикации', 'Не запланировано', 'Вышло', '', 'Запланировано', 'Согласовано']
    assert [row[1:] for row in google.tables[0]] == [row[1:] for row in before]
    assert len(google.meta[0]['conditionalFormats']) == len(COLORS) + 1
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 9))
    assert len(google.writes) == 1
