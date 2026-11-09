import io
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bmauto.core import Store, append_materials
from bmauto.monitor import MonitorNotFound, MonitorService, MonitorUnavailable, monitor_rows, monitor_url, render_monitor
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


@pytest.fixture
def monitor(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project])
    token = store.monitor_token(project['id'])
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


def test_tokens_are_unique_persistent_and_do_not_change_project_revision(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project, {**project, 'id': 'other'}])
    first = store.monitor_token(project['id'])
    assert len(first) == 43 and first != store.monitor_token('other')
    assert Store(tmp_path).monitor_token(project['id']) == first
    assert store.projects()[project['id']]['revision'] == 1
    assert store.monitor_project(first)['id'] == project['id']
    assert store.monitor_project('invalid') is None


def test_month_data_and_cache_are_read_only(monitor):
    store, token, service, calls = monitor
    data = service.load(token, 0)
    assert data['name'] == 'Test' and data['sheet_id'] == 0
    assert [s['title'] for s in data['sheets']] == ['Ноябрь', 'Декабрь']
    assert [r['date'] for r in data['rows']] == sorted(r['date'] for r in data['rows'])
    assert data['rows'][2]['assignment'] == ''  # Dates without assignments are still visible on the dashboard.
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
    new_token = store.monitor_token('test', rotate=True)
    assert new_token != token
    with pytest.raises(MonitorNotFound):
        service.load(token, 0)
    assert service.load(new_token, 0)['name'] == 'Test'
    store.ensure_owners(1)
    store.delete_project('test', 1, {1}, store.projects()['test']['revision'])
    with pytest.raises(MonitorNotFound):
        service.load(new_token, 0)


def test_each_project_uses_its_own_google_connection(monitor):
    store, token, service, calls = monitor
    store.save_project({'id': 'other', 'name': 'Other', 'spreadsheet_id': 'other-sheet'})
    other = store.monitor_token('other')
    service.load(token, 0)
    service.load(other, 0)
    assert ('credentials', 'test') in calls and ('credentials', 'other') in calls
    assert ('rows', 'test', 0) in calls and ('rows', 'other', 0) in calls


def test_html_escapes_project_and_assignment_content_and_preserves_lines(monitor):
    _, token, service, _ = monitor
    data = service.load(token, 0)
    data['name'] = '<script>private</script>'
    data['rows'][0]['assignment'] = '<img src=x onerror=alert(1)>\nВторая строка'
    page = render_monitor(data, '/monitor/' + token)
    assert '<script>private</script>' not in page and '&lt;script&gt;private&lt;/script&gt;' in page
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


def test_members_can_get_monitor_link_but_only_owner_can_rotate(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    owner = Conversation(bot)
    owner.send('project', p=project['id'])
    owner.send('settings')
    assert 'monitor' in actions(bot)
    owner.send('monitor')
    original = bot.store.monitor_token(project['id'])
    assert '/monitor/' + original in bot.vk.messages[-1][0]
    owner.send('monitor_open')
    assert bot.vk.opened_url == 'https://bot.itiscat.ru/monitor/' + original
    member = Conversation(bot, 3)
    member.send('project', p=project['id'])
    member.send('monitor')
    assert 'monitor_rotate' not in actions(bot)
    member.send('monitor_rotate')
    assert bot.store.monitor_token(project['id']) == original
    owner.send('monitor_rotate')
    confirmation = bot.store.session(1)['monitor_confirmation']['token']
    owner.send('monitor_rotate_confirm', token=confirmation)
    assert bot.store.monitor_token(project['id']) != original
    assert bot.store.monitor_project(original) is None
    current = bot.store.monitor_token(project['id'])
    owner.send('monitor_rotate_confirm', token=confirmation)
    assert bot.store.monitor_token(project['id']) == current


def test_upstream_errors_do_not_expose_google_urls_or_credentials(monitor):
    _, token, service, _ = monitor
    class Failing:
        def __init__(self, path):
            raise RuntimeError('private credentials /secret/token.json and signed-url')
    service.google_factory = Failing
    with pytest.raises(MonitorUnavailable) as error:
        service.load(token, 0)
    assert '/secret' not in str(error.value) and 'signed-url' not in str(error.value)


def test_revocation_while_waiting_for_google_does_not_return_data(monitor):
    store, token, service, _ = monitor
    original = service.google_factory
    def reader(path):
        google = original(path)
        rows = google.rows
        def read(source):
            result = rows(source)
            store.monitor_token('test', rotate=True)
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
