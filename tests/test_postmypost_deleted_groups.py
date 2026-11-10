import pytest

from bmauto.postmypost import PostmypostError
from postmypost_fakes import connect_postmypost
from test_projects import Conversation, bot_at
from bmauto.statuses import APPROVED


def queued(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    api.projects_data.append({'id': 11, 'name': 'Другой проект Postmypost'})
    accounts = [{'id': 20, 'name': 'ВК', 'remote_project': 10}, {'id': 21, 'name': 'Telegram', 'remote_project': 11}]
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'remote_project': None, 'accounts': accounts}, 1)
    chat = Conversation(bot)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('edit_text')
    chat.send(text='Публикация')
    chat.send('publish')
    chat.send('approve')
    assert len(api.created) == 2
    return bot, chat, api, accounts


@pytest.mark.parametrize('removed', [0, 1])
@pytest.mark.parametrize('deleted_response', ['status-zero', 'http-404'])
def test_deleted_group_no_longer_blocks_remaining_accounts(tmp_path, project, removed, deleted_response):
    bot, chat, api, accounts = queued(tmp_path, project)
    dead_id = 91 + removed
    if deleted_response == 'status-zero':
        api.records[dead_id]['publication_status'] = 0
    else:
        original = api.status
        def status(ident):
            if ident == dead_id:
                raise PostmypostError('Запись не найдена', http_status=404)
            return original(ident)
        api.status = status
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'accounts': [accounts[1 - removed]]}, 2)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('approve')
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert draft['postmypost_id'] == 92 - removed
    assert bot.google.table[2][0] == APPROVED and len(api.created) == 2
    job = bot.store.pmp_job(key if removed == 0 else key + ':pmp:11')
    assert job['state'] == 'cancelled'


@pytest.mark.parametrize('response', ['active', '403', 'timeout', 'wrong-id'])
def test_active_or_unverified_group_is_not_silently_forgotten(tmp_path, project, response):
    bot, chat, api, accounts = queued(tmp_path, project)
    original = api.status
    def status(ident):
        if ident == 92:
            if response == '403':
                raise PostmypostError('Нет доступа', http_status=403)
            if response == 'timeout':
                raise PostmypostError('Сервис недоступен')
            if response == 'wrong-id':
                return {'id': 999, 'publication_status': 0}
        return original(ident)
    api.status = status
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'accounts': [accounts[0]]}, 2)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('approve')
    key, _ = bot.draft_for(bot.store.session(1), project)
    assert bot.store.pmp_job(key + ':pmp:11')['state'] == 'scheduled'
    assert len(api.created) == 2


def test_readding_previously_deleted_group_creates_one_replacement(tmp_path, project):
    bot, chat, api, accounts = queued(tmp_path, project)
    api.records[92]['publication_status'] = 0
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'accounts': [accounts[0]]}, 2)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('approve')
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'accounts': accounts}, 3)
    chat.send('project_home')
    chat.send('task', i=0)
    chat.send('approve')
    assert len(api.created) == 3 and api.records[93]['account_ids'] == [21]
    key, draft = bot.draft_for(bot.store.session(1), project)
    assert draft['postmypost_id'] == [91, 93]
    assert bot.store.pmp_job(key + ':pmp:11')['previous_ids'] == [92]
