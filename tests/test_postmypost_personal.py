import json
import sqlite3
from copy import deepcopy

import pytest

from bmauto.core import Store
from bmauto.postmypost import PostmypostScheduler
from postmypost_fakes import connect_postmypost
from test_projects import Conversation, bot_at
from bmauto.statuses import APPROVED, PLANNED


def test_legacy_project_tokens_move_to_one_owner_record_without_losing_targets(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project])
    store.ensure_owners(1)
    store.save_project({'id': 'other', 'name': 'Другой'}, owner=1)
    old = {'revision': 3, 'credential_owner': 1, 'token': 'old-token', 'remote_project': 10, 'accounts': [{'id': 20, 'name': 'ВК'}]}
    with store.db:
        for ident in (project['id'], 'other'):
            store.db.execute('INSERT INTO pmp_connections VALUES (?,?)', (ident, json.dumps(old)))
    restored = Store(tmp_path)
    assert restored.pmp_credential(1) == {'token': 'old-token', 'revision': 1}
    assert restored.db.execute('SELECT COUNT(*) FROM pmp_credentials').fetchone()[0] == 1
    for ident in (project['id'], 'other'):
        config = restored.pmp_connection(ident)
        assert 'token' not in config and config['credential_owner'] == 1
        assert config['revision'] == 3 and config['accounts'][0]['id'] == 20
    backups = list(tmp_path.glob('.pmp-migration-*.sqlite3'))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    assert Store(tmp_path).pmp_credential(1)['token'] == 'old-token'
    assert len(list(tmp_path.glob('.pmp-migration-*.sqlite3'))) == 1


def test_conflicting_old_tokens_are_not_silently_assigned_to_wrong_personal_account(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project])
    store.ensure_owners(1)
    store.save_project({'id': 'other', 'name': 'Другой'}, owner=1)
    with store.db:
        for ident, token in ((project['id'], 'old-one'), ('other', 'old-two')):
            store.db.execute('INSERT INTO pmp_connections VALUES (?,?)', (ident, json.dumps({'revision': 1, 'token': token, 'accounts': []})))
    restored = Store(tmp_path)
    assert restored.pmp_credential(1) is None
    assert 'token' not in restored.pmp_connection(project['id'])
    backup = sqlite3.connect(next(tmp_path.glob('.pmp-migration-*.sqlite3')))
    assert {json.loads(row[0])['token'] for row in backup.execute('SELECT body FROM pmp_connections')} == {'old-one', 'old-two'}
    backup.close()


def test_deleting_project_keeps_personal_token_and_other_project_binding(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    connect_postmypost(bot, project)
    bot.store.save_project({'id': 'other', 'name': 'Другой'}, owner=1)
    bot.store.save_pmp_connection('other', 1, bot.allowed, {'credential_owner': 1, 'remote_project': 10, 'accounts': [{'id': 21, 'name': 'Telegram'}]}, 0)
    bot.store.delete_project(project['id'], 1, bot.allowed, bot.store.projects()[project['id']]['revision'])
    assert bot.store.pmp_credential(1)['token'] == 'test-token'
    assert bot.store.pmp_connection('other')['credential_owner'] == 1


def test_rotation_updates_personal_revision_without_copying_tokens_into_projects(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    connect_postmypost(bot, project)
    before = deepcopy(bot.store.pmp_connection(project['id']))
    stamp = bot.scheduler.settings_stamp(project)
    bot.store.save_pmp_credential(1, 'new-token', bot.allowed, 1)
    assert bot.scheduler.settings_stamp(project) != stamp
    assert bot.store.pmp_connection(project['id']) == before
    assert 'token' not in before


def test_member_approval_uses_destination_owners_token_instead_of_members_token(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    owner = Conversation(bot)
    owner.send('edit_text')
    owner.send(text='Текст')
    owner.send('publish')
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    bot.store.save_pmp_credential(3, 'members-private-token', bot.allowed, 0)
    member = Conversation(bot, 3)
    member.send('project', p=project['id'])
    member.send('task', i=0)
    member.send('approve')
    assert bot.google.table[2][0] == APPROVED and len(api.created) == 1
    assert all('members-private-token' not in text for text, _ in bot.vk.messages)


def test_token_change_invalidates_open_task_before_any_remote_write(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    chat = Conversation(bot)
    chat.send('edit_text')
    chat.send(text='Текст')
    chat.send('publish')
    bot.store.save_pmp_credential(1, 'replacement', bot.allowed, 1)
    chat.send('approve')
    assert not api.created and bot.google.table[2][0] == PLANNED


def test_cannot_bind_another_users_personal_token_to_project(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    bot.store.save_pmp_credential(2, 'someone-elses-token', bot.allowed, 0)
    with pytest.raises(ValueError, match='ваш личный токен'):
        bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'credential_owner': 2, 'accounts': [{'id': 20}]}, 0)


def test_personal_menu_ignores_stale_selected_project_after_access_revocation(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    bot.store.save_project({'id': 'other', 'name': 'Другой'}, owner=1)
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    bot.store.add_member('other', 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    member.send('project', p=project['id'])
    stale = bot.store.session(3)
    bot.store.remove_member(project['id'], 1, 3, bot.allowed)
    bot.store.save_session(3, stale)
    member.send('pmp_personal')
    assert bot.store.session(3)['view'] == 'pmp_personal'
    assert 'project' not in bot.store.session(3)


def test_multiple_remote_projects_are_scheduled_with_one_personal_token(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    api.projects_data.append({'id': 11, 'name': 'Второй проект Postmypost'})
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'remote_project': None, 'accounts': [
        {'id': 20, 'name': 'ВК', 'remote_project': 10}, {'id': 21, 'name': 'Telegram', 'remote_project': 11}]}, 1)
    chat = Conversation(bot)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('edit_text')
    chat.send(text='Текст')
    chat.send('publish')
    chat.send('approve')
    assert bot.google.table[2][0] == APPROVED
    assert [(p['project_id'], p['account_ids']) for p in api.created] == [(10, [20]), (11, [21])]
    _, draft = bot.draft_for(bot.store.session(1), project)
    assert draft['postmypost_id'] == [91, 92]
    chat.send('approve')
    assert len(api.created) == 2


def test_partial_remote_failure_retries_only_missing_group_and_never_marks_green_early(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    api.projects_data.append({'id': 11, 'name': 'Второй проект'})
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'remote_project': None, 'accounts': [
        {'id': 20, 'name': 'ВК', 'remote_project': 10}, {'id': 21, 'name': 'Telegram', 'remote_project': 11}]}, 1)
    chat = Conversation(bot)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('edit_text')
    chat.send(text='Текст')
    chat.send('publish')
    def fail_second_group():
        api.fail_create = len(api.created) == 1
    api.before_create = fail_second_group
    chat.send('approve')
    assert bot.google.table[2][0] == PLANNED and len(api.records) == 1
    api.before_create, api.fail_create = None, False
    chat.send('approve')
    assert bot.google.table[2][0] == APPROVED and len(api.records) == 2
    assert len([p for p in api.created if p['project_id'] == 10]) == 1


def test_legacy_token_with_unknown_author_is_not_assigned_to_project_owner(tmp_path, project):
    store = Store(tmp_path)
    store.seed_projects([project])
    store.ensure_owners(1)
    with store.db:
        store.db.execute('INSERT INTO pmp_connections VALUES (?,?)', (project['id'], json.dumps({
            'revision': 1, 'token': 'unknown-persons-token', 'remote_project': 10, 'accounts': [{'id': 20, 'name': 'ВК'}]})))
    restored = Store(tmp_path)
    assert restored.pmp_credential(1) is None
    assert 'token' not in restored.pmp_connection(project['id'])
    assert restored.pmp_connection(project['id'])['accounts'][0]['id'] == 20
    assert list(tmp_path.glob('.pmp-migration-*.sqlite3'))


def test_revocation_during_token_check_does_not_save_key_or_break_error_screen(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    from postmypost_fakes import FakePostmypost
    api = FakePostmypost()
    bot.scheduler.client_factory = api.factory
    def revoke_while_checking():
        bot.store.remove_member(project['id'], 1, 3, bot.allowed)
        return []
    api.projects = revoke_while_checking
    member = Conversation(bot, 3)
    member.send('pmp_token')
    member.send(text='test-token')
    assert bot.store.pmp_credential(3) is None
    assert all('test-token' not in text for text, _ in bot.vk.messages)
