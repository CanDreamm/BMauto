import io
import json
import sqlite3
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from bmauto.core import Store, append_materials
from bmauto.monitor import MonitorNotFound, MonitorService, monitor_rows, render_monitor
from bmauto.oauth_server import make_handler
from test_projects import Conversation, actions, bot_at


URL = 'https://drive.google.com/drive/folders/materials'
HEADERS = ['Статус', 'Дата', 'День недели', 'Задание', 'Другой канал']
ROWS = [['План'], HEADERS,
        ['Не запланировано', '01.11.2026', 'Воскресенье', 'Задание без материалов', 'Чужой текст'],
        ['Запланировано', '12.11.2026', 'Четверг', append_materials('Новая публикация\nВторая строка', URL), 'Чужой текст'],
        ['Нет публикации', '03.11.2026', 'Вторник', '', 'Чужой текст'],
        ['Согласовано', '10.11.2026', 'Вторник', append_materials('Согласованное задание', URL), ''],
        ['Вышло', '02.11.2026', 'Понедельник', append_materials('Готовая публикация', URL), '']]


@pytest.fixture(autouse=True)
def monitor_clock(monkeypatch):
    import bmauto.monitor as module
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 9, 12, tzinfo=tz)
    monkeypatch.setattr(module, 'datetime', Clock)


@pytest.fixture
def monitor(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project])
    store.ensure_owners(1)
    token = store.monitor_token(1)
    calls = []
    def factory():
        return SimpleNamespace(store=Store(tmp_path), db=sqlite3.connect(':memory:'), path_for=lambda p: p['id'])
    class Reader:
        def __init__(self, credentials):
            calls.append(('credentials', credentials))
        def worksheets(self, project):
            calls.append(('worksheets', project['id']))
            return [{'sheetId': 1, 'title': 'Декабрь'}, {'sheetId': 0, 'title': 'Ноябрь'}, {'sheetId': 9, 'title': 'Примечания'}]
        def rows(self, source):
            calls.append(('rows', source['id'], source['_sheet_id']))
            return deepcopy(ROWS) if source['_sheet_id'] == 0 else [['План'], HEADERS, ['', '01.12.2026', 'Вторник', 'Задание декабря']]
    return store, token, MonitorService(factory, Reader), calls


def test_personal_tokens_are_unique_persistent_and_do_not_change_project_revision(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project])
    store.ensure_owners(1)
    store.add_member(project['id'], 1, 3, {1})
    first = store.monitor_token(1)
    assert len(first) == 43 and first != store.monitor_token(3)
    assert Store(tmp_path).monitor_token(1) == first
    assert store.projects()[project['id']]['revision'] == 1
    assert store.monitor_user(first) == 1
    assert store.monitor_user('invalid') is None


def test_month_data_and_cache_are_read_only(monitor):
    store, token, service, calls = monitor
    data = service.load(token, 0)
    assert data['name'] == 'Test' and data['sheet_id'] == 0
    assert [s['title'] for s in data['sheets']] == ['Ноябрь', 'Декабрь']
    assert [r['date'] for r in data['rows']] == sorted(r['date'] for r in data['rows'])
    assert [r['date'] for r in data['rows']] == ['2026-11-10', '2026-11-12']
    assert data['rows'][-1]['materials'] == URL
    assert all('Чужой текст' not in str(row) for row in data['rows'])
    count = len(calls)
    assert service.load(token, 0) == data and len(calls) == count
    assert service.load(token, 1)['rows'][0]['assignment'] == 'Задание декабря'
    assert set(call[0] for call in calls) == {'credentials', 'worksheets', 'rows'}


def test_invalid_or_other_month_does_not_disclose_project(monitor):
    _, token, service, calls = monitor
    with pytest.raises(MonitorNotFound):
        service.load('A' * 43)
    assert not calls
    with pytest.raises(MonitorNotFound):
        service.load(token, 9)


def test_rotation_and_deletion_revoke_even_cached_pages(monitor, tmp_path):
    store, token, service, _ = monitor
    service.load(token, 0)
    new_token = store.monitor_token(1, rotate=True)
    assert new_token != token
    with pytest.raises(MonitorNotFound):
        service.load(token, 0)
    assert service.load(new_token, 0)['name'] == 'Test'
    store.ensure_owners(1)
    store.delete_project('test', 1, {1}, store.projects()['test']['revision'])
    with pytest.raises(MonitorNotFound):
        service.load(new_token, 0, project_id='test')
    assert service.load(new_token)['projects'] == []


def test_each_project_uses_its_own_google_connection(monitor):
    store, token, service, calls = monitor
    store.save_project({'id': 'other', 'name': 'Other', 'spreadsheet_id': 'other-sheet'}, owner=1)
    service.load(token, 0, project_id='test')
    data = service.load(token, 0, project_id='other')
    assert [p['id'] for p in data['projects']] == ['test', 'other']
    assert ('credentials', 'test') in calls and ('credentials', 'other') in calls
    assert ('rows', 'test', 0) in calls and ('rows', 'other', 0) in calls


def test_html_escapes_project_and_assignment_content_and_preserves_lines(monitor):
    _, token, service, _ = monitor
    data = service.load(token, 0)
    data['name'] = '<script>private</script>'
    data['projects'][0]['name'] = '<img src=external onerror=alert(1)>'
    data['rows'][0]['assignment'] = '<img src=x onerror=alert(1)>\nВторая строка'
    page = render_monitor(data, '/monitor/' + token)
    assert '<script>private</script>' not in page and '&lt;script&gt;private&lt;/script&gt;' in page
    assert '<img src=external' not in page and '&lt;img src=external onerror=alert(1)&gt;' in page
    assert '<img src=x' not in page and '&lt;img src=x onerror=alert(1)&gt;' in page
    assert '\nВторая строка' in page
    assert all(label in page for label in ['Статус', 'Дата', 'День', 'Задание', 'Ноябрь', 'Декабрь'])
    assert 'Чужой текст' not in page and 'contenteditable' not in page and '<form' not in page
    assert 'rel="noreferrer noopener"' in page


def request(service, path, method='GET'):
    handler_class = make_handler(lambda: pytest.fail('OAuth must not be called'), service)
    handler = handler_class.__new__(handler_class)
    handler.path, handler.command, handler.wfile = path, method, io.BytesIO()
    status, headers = [], {}
    handler.send_response = status.append
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    getattr(handler, 'do_' + method)()
    return status[0], headers, handler.wfile.getvalue().decode()


def test_http_is_read_only_and_secret_pages_are_not_cached_or_logged(monitor):
    _, token, service, _ = monitor
    path = '/monitor/' + token + '?month=0'
    status, headers, page = request(service, path)
    assert status == 200 and 'Test' in page
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert headers['X-Robots-Tag'] == 'noindex, nofollow, noarchive'
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert request(service, path, 'HEAD')[2] == ''
    for method in ['POST', 'PUT', 'PATCH', 'DELETE']:
        status, headers, _ = request(service, path, method)
        assert status == 405 and headers['Allow'] == 'GET, HEAD'
    status, _, body = request(service, '/monitor/' + 'A' * 43)
    assert status == 404 and 'Test' not in body
    assert request(service, path + '&month=1')[0] == 404
    assert request(service, '/monitor-assets/../core.py')[0] == 404
    assert request(service, '/monitor-assets/monitor.css')[1]['Content-Type'].startswith('text/css')


def test_header_row_three_and_only_columns_A_through_D_are_used():
    rows = [['Title'], [], HEADERS, ['Запланировано', '10.11.2026', 'Вторник', 'Задание', 'Не показывать']]
    result = monitor_rows(rows, {})
    assert result[0]['status'] == 'Запланировано'
    assert result[0]['assignment'] == 'Задание' and 'Не показывать' not in str(result)


def test_each_member_gets_and_rotates_only_their_own_link(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    owner = Conversation(bot)
    owner.send('projects')
    assert 'monitor' in actions(bot)
    owner.send('monitor')
    original = bot.store.monitor_token(1)
    assert '/monitor/' + original in bot.vk.messages[-1][0]
    owner.send('monitor_open')
    assert bot.vk.opened_url == 'https://bot.itiscat.ru/monitor/' + original
    member = Conversation(bot, 3)
    member.send('monitor')
    member_token = bot.store.monitor_token(3)
    assert member_token != original
    assert 'monitor_rotate' in actions(bot)
    member.send('monitor_rotate')
    confirmation = bot.store.session(3)['monitor_confirmation']['token']
    member.send('monitor_rotate_confirm', token=confirmation)
    assert bot.store.monitor_token(3) != member_token
    assert bot.store.monitor_user(member_token) is None
    assert bot.store.monitor_token(1) == original
    current = bot.store.monitor_token(3)
    member.send('monitor_rotate_confirm', token=confirmation)
    assert bot.store.monitor_token(3) == current


def test_upstream_errors_do_not_expose_google_urls_or_credentials(monitor):
    _, token, service, _ = monitor
    class Failing:
        def __init__(self, path):
            raise RuntimeError('private credentials /secret/token.json and signed-url')
    service.google_factory = Failing
    data = service.load(token, 0)
    assert '/secret' not in str(data) and 'signed-url' not in str(data)
    assert data['message'] and data['projects'] == [{'id': 'test', 'name': 'Test'}]


def test_revocation_while_waiting_for_google_does_not_return_data(monitor):
    store, token, service, _ = monitor
    original = service.google_factory
    def reader(path):
        google = original(path)
        rows = google.rows
        def read(source):
            result = rows(source)
            store.monitor_token(1, rotate=True)
            return result
        google.rows = read
        return google
    service.google_factory = reader
    with pytest.raises(MonitorNotFound):
        service.load(token, 0)
    assert not service.cache


def test_head_request_cannot_consume_google_oauth_code(monitor):
    _, _, service, _ = monitor
    status, headers, content = request(service, '/oauth/google/callback?state=do-not-consume&code=do-not-consume', 'HEAD')
    assert status == 405 and headers['Allow'] == 'GET' and content == ''


def test_monitor_navigation_cancels_pending_setting_input(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    chat = Conversation(bot)
    chat.send('project', p=project['id'])
    chat.send('project_edit', field='concept')
    chat.send('monitor')
    assert 'wizard' not in bot.store.session(1)
    chat.send(text='Этот текст не должен изменить концепцию')
    assert bot.store.projects()[project['id']]['concept'] == project['concept']


def test_manual_refresh_reads_fresh_google_data_even_inside_cache_window(monitor):
    _, token, service, calls = monitor
    service.load(token, 0)
    count = len(calls)
    assert request(service, '/monitor/' + token + '?month=0')[0] == 200
    assert len(calls) == count
    assert request(service, '/monitor/' + token + '?month=0&refresh=1')[0] == 200
    assert len(calls) > count


def test_personal_link_only_lists_member_projects_and_blocks_other_project_ids(monitor):
    store, _, service, calls = monitor
    store.save_project({'id': 'private', 'name': 'Чужой проект', 'spreadsheet_id': 'private-sheet'}, owner=2)
    store.add_member('test', 1, 3, {1})
    member_token = store.monitor_token(3)
    owner_token = store.monitor_token(2)
    assert service.load(owner_token, 0)['name'] == 'Чужой проект'
    data = service.load(member_token, 0)
    assert data['projects'] == [{'id': 'test', 'name': 'Test'}]
    assert data['project_id'] == 'test'
    count = len(calls)
    with pytest.raises(MonitorNotFound):
        service.load(member_token, 0, project_id='private')
    assert len(calls) == count
    assert 'Чужой проект' not in render_monitor(data, '/monitor/' + member_token)


def test_revoked_project_is_hidden_even_when_data_is_cached(monitor):
    store, _, service, _ = monitor
    store.save_project({'id': 'second', 'name': 'Второй', 'spreadsheet_id': 'second-sheet'}, owner=1)
    store.add_member('test', 1, 3, {1})
    store.add_member('second', 1, 3, {1})
    token = store.monitor_token(3)
    assert len(service.load(token, 0, project_id='test')['projects']) == 2
    store.remove_member('test', 1, 3, {1})
    with pytest.raises(MonitorNotFound):
        service.load(token, 0, project_id='test')
    data = service.load(token, 0)
    assert data['project_id'] == 'second'
    assert data['projects'] == [{'id': 'second', 'name': 'Второй'}]
    store.remove_member('second', 1, 3, {1})
    empty = service.load(token)
    assert empty['projects'] == [] and empty['rows'] == []
    assert 'Пока вам не назначены проекты' in empty['message']


def test_new_assignments_appear_without_changing_personal_link(monitor):
    store, token, service, _ = monitor
    assert len(service.load(token, 0)['projects']) == 1
    store.save_project({'id': 'new', 'name': 'Новый', 'spreadsheet_id': 'new-sheet'}, owner=2)
    store.add_member('new', 2, 1, {2})
    assert store.monitor_token(1) == token
    data = service.load(token, 0)
    assert [p['id'] for p in data['projects']] == ['test', 'new']
    assert service.load(token, 0, project_id='new')['name'] == 'Новый'


def test_old_project_links_do_not_become_personal_links(monitor):
    store, token, service, _ = monitor
    legacy = 'Z' * 43
    with store.db:
        store.db.execute('CREATE TABLE monitor_links (project_id TEXT PRIMARY KEY, token TEXT UNIQUE)')
        store.db.execute('INSERT INTO monitor_links VALUES (?,?)', ('test', legacy))
    with pytest.raises(MonitorNotFound):
        service.load(legacy, 0)
    assert service.load(token, 0)['name'] == 'Test'


def test_invalid_project_query_and_cross_project_month_ids_are_rejected(monitor):
    _, token, service, _ = monitor
    path = '/monitor/' + token
    assert request(service, path + '?project=private')[0] == 404
    assert request(service, path + '?project=test&project=private')[0] == 404
    assert request(service, path + '?project=test&month=9')[0] == 404
    assert request(service, path + '?project=test&month=0')[0] == 200


def test_failed_google_connection_does_not_hide_other_project_tabs(monitor):
    store, token, service, _ = monitor
    store.save_project({'id': 'unconfigured', 'name': 'Без таблицы'}, owner=1)
    data = service.load(token, project_id='unconfigured')
    assert data['message'] and data['rows'] == []
    assert [p['id'] for p in data['projects']] == ['test', 'unconfigured']
    page = render_monitor(data, '/monitor/' + token)
    assert '?project=test' in page and 'Без таблицы' in page
    assert service.load(token, 0, project_id='test')['rows']


def test_membership_revoked_while_google_is_loading_is_checked_again(monitor):
    store, _, service, _ = monitor
    store.add_member('test', 1, 3, {1})
    token = store.monitor_token(3)
    original = service.google_factory
    def reader(path):
        google = original(path)
        rows = google.rows
        def read(source):
            result = rows(source)
            store.remove_member('test', 1, 3, {1})
            return result
        google.rows = read
        return google
    service.google_factory = reader
    with pytest.raises(MonitorNotFound):
        service.load(token, 0, project_id='test')
    assert not service.cache


def test_personal_monitor_root_menu_stays_within_vk_keyboard_limit(tmp_path, project):
    from bmauto.vk import keyboard
    bot = bot_at(tmp_path, {'projects': [{**project, 'id': str(i), 'name': f'Проект {i}'} for i in range(24)]})
    chat = Conversation(bot)
    for page in range(4):
        chat.send('project_page', page=page)
        buttons = keyboard(bot.vk.messages[-1][1])['buttons']
        assert len(buttons) <= 10
        assert 'monitor' in actions(bot)


def test_monitor_includes_today_and_future_but_excludes_past_dates():
    rows = [['План'], HEADERS,
            ['Дата прошла', '08.11.2026', 'Воскресенье', 'Старое задание'],
            ['Запланировано', '09.11.2026', 'Понедельник', 'Сегодня'],
            ['', '10.11.2026', 'Вторник', 'Будущее задание'],
            ['', '11.11.2026', 'Среда', '']]
    result = monitor_rows(rows, {}, today=date(2026, 11, 9))
    assert [r['date'] for r in result] == ['2026-11-09', '2026-11-10', '2026-11-11']
    assert result[-1]['assignment'] == ''


def test_monitor_cache_does_not_keep_yesterdays_future_rows(monitor, monkeypatch):
    import bmauto.monitor as module
    _, token, service, calls = monitor
    assert len(service.load(token, 0)['rows']) == 2
    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 11, 0, tzinfo=tz)
    monkeypatch.setattr(module, 'datetime', NextDay)
    assert [r['date'] for r in service.load(token, 0)['rows']] == ['2026-11-12']
