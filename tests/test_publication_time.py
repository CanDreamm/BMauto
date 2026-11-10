from copy import deepcopy
from datetime import date
from urllib.parse import unquote

import pytest

from bmauto.core import append_materials, assignment_from_task, tasks_from_rows
from bmauto.google import Google
from bmauto.monitor import monitor_rows, render_monitor
from bmauto.statuses import APPROVED, EXPIRED, PLANNED
from test_projects import Conversation, bot_at
from test_statuses import Sheets

HEADERS = ['Статус', 'Дата', 'Время', 'День недели', 'Задание', 'Другой канал']
URL = 'https://drive.google.com/drive/folders/ready'


def time_rows():
    return [['План'], HEADERS, ['', '11.11.2099', '13:45', 'Среда', 'Задание для публикации', 'Не отправлять в ИИ']]


def selected(rows):
    t = tasks_from_rows(rows, {}, date(2026, 11, 1))[0]
    return dict(row=t.row, day=t.day.isoformat(), time=t.time, weekday=t.weekday,
                fields=t.fields, assignment=t.assignment, sheet_id=0, source_spreadsheet_id='sheet')


def test_time_column_moves_assignment_to_E_and_is_shown_with_task():
    rows = time_rows()
    t = tasks_from_rows(rows, {}, date(2026, 11, 1))[0]
    assert t.time == '13:45' and t.weekday == 'Среда'
    assert t.assignment == 'Задание для публикации'
    assert 'Время: 13:45' in t.description()
    assert 'Не отправлять в ИИ' not in t.description()
    fallback = {'fields': dict(zip(HEADERS, rows[2]))}
    assert assignment_from_task(fallback) == t.assignment
    rows[2][4] = ''
    assert tasks_from_rows(rows, {}, date(2026, 11, 1)) == []


def test_time_column_keeps_today_and_sorts_same_day_by_time():
    rows = time_rows()
    rows.append(['', '11.11.2099', '09:00', 'Среда', 'Утреннее задание', ''])
    tasks = tasks_from_rows(rows, {}, date(2099, 11, 11))
    assert [t.time for t in tasks] == ['09:00', '13:45']
    assert tasks_from_rows(rows, {}, date(2099, 11, 12)) == []


def test_monitor_uses_E_and_displays_time():
    rows = time_rows()
    rows[2][4] = append_materials(rows[2][4], URL)
    result = monitor_rows(rows, {}, today=date(2026, 11, 1))
    assert result[0]['time'] == '13:45' and result[0]['weekday'] == 'Среда'
    assert result[0]['materials'] == URL and result[0]['assignment'] == 'Задание для публикации'
    page = render_monitor({'name': 'Test', 'rows': result, 'sheets': [], 'sheet_id': None,
                           'month': 'Ноябрь', 'updated': '', 'projects': [], 'project_id': 'test'}, '/monitor/test')
    assert '13:45' in page and 'Не отправлять в ИИ' not in page


@pytest.mark.parametrize('native', [False, True])
def test_status_sync_uses_shifted_assignment_and_preserves_all_other_columns(project, native):
    google = Sheets()
    rows = time_rows()
    rows[2][4] = append_materials(rows[2][4], URL)
    if native:
        rows[2][1] = (date(2099, 11, 11) - date(1899, 12, 30)).days
        rows[2][2] = (13 * 60 + 45) / (24 * 60)
    google.tables[0] = deepcopy(rows)
    google.sync_statuses(project, google.worksheets(project), today=date(2026, 11, 1))
    assert google.tables[0][2][0] == PLANNED
    assert google.tables[0][2][1:] == rows[2][1:]
    google.sync_statuses(project, google.worksheets(project), today=date(2100, 1, 1))
    assert google.tables[0][2][0] == EXPIRED
    assert google.tables[0][2][1:] == rows[2][1:]


@pytest.mark.parametrize('by_id', [False, True])
def test_google_link_is_written_to_E_not_the_weekday_column(project, by_id):
    rows = time_rows()
    google = Google.__new__(Google)
    google.rows = lambda source: rows
    calls = []
    google.request = lambda method, path, **kwargs: calls.append((method, path, kwargs))
    source = {**project, '_expected_fields': dict(zip(HEADERS, rows[2])),
              '_expected_assignment': rows[2][4]}
    if by_id:
        source['_sheet_id'] = 0
    google.set_link(source, 3, HEADERS, URL)
    method, path, args = calls[0]
    if by_id:
        update = args['json']['requests'][0]['updateCells']
        assert update['range']['startColumnIndex'] == 4 and update['range']['endColumnIndex'] == 5
        assert update['rows'][0]['values'][0]['userEnteredValue']['stringValue'].endswith(URL)
    else:
        assert unquote(path).endswith("'Sheet'!E3")
    assert rows[2][3] == 'Среда'


def test_changed_time_blocks_approval_and_link_update(project):
    google = Sheets()
    google.tables[0] = time_rows()
    chosen = selected(google.tables[0])
    google.tables[0][2][4] = append_materials(google.tables[0][2][4], URL)
    google.tables[0][2][2] = '14:00'
    with pytest.raises(ValueError, match='изменилось'):
        google.set_status(project, chosen, APPROVED, URL)
    writer = Google.__new__(Google)
    writer.rows = lambda source: google.tables[0]
    writer.request = lambda *a, **kw: pytest.fail('Stale task must not write')
    with pytest.raises(ValueError, match='изменилась'):
        writer.set_link({**project, '_expected_fields': chosen['fields']}, 3, HEADERS, URL)
    assert not google.writes


def test_adding_time_column_preserves_existing_draft_identity(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    chat = Conversation(bot)
    chat.send('project', p=project['id'])
    chat.send('task', i=0)
    key, draft = bot.draft_for(bot.store.session(1), project)
    draft['text'] = 'Черновик до вставки времени'
    bot.store.save_draft(key, draft)
    bot.google.table[1].insert(2, 'Время')
    bot.google.table[2].insert(2, '13:45')
    chat.send('project_home')
    chat.send('task', i=0)
    new_key, saved = bot.draft_for(bot.store.session(1), project)
    assert key == new_key and saved['text'] == 'Черновик до вставки времени'
    assert 'Время: 13:45' in bot.vk.messages[-1][0]
