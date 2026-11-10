from copy import deepcopy

from bmauto.core import Store
from bmauto.vk import keyboard
from postmypost_fakes import FakePostmypost
from test_projects import Conversation, actions, bot_at


def setup(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = FakePostmypost()
    bot.scheduler.client_factory = api.factory
    return bot, api, Conversation(bot)


def connect_personal(chat):
    chat.send('pmp_personal')
    chat.send('pmp_token')
    chat.send(text='test-token')


def choose(bot, chat, action, index=0):
    nonce = bot.store.session(chat.user)['pmp_choices']['nonce']
    chat.send(action, i=index, n=nonce)


def test_personal_token_is_saved_once_and_project_opens_social_networks_directly(tmp_path, project):
    bot, api, chat = setup(tmp_path, project)
    connect_personal(chat)
    assert bot.store.pmp_credential(1)['token'] == 'test-token'
    assert all('test-token' not in text for text, _ in bot.vk.messages)
    assert 'test-token' not in str(bot.store.session(1))
    chat.send('project', p=project['id'])
    chat.send('settings')
    chat.send('pmp')
    assert bot.store.session(1)['view'] == 'pmp_accounts'
    assert 'pmp_token' not in actions(bot) and 'pmp_project' not in actions(bot)
    choose(bot, chat, 'pmp_toggle', 0)
    choose(bot, chat, 'pmp_toggle', 1)
    choose(bot, chat, 'pmp_save')
    config = bot.store.pmp_connection(project['id'])
    assert 'token' not in config and config['credential_owner'] == 1
    assert [item['id'] for item in config['accounts']] == [20, 21]
    assert all(item['remote_project'] == 10 for item in config['accounts'])
    assert Store(tmp_path).pmp_connection(project['id']) == config
    assert 'Сообщество ВК' in bot.vk.messages[-1][0] and 'Канал Telegram' in bot.vk.messages[-1][0]
    chat.send('settings')
    assert bot.store.session(1)['view'] == 'settings'


def test_same_personal_connection_is_used_for_another_project_without_reentry(tmp_path, project):
    bot, api, chat = setup(tmp_path, project)
    connect_personal(chat)
    bot.store.save_project({'id': 'other', 'name': 'Другой'}, owner=1)
    for ident in (project['id'], 'other'):
        chat.send('project', p=ident)
        chat.send('pmp')
        choose(bot, chat, 'pmp_toggle', 0)
        choose(bot, chat, 'pmp_save')
        assert bot.store.pmp_connection(ident)['credential_owner'] == 1
        assert 'token' not in bot.store.pmp_connection(ident)
    assert bot.store.db.execute('SELECT COUNT(*) FROM pmp_credentials').fetchone()[0] == 1


def test_stale_selection_cannot_override_a_replaced_personal_token(tmp_path, project):
    bot, _, chat = setup(tmp_path, project)
    connect_personal(chat)
    chat.send('project', p=project['id'])
    chat.send('pmp')
    nonce = bot.store.session(1)['pmp_choices']['nonce']
    bot.store.save_pmp_credential(1, 'replacement', bot.allowed, 1)
    chat.send('pmp_toggle', i=0, n=nonce)
    assert bot.store.pmp_credential(1)['token'] == 'replacement'
    assert bot.store.pmp_connection(project['id']) is None


def test_non_manager_has_own_personal_token_but_cannot_change_project_accounts(tmp_path, project):
    bot, api, owner = setup(tmp_path, project)
    connect_personal(owner)
    owner.send('project', p=project['id'])
    owner.send('pmp')
    choose(bot, owner, 'pmp_toggle')
    choose(bot, owner, 'pmp_save')
    before = deepcopy(bot.store.pmp_connection(project['id']))
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    connect_personal(member)
    assert bot.store.pmp_credential(1)['revision'] == 1
    assert bot.store.pmp_credential(3)['revision'] == 1
    member.send('project', p=project['id'])
    member.send('settings')
    assert 'pmp' not in actions(bot)
    for action in ('pmp', 'pmp_accounts', 'pmp_toggle', 'pmp_save'):
        member.send(action, i=0, n='forged')
    assert bot.store.pmp_connection(project['id']) == before
    assert all('test-token' not in text for text, _ in bot.vk.messages)


def test_social_account_pagination_stays_within_vk_keyboard_limit(tmp_path, project):
    bot, api, chat = setup(tmp_path, project)
    api.projects_data = [{'id': i + 1, 'name': f'Проект {i + 1}'} for i in range(15)]
    connect_personal(chat)
    chat.send('project', p=project['id'])
    chat.send('pmp')
    for page in range(5):
        chat.send('pmp_page', page=page)
        assert len(keyboard(bot.vk.messages[-1][1])['buttons']) <= 10
    chat.send('pmp_home')
    assert bot.store.session(1)['view'] == 'pmp'
