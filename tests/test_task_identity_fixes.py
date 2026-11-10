from datetime import date, timedelta

from bmauto.core import Store, append_materials, tasks_from_rows
from bmauto.main import Bot
from bmauto.monitor import monitor_rows
from bmauto.postmypost import PostmypostScheduler
from postmypost_fakes import connect_postmypost
from test_projects import Conversation, bot_at


def prepared(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    api = connect_postmypost(bot, project)
    chat = Conversation(bot)
    chat.send('edit_text')
    chat.send(text='Исходный текст')
    chat.send('publish')
    key, draft = bot.draft_for(bot.store.session(1), project)
    return bot, chat, api, key, draft


def test_editing_assignment_keeps_draft_folder_and_existing_remote_post(tmp_path, project):
    bot, chat, api, key, before = prepared(tmp_path, project)
    chat.send('approve')
    url = f"https://drive.google.com/drive/folders/{before['folder']}"
    bot.google.table[2][4] = append_materials('Исправленное задание', url)
    chat.send('project_home')
    chat.send('task', i=0)
    new_key, saved = bot.draft_for(bot.store.session(1), project)
    assert new_key == key and saved['text'] == 'Исходный текст'
    chat.send('edit_text')
    chat.send(text='Обновлённая публикация')
    chat.send('publish')
    chat.send('approve')
    assert len(api.created) == 1 and len(api.updated) == 1
    assert bot.store.draft(key)['folder'] == before['folder']
    assert bot.google.table[2][4] == append_materials('Исправленное задание', url)
    assert bot.google.created == ['publication.docx']


def test_date_change_reuses_draft_and_reschedules_same_remote_id(tmp_path, project):
    bot, chat, api, key, before = prepared(tmp_path, project)
    chat.send('approve')
    old_day = date.fromisoformat(bot.store.session(1)['task']['day'])
    new_day = old_day + timedelta(days=1)
    bot.google.table[2][1] = new_day.strftime('%d.%m.%Y')
    chat.send('project_home')
    chat.send('task', i=0)
    assert bot.draft_for(bot.store.session(1), project)[0] == key
    chat.send('approve')
    assert len(api.created) == 1 and len(api.updated) == 1
    assert api.records[91]['post_at'].startswith(new_day.isoformat())
    assert bot.store.draft(key)['folder'] == before['folder']


def test_unpublished_draft_survives_assignment_edit(tmp_path, project):
    bot = bot_at(tmp_path, {'projects': [project]})
    chat = Conversation(bot)
    chat.send('project', p=project['id'])
    chat.send('task', i=0)
    chat.send('edit_text')
    chat.send(text='Ещё не отправленный текст')
    key, _ = bot.draft_for(bot.store.session(1), project)
    bot.google.table[2][3] = 'Новое описание задания'
    chat.send('project_home')
    chat.send('task', i=0)
    new_key, saved = bot.draft_for(bot.store.session(1), project)
    assert key == new_key and saved['text'] == 'Ещё не отправленный текст'


def test_old_drafts_are_indexed_from_saved_sessions_on_upgrade(tmp_path, project):
    bot, chat, api, key, before = prepared(tmp_path, project)
    with bot.store.db:
        bot.store.db.execute('DELETE FROM draft_tasks')  # State produced by the previous version.
    url = f"https://drive.google.com/drive/folders/{before['folder']}"
    bot.google.table[2][4] = append_materials('Изменено до обновления бота', url)
    store = Store(tmp_path)
    restarted = Bot({}, store, bot.vk, bot.google, bot.llm, bot.allowed,
                    scheduler=PostmypostScheduler(store, api.factory, bot.allowed))
    chat = Conversation(restarted)
    chat.send('project_home')
    chat.send('task', i=0)
    new_key, saved = restarted.draft_for(store.session(1), project)
    assert key == new_key and saved['folder'] == before['folder']
    assert saved['text'] == 'Исходный текст'


def test_other_project_with_same_material_link_cannot_load_private_draft(tmp_path, project):
    bot, chat, api, key, before = prepared(tmp_path, project)
    other = bot.store.save_project({**project, 'id': 'other', 'name': 'Другой'}, owner=1)
    chat.send('project', p=other['id'])
    chat.send('task', i=0)
    other_key, saved = bot.draft_for(bot.store.session(1), other)
    assert other_key != key and saved['text'] == '' and saved['folder'] is None


def test_changing_google_scope_does_not_reuse_draft_from_previous_account(tmp_path, project):
    bot, chat, api, key, before = prepared(tmp_path, project)
    current = bot.store.projects()[project['id']]
    changed = bot.store.save_project({**current, 'draft_scope': 'different-google-account'}, current['revision'])
    chat.send('project', p=project['id'])
    chat.send('task', i=0)
    new_key, saved = bot.draft_for(bot.store.session(1), changed)
    assert new_key != key and saved['text'] == '' and saved['folder'] is None


def test_reusing_row_for_different_task_without_link_does_not_overwrite_old_published_draft(tmp_path, project):
    bot, chat, api, key, before = prepared(tmp_path, project)
    bot.google.table[2][1] = (date.fromisoformat(bot.store.session(1)['task']['day']) + timedelta(days=1)).strftime('%d.%m.%Y')
    bot.google.table[2][4] = 'Совсем другое задание без ссылки'
    chat.send('project_home')
    chat.send('task', i=0)
    new_key, saved = bot.draft_for(bot.store.session(1), project)
    assert new_key != key and saved['text'] == ''
    assert bot.store.draft(key)['text'] == 'Исходный текст'


def test_times_are_sorted_chronologically_in_tasks_and_monitor():
    rows = [['План'], ['Статус', 'Дата', 'Время', 'День недели', 'Задание'],
            ['', '11.11.2099', '13:45', 'Среда', 'День'],
            ['', '11.11.2099', '9:00', 'Среда', 'Утро'],
            ['', '11.11.2099', '09:00:30', 'Среда', 'Через полминуты'],
            ['', '11.11.2099', '', 'Среда', 'Время пока не задано']]
    expected = ['9:00', '09:00:30', '13:45', '']
    assert [r.time for r in tasks_from_rows(rows, {}, date(2026, 11, 10))] == expected
    assert [r['time'] for r in monitor_rows(rows, {}, date(2026, 11, 10))] == expected


def test_approval_keeps_sending_latest_shared_version(tmp_path, project):
    bot, owner, api, key, before = prepared(tmp_path, project)
    bot.store.add_member(project['id'], 1, 3, bot.allowed)
    member = Conversation(bot, 3)
    member.send('project', p=project['id'])
    member.send('task', i=0)
    member.send('edit_text')
    member.send(text='Актуальная общая версия')
    member.send('publish')
    owner.send('approve')
    assert api.created[0]['details'][0]['content'] == 'Актуальная общая версия'
