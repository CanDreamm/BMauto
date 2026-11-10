from copy import deepcopy

from bmauto.core import Store
from bmauto.vk import keyboard
from postmypost_fakes import FakePostmypost
from test_projects import Conversation, actions, bot_at


def setup(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = FakePostmypost()
    bot.scheduler.client_factory = api.factory
    chat = Conversation(bot)
    chat.send('project', p=project['id'])
    chat.send('settings')
    return bot, api, chat


def choose(bot, chat, action, index=0):
    nonce = bot.store.session(chat.user)['pmp_choices']['nonce']
    chat.send(action, i=index, n=nonce)


def test_token_and_multiple_social_networks_are_configured_in_bot_and_persist(tmp_path, project):
    bot, api, chat = setup(tmp_path, project)
    assert 'pmp' in actions(bot)
    chat.send('pmp')
    chat.send('pmp_token')
    chat.send(text='test-token')
    assert all('test-token' not in text for text, _ in bot.vk.messages)
    assert 'test-token' not in str(bot.store.session(1))
    chat.send('pmp_projects')
    choose(bot, chat, 'pmp_project')
    choose(bot, chat, 'pmp_toggle', 0)
    choose(bot, chat, 'pmp_toggle', 1)
    choose(bot, chat, 'pmp_save')
    config = bot.store.pmp_connection(project['id'])
    assert config['remote_project'] == 10
    assert [item['id'] for item in config['accounts']] == [20, 21]
    assert Store(tmp_path).pmp_connection(project['id']) == config
    assert 'Сообщество ВК' in bot.vk.messages[-1][0] and 'Канал Telegram' in bot.vk.messages[-1][0]
    chat.send('settings')
    assert bot.store.session(1)['view'] == 'settings'


def test_stale_selection_cannot_override_changed_postmypost_token(tmp_path, project):
    bot, _, chat = setup(tmp_path, project)
    chat.send('pmp_token')
    chat.send(text='test-token')
    chat.send('pmp_projects')
    nonce = bot.store.session(1)['pmp_choices']['nonce']
    before = bot.store.pmp_connection(project['id'])
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {'token': 'replacement'}, before['revision'])
    chat.send('pmp_project', i=0, n=nonce)
    assert bot.store.pmp_connection(project['id'])['token'] == 'replacement'
    assert bot.store.pmp_connection(project['id']).get('remote_project') is None


def test_non_manager_cannot_read_replace_or_attach_project_token(tmp_path, project):
    bot, api, owner = setup(tmp_path, project)
    owner.send('pmp_token')
    owner.send(text='test-token')
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    before = deepcopy(bot.store.pmp_connection(project['id']))
    member = Conversation(bot, 3)
    member.send('project', p=project['id'])
    member.send('settings')
    assert 'pmp' not in actions(bot)
    for action in ('pmp', 'pmp_token', 'pmp_projects', 'pmp_project', 'pmp_toggle', 'pmp_save'):
        member.send(action, i=0, n='forged')
    assert bot.store.pmp_connection(project['id']) == before
    assert all('test-token' not in text for text, _ in bot.vk.messages)


def test_project_choice_pagination_stays_within_vk_keyboard_limit(tmp_path, project):
    bot, api, chat = setup(tmp_path, project)
    api.projects_data = [{'id': i + 1, 'name': f'Проект {i + 1}'} for i in range(30)]
    chat.send('pmp_token')
    chat.send(text='test-token')
    chat.send('pmp_projects')
    for page in range(5):
        chat.send('pmp_page', page=page)
        assert len(keyboard(bot.vk.messages[-1][1])['buttons']) <= 10
    chat.send('pmp')
    assert bot.store.session(1)['view'] == 'pmp'
