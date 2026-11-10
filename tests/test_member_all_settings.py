import pytest

from bmauto.core import Store
from test_members import make_bot, add_through_menu
from test_projects import Conversation, actions


def test_member_can_add_and_remove_another_member(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    member = Conversation(bot, 3)
    member.send('project', p='test')
    member.send('members')
    member.send('member_add')
    member.send(text='4')
    assert bot.store.can_access('test', 4, bot.allowed)
    member.send('member_remove', user=4)
    member.send('member_remove_confirm', user=4)
    assert not bot.store.can_access('test', 4, bot.allowed)
    assert bot.store.can_access('test', 3, bot.allowed)


@pytest.mark.parametrize('action,payload', [
    ('project_edit', {'field': 'concept'}), ('project_edit', {'field': 'prompt'}),
    ('project_edit', {'field': 'spreadsheet_id'}), ('members', {}),
    ('project_delete', {}), ('project_monitor', {}), ('pmp', {})])
def test_settings_do_not_become_accessible_in_someone_elses_project(tmp_path, project, action, payload):
    bot = make_bot(tmp_path, [project])
    bot.store.save_project({'id': 'other', 'name': 'Доступный проект'}, owner=1)
    bot.store.add_member('other', 1, 3, bot.allowed)
    before = bot.store.projects()['test'].copy()
    bot.store.save_session(3, {'project': 'test', 'view': 'settings'})
    member = Conversation(bot, 3)
    member.send(action, **payload)
    assert bot.store.projects()['test'] == before
    assert 'wizard' not in bot.store.session(3)
    assert bot.store.session(3).get('project') != 'test'
    assert not bot.store.can_manage('test', 3, bot.allowed)


@pytest.mark.parametrize('field', ['concept', 'prompt', 'spreadsheet_id'])
def test_access_revocation_blocks_pending_setting_input(tmp_path, project, field):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    bot.store.save_project({'id': 'other', 'name': 'Другой'}, owner=1)
    bot.store.add_member('other', 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    member.send('project', p='test')
    member.send('project_edit', field=field)
    pending = bot.store.session(3)
    before = bot.store.projects()['test'].copy()
    bot.store.remove_member('test', 1, 3, bot.allowed)
    bot.store.save_session(3, pending)
    member.send(text='new-value')
    assert bot.store.projects()['test'] == before
    assert 'wizard' not in bot.store.session(3)


def test_member_can_resume_google_table_input_after_restart(tmp_path, project):
    bot = make_bot(tmp_path, [project])
    add_through_menu(bot)
    member = Conversation(bot, 3)
    member.send('project', p='test')
    member.send('project_edit', field='spreadsheet_id')
    restarted = make_bot(tmp_path, [])
    restarted.render(3)
    assert restarted.store.session(3)['view'] == 'edit'
    Conversation(restarted, 3).send(text='https://docs.google.com/spreadsheets/d/new-sheet/edit')
    assert restarted.store.projects()['test']['spreadsheet_id'] == 'new-sheet'
