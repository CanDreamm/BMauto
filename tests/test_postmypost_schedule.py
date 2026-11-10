from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bmauto.core import Store
from bmauto.postmypost import PostmypostScheduler, PostmypostError, publication_at
from bmauto.statuses import APPROVED, PLANNED
from postmypost_fakes import connect_postmypost
from test_projects import Conversation, actions, bot_at


@pytest.fixture
def ready(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    chat = Conversation(bot)
    chat.send('edit_text')
    chat.send(text='Согласованный текст')
    chat.send('publish')
    assert bot.google.table[2][0] == PLANNED
    return bot, chat, api


def test_approve_schedules_multiple_social_networks_before_marking_green(ready, project):
    bot, chat, api = ready
    def assert_still_yellow():
        assert bot.google.table[2][0] == PLANNED
    api.before_create = assert_still_yellow
    chat.send('approve')
    assert bot.google.table[2][0] == APPROVED
    assert len(api.created) == 1
    payload = api.created[0]
    assert payload['account_ids'] == [20, 21] and payload['publication_status'] == 5
    assert len(payload['details']) == 2
    assert all(d['publication_type'] == 1 and d['content'] == 'Согласованный текст' for d in payload['details'])
    task = bot.store.session(1)['task']
    assert payload['post_at'] == task['day'] + 'T10:45:00+00:00'
    assert 'approve' not in actions(bot)
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert draft['postmypost_id'] == 91 and bot.store.pmp_job(key)['state'] == 'scheduled'
    chat.send('approve')  # Old callback must not create anything else.
    assert len(api.created) == 1


@pytest.mark.parametrize('reason', ['rejection', 'draft', 'status_read', 'disconnected'])
def test_no_green_status_when_service_has_not_confirmed_pending_publication(ready, reason):
    bot, chat, api = ready
    if reason == 'rejection':
        api.fail_create = True
    elif reason == 'draft':
        api.draft_response = True
    elif reason == 'status_read':
        api.fail_status = True
    else:
        api.connected = False
    chat.send('approve')
    assert bot.google.table[2][0] == PLANNED
    assert 'approve' in actions(bot)


def test_lost_create_response_and_restart_never_create_duplicate(ready, project):
    bot, chat, api = ready
    api.lose_create_response = True
    chat.send('approve')
    assert len(api.created) == 1 and bot.google.table[2][0] == PLANNED
    bot.scheduler = PostmypostScheduler(Store(bot.store.root), api.factory)
    chat.send('approve')
    assert len(api.created) == 1 and bot.google.table[2][0] == PLANNED
    assert any('неизвестный результат' in text for text, _ in bot.vk.messages[-3:])


def test_failure_saving_sheet_status_reuses_known_remote_id(ready):
    bot, chat, api = ready
    original = bot.google.set_status
    def broken(*args):
        raise ValueError('Ошибка таблицы')
    bot.google.set_status = broken
    chat.send('approve')
    assert len(api.created) == 1 and bot.google.table[2][0] == PLANNED
    bot.google.set_status = original
    chat.send('approve')
    assert len(api.created) == 1 and bot.google.table[2][0] == APPROVED


def test_existing_remote_post_is_updated_after_edit_instead_of_created_again(ready):
    bot, chat, api = ready
    chat.send('approve')
    chat.send('edit_text')
    chat.send(text='Исправленный текст')
    chat.send('publish')
    chat.send('approve')
    assert len(api.created) == 1 and len(api.updated) == 1
    assert api.updated[0][0] == 91
    assert bot.google.table[2][0] == APPROVED
    assert api.records[91]['details'][0]['content'] == 'Исправленный текст'


def test_all_photos_are_uploaded_and_attached_to_every_selected_account(ready, project):
    bot, chat, api = ready
    key, draft = bot.draft_for(bot.store.session(1), project)
    for i in range(3):
        path = f'photo_{i}.jpg'
        (bot.store.root / path).write_bytes(b'photo' + bytes([i]))
        draft['photos'].append({'id': str(i), 'path': path, 'mime': 'image/jpeg'})
    bot.store.save_draft(key, draft)
    chat.send('approve')
    assert len(api.uploads) == 3
    assert all(detail['file_ids'] == [801, 802, 803] for detail in api.created[0]['details'])


@pytest.mark.parametrize('change', ['time', 'assignment', 'targets'])
def test_stale_task_never_schedules_to_changed_time_or_recipients(ready, project, change):
    bot, chat, api = ready
    if change == 'time':
        bot.google.table[2][2] = '14:15'
    elif change == 'assignment':
        bot.google.table[2][4] = 'Другое задание'
    else:
        bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'accounts': [{'id': 21, 'name': 'Telegram'}]}, 1)
    chat.send('approve')
    assert not api.created and bot.google.table[2][0] == PLANNED


def test_connection_is_required_before_approval(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    chat = Conversation(bot)
    chat.send('project', p=project['id'])
    chat.send('task', i=0)
    chat.send('edit_text')
    chat.send(text='Текст')
    chat.send('publish')
    chat.send('approve')
    assert bot.google.table[2][0] == PLANNED
    assert any('Подключите Postmypost' in text for text, _ in bot.vk.messages[-3:])


def test_task_uses_project_specific_token_and_destinations(ready, project):
    bot, chat, api = ready
    bot.store.save_project({'id': 'other', 'name': 'Другой', 'spreadsheet_id': 'other'}, owner=1)
    bot.store.save_pmp_connection('other', 1, bot.allowed, {'token': 'other-private-token', 'remote_project': 999,
                                   'accounts': [{'id': 9999, 'name': 'Чужая площадка'}]}, 0)
    chat.send('approve')
    assert api.created[0]['project_id'] == 10 and api.created[0]['account_ids'] == [20, 21]
    assert all('other-private-token' not in text for text, _ in bot.vk.messages)


def test_claim_prevents_concurrent_creation_for_same_draft(ready, project):
    bot, _, _ = ready
    key, _ = bot.draft_for(bot.store.session(1), project)
    job = {'state': 'sending', 'id': None}
    bot.store.claim_pmp_job(key, project['id'], None, job, 1)
    other = Store(bot.store.root)
    with pytest.raises(ValueError, match='обрабатывается'):
        other.claim_pmp_job(key, project['id'], None, job, 1)


@pytest.mark.parametrize('clock', ['', '24:00', '13:65', 'tomorrow'])
def test_missing_or_invalid_publication_time_is_rejected(clock):
    with pytest.raises(PostmypostError, match='ЧЧ:ММ'):
        publication_at({'day': '2099-11-11', 'time': clock})


def test_publication_time_is_interpreted_in_moscow_and_past_time_rejected():
    now = datetime(2026, 11, 10, 13, 0, tzinfo=ZoneInfo('Europe/Moscow'))
    assert publication_at({'day': '2026-11-10', 'time': '13:45'}, now) == '2026-11-10T10:45:00+00:00'
    with pytest.raises(PostmypostError, match='прошли'):
        publication_at({'day': '2026-11-10', 'time': '12:45'}, now)


def test_time_change_updates_existing_pending_publication(ready, project):
    bot, chat, api = ready
    chat.send('approve')
    assert bot.google.table[2][0] == APPROVED
    bot.google.table[2][2] = '14:15'
    chat.send('project_home')
    chat.send('task', i=0)
    assert 'approve' in actions(bot)
    chat.send('approve')
    assert len(api.created) == 1 and len(api.updated) == 1
    assert api.records[91]['post_at'].endswith('T11:15:00+00:00')


def test_revocation_while_remote_post_is_created_does_not_mark_sheet_green(ready, project):
    bot, owner, api = ready
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    member.send('project', p=project['id'])
    member.send('task', i=0)
    api.before_create = lambda: bot.store.remove_member(project['id'], 1, 3, bot.allowed)
    member.send('approve')
    assert bot.google.table[2][0] == PLANNED
    assert len(api.created) == 1


@pytest.mark.parametrize('clock', ['13:45', '13:45:00'])
def test_formatted_google_time_with_optional_seconds(clock):
    now = datetime(2026, 11, 10, 13, 0, tzinfo=ZoneInfo('Europe/Moscow'))
    assert publication_at({'day': '2026-11-10', 'time': clock}, now) == '2026-11-10T10:45:00+00:00'


def test_retry_after_failed_readback_checks_existing_entry_and_marks_green(ready, project):
    bot, chat, api = ready
    api.fail_status = True
    chat.send('approve')
    assert bot.google.table[2][0] == PLANNED and len(api.created) == 1
    api.fail_status = False
    chat.send('approve')
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert len(api.created) == 1 and bot.google.table[2][0] == APPROVED
    assert bot.store.pmp_job(key)['state'] == 'scheduled'
