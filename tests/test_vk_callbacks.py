import json

import pytest

from bmauto.core import Store
from bmauto.main import Bot
from bmauto.vk import VK, VKAPIError, keyboard
from test_workflow import FakeGoogle, FakeLLM


class ChatAPI:
    def __init__(self):
        self.calls = []
        self.number = 100
        self.fail_send = False
        self.deny_incoming = False

    def __call__(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == 'messages.send':
            if self.fail_send:
                raise VKAPIError(method, 100, 'send failed')
            self.number += 1
            return [{'peer_id': int(kwargs.get('peer_ids', kwargs.get('peer_id', 1))),
                     'conversation_message_id': self.number, 'message_id': 0}]
        if method == 'messages.delete':
            if self.deny_incoming and kwargs.get('cmids') == '9' and kwargs['delete_for_all']:
                raise VKAPIError(method, 15, 'cannot delete incoming for all')
            return {kwargs.get('cmids', kwargs.get('message_ids')): 1}
        if method == 'messages.sendMessageEventAnswer':
            return 1
        raise AssertionError(method)


def client(tmp_path):
    vk = VK('test', 1)
    vk.bind_store(Store(tmp_path))
    api = ChatAPI()
    vk.call = api
    return vk, api


def event(action, number=1, **payload):
    return {'event_id': f'event-{number}', 'user_id': 1, 'peer_id': 1,
            'conversation_message_id': 100, 'payload': {'a': action, **payload}}


def test_every_button_is_callback_and_payload_contains_no_style_metadata():
    rows = keyboard([('Назад', {'a': 'projects'}), ('Отправить', {'a': 'publish', '_color': 'positive'}),
                     ('⠀', {'a': 'noop', '_callback': True})])['buttons']
    assert all(row[0]['action']['type'] == 'callback' for row in rows)
    assert json.loads(rows[1][0]['action']['payload']) == {'a': 'publish'}
    assert json.loads(rows[2][0]['action']['payload']) == {'a': 'noop'}


def test_new_screen_is_sent_before_previous_screen_is_deleted(tmp_path):
    vk, api = client(tmp_path)
    vk.begin_response(1)
    vk.send(1, 'Первое меню', [('Далее', {'a': 'project'})])
    assert vk.end_response(1)
    first = vk.store.chat_messages(1)[0]
    vk.begin_response(1)
    vk.send(1, 'Второе меню', [('Назад', {'a': 'projects'})])
    vk.end_response(1)
    assert [method for method, _ in api.calls] == ['messages.send', 'messages.send', 'messages.delete']
    assert api.calls[-1][1] == {'peer_id': 1, 'cmids': str(first['cmid']), 'delete_for_all': 1}
    assert len(vk.store.chat_messages(1)) == 1
    assert vk.store.chat_messages(1)[0]['key'] != first['key']


def test_cleanup_survives_restart_and_does_not_read_or_delete_old_history(tmp_path):
    vk, api = client(tmp_path)
    vk.begin_response(1)
    vk.send(1, 'Tracked menu')
    vk.end_response(1)
    tracked = vk.store.chat_messages(1)[0]
    restarted = VK('test', 1)
    restarted.bind_store(Store(tmp_path))
    restarted.call = api
    restarted.begin_response(1)
    restarted.send(1, 'New menu')
    restarted.end_response(1)
    deleted = [args['cmids'] for method, args in api.calls if method == 'messages.delete']
    assert deleted == [str(tracked['cmid'])]
    assert not any(method.startswith('messages.get') for method, _ in api.calls)


def test_notices_are_kept_with_menu_and_temporary_progress_is_deleted(tmp_path):
    vk, api = client(tmp_path)
    vk.begin_response(1)
    vk.send(1, 'Генерирую…', transient=True)
    vk.send(1, 'Причина ошибки')
    vk.send(1, 'Меню задания', [('Назад', {'a': 'projects'})])
    vk.end_response(1)
    sent = [args for method, args in api.calls if method == 'messages.send']
    assert [args['message'] for args in sent] == ['Генерирую…', 'Причина ошибки\n\nМеню задания']
    assert len(vk.store.chat_messages(1)) == 1
    assert any(method == 'messages.delete' for method, _ in api.calls)


def test_long_current_screen_keeps_all_chunks_and_its_keyboard(tmp_path):
    vk, api = client(tmp_path)
    vk.begin_response(1)
    text = 'Текст' * 2000
    vk.send(1, text, [('Назад', {'a': 'projects'})])
    vk.end_response(1)
    sent = [args for method, args in api.calls if method == 'messages.send']
    assert ''.join(item['message'] for item in sent) == text
    assert len(vk.store.chat_messages(1)) == len(sent) == 3
    assert all('keyboard' not in item for item in sent[:-1]) and 'keyboard' in sent[-1]
    assert not any(method == 'messages.delete' for method, _ in api.calls)


def test_failed_new_screen_does_not_delete_previous_or_input(tmp_path):
    vk, api = client(tmp_path)
    vk.begin_response(1)
    vk.send(1, 'Рабочее меню')
    vk.end_response(1)
    previous = vk.store.chat_messages(1)
    api.fail_send = True
    vk.begin_response(1)
    vk.send(1, 'Новое меню')
    assert not vk.end_response(1, incoming={'conversation_message_id': 9})
    assert vk.store.chat_messages(1) == previous
    assert not any(method == 'messages.delete' for method, _ in api.calls)


def test_input_is_deleted_after_success_and_community_only_fallback_is_supported(tmp_path):
    vk, api = client(tmp_path)
    api.deny_incoming = True
    vk.begin_response(1)
    vk.send(1, 'Ввод обработан', [])
    vk.end_response(1, incoming={'conversation_message_id': 9})
    deletes = [args for method, args in api.calls if method == 'messages.delete']
    assert deletes == [{'peer_id': 1, 'cmids': '9', 'delete_for_all': 1},
                       {'peer_id': 1, 'cmids': '9', 'delete_for_all': 0}]


def test_callbacks_reach_bot_and_are_deduplicated_by_event_not_message_id(tmp_path, project):
    vk, api = client(tmp_path)
    bot = Bot({'projects': [project]}, vk.store, vk, FakeGoogle(), FakeLLM(), {1})
    vk.handle_callback(event('project', 1, p='test'), bot.handle)
    vk.handle_callback(event('task', 2, i=0), bot.handle)
    assert bot.store.session(1)['view'] == 'task'
    count = len([method for method, _ in api.calls if method == 'messages.send'])
    vk.handle_callback(event('task', 2, i=0), bot.handle)
    assert len([method for method, _ in api.calls if method == 'messages.send']) == count
    vk.handle_callback(event('project_home', 3), bot.handle)
    assert bot.store.session(1)['view'] == 'dates'
    assert len(vk.store.chat_messages(1)) == 1


def test_blank_callback_only_acknowledges_and_never_sends_a_message(tmp_path):
    vk, api = client(tmp_path)
    vk.handle_callback(event('noop'), lambda message: pytest.fail('Blank must not dispatch'))
    assert [method for method, _ in api.calls] == ['messages.sendMessageEventAnswer']
    assert 'event_data' not in api.calls[0][1]


@pytest.mark.parametrize("action", ["google_open", "monitor_open"])
def test_browser_link_opens_from_callback_without_chat_message(tmp_path, action):
    vk, api = client(tmp_path)
    vk.handle_callback(event(action), lambda message: vk.open_callback_link(1, 'https://accounts.google.com/auth'))
    assert len(api.calls) == 1
    assert api.calls[0][0] == 'messages.sendMessageEventAnswer'
    assert json.loads(api.calls[0][1]['event_data']) == {'type': 'open_link', 'link': 'https://accounts.google.com/auth'}


def test_long_poll_enables_callback_events_without_turning_off_other_events():
    vk = VK('test', 1)
    calls = []
    def call(method, **kwargs):
        calls.append((method, kwargs))
        return {'is_enabled': True, 'api_version': '5.199', 'events': {'message_new': 1, 'message_event': 0, 'wall_post_new': 1}}
    vk.call = call
    vk.enable_callbacks()
    args = calls[-1][1]
    assert calls[-1][0] == 'groups.setLongPollSettings'
    assert args['message_new'] == args['message_event'] == args['wall_post_new'] == 1


def test_long_poll_dispatches_callback_events_alongside_text_messages(tmp_path):
    import httpx
    vk, api = client(tmp_path)
    handled = []
    original_call = vk.call
    def call(method, **kwargs):
        if method == 'groups.getLongPollSettings':
            return {'is_enabled': True, 'api_version': '5.199', 'events': {'message_new': 1, 'message_event': 1}}
        if method == 'groups.getLongPollServer':
            return {'server': 'https://vk.com/poll', 'key': 'test', 'ts': '1'}
        return original_call(method, **kwargs)
    class Poll:
        number = 0
        def get(self, *args, **kwargs):
            self.number += 1
            if self.number > 1:
                raise KeyboardInterrupt
            return httpx.Response(200, request=httpx.Request('GET', 'https://vk.com/poll'), json={
                'ts': '2', 'updates': [
                    {'type': 'message_new', 'object': {'message': {'id': 1, 'from_id': 1, 'peer_id': 1, 'text': 'Начать'}}},
                    {'type': 'message_event', 'object': event('projects')},
                    {'type': 'message_event', 'object': event('noop', 2)}]})
    vk.call, vk.http = call, Poll()
    with pytest.raises(KeyboardInterrupt):
        vk.run(handled.append)
    assert len(handled) == 2
    assert handled[0]['text'] == 'Начать'
    assert json.loads(handled[1]['payload']) == {'a': 'projects'}
    assert handled[1]['_callback_event_id'] == 'event-1'
    assert len([method for method, _ in api.calls if method == 'messages.sendMessageEventAnswer']) == 2
