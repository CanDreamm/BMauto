"""Stateful Postmypost test service using the official v4.1 response shape."""
from copy import deepcopy

from bmauto.postmypost import PostmypostError, UnknownPublication


class FakePostmypost:
    def __init__(self):
        self.records, self.created, self.updated, self.uploads = {}, [], [], []
        self.fail_create = False
        self.lose_create_response = False
        self.fail_status = False
        self.connected = True
        self.before_create = None
        self.draft_response = False
        self.projects_data = [{'id': 10, 'name': 'Бренд'}]

    def factory(self, token):
        assert token == 'test-token'
        return self

    def close(self):
        pass

    def projects(self):
        return deepcopy(self.projects_data)

    def channels(self):
        return [{'id': 1, 'code': 'vk'}, {'id': 2, 'code': 'telegram'}]

    def accounts(self, project):
        assert project == 10
        return [{'id': 20, 'chanel_id': 1, 'name': 'Сообщество ВК', 'connection_status': 1 if self.connected else 0},
                {'id': 21, 'chanel_id': 2, 'name': 'Канал Telegram', 'connection_status': 1}]

    def upload(self, project, path, mime):
        self.uploads.append((project, path.read_bytes(), mime))
        return 800 + len(self.uploads)

    def save(self, ident, payload):
        body = deepcopy(payload)
        body['id'] = ident
        for detail in body['details']:
            detail['files'] = [{'id': i} for i in detail.pop('file_ids')]
        self.records[ident] = body
        return deepcopy(body)

    def create(self, payload):
        if self.before_create:
            self.before_create()
        self.created.append(deepcopy(payload))
        if self.fail_create:
            raise PostmypostError('Postmypost: HTTP 422. Запрос отклонён.')
        ident = 90 + len(self.created)
        result = self.save(ident, payload)
        if self.draft_response:
            self.records[ident]['publication_status'] = result['publication_status'] = 4
        if self.lose_create_response:
            raise UnknownPublication('Результат неизвестен')
        return result

    def update(self, ident, payload):
        self.updated.append((ident, deepcopy(payload)))
        return self.save(ident, payload)

    def status(self, ident):
        if self.fail_status:
            raise PostmypostError('Не удалось проверить запись')
        return deepcopy(self.records[ident])


def connect_postmypost(bot, project, api=None):
    from test_projects import Conversation
    api = api or FakePostmypost()
    bot.scheduler.client_factory = api.factory
    bot.store.save_pmp_connection(project['id'], 1, bot.allowed, {
        'token': 'test-token', 'remote_project': 10, 'remote_name': 'Бренд',
        'accounts': [{'id': 20, 'name': 'ВК'}, {'id': 21, 'name': 'Telegram'}]}, 0)
    if 'Время' not in bot.google.table[1]:
        bot.google.table[1].insert(2, 'Время')
        bot.google.table[2].insert(2, '13:45')
    chat = Conversation(bot)
    chat.send('project', p=project['id'])
    chat.send('task', i=0)
    return api
