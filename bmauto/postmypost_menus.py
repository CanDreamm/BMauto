"""VK setup of a project's Postmypost token and multiple destinations."""
import secrets

from .vk import back_footer

SCREENS = {'pmp', 'pmp_token', 'pmp_projects', 'pmp_accounts'}


class PostmypostMenus:
    def __init__(self, store, vk, admins, scheduler):
        self.store, self.vk, self.admins, self.scheduler = store, vk, admins, scheduler

    def config(self, project):
        return self.store.pmp_connection(project['id']) or {'revision': 0}

    def require(self, user, session):
        ident = session.get('project')
        if not self.store.can_manage(ident, user, self.admins):
            raise ValueError('Postmypost настраивает владелец проекта или администратор')
        return self.store.projects()[ident]

    def fetch(self, config, method, *args):
        api = self.scheduler.client_factory(config['token'])
        try:
            return getattr(api, method)(*args)
        finally:
            api.close()

    def show(self, user, project):
        config = self.config(project)
        session = self.store.session(user)
        session.update(view='pmp', mode=None)
        session.pop('pmp_choices', None)
        self.store.save_session(user, session)
        names = '\n'.join('• ' + item['name'] for item in config.get('accounts', [])) or 'Соцсети ещё не выбраны'
        message = f"Postmypost проекта «{project['name']}»\n\n"
        message += ('Токен подключён' if config.get('token') else 'Токен не подключён')
        message += '\nПроект Postmypost: ' + config.get('remote_name', 'Не выбран') + '\n\n' + names
        buttons = [('Заменить токен' if config.get('token') else 'Подключить Postmypost', {'a': 'pmp_token'})]
        if config.get('token'):
            buttons.append(('Выбрать проект и соцсети', {'a': 'pmp_projects'}))
        self.vk.send(user, message, buttons + [back_footer({'a': 'settings'})])

    def render(self, user, session):
        project = self.require(user, session)
        if session.get('view') == 'pmp_token':
            self.vk.send(user, 'Отправьте API-токен Postmypost одним сообщением. Он сохраняется в закрытой базе бота и не показывается в меню. Соцсети сначала должны быть подключены в кабинете Postmypost. После замены токена выберите соцсети заново; уже созданные публикации останутся в Postmypost.', [back_footer({'a': 'pmp'})])
        elif (session.get('view') in {'pmp_projects', 'pmp_accounts'} and session.get('pmp_choices')
              and session['pmp_choices']['revision'] == self.config(project)['revision']):
            self.choices(user, session)
        else:
            self.show(user, project)

    def choices(self, user, session):
        choice = session['pmp_choices']
        if choice['project'] != session.get('project') or choice['revision'] != self.config(self.require(user, session))['revision']:
            raise ValueError('Настройки Postmypost изменились. Откройте список заново')
        page = max(0, min(session.get('pmp_page', 0), max(0, (len(choice['items']) - 1) // 6)))
        session['pmp_page'] = page
        accounts = session['view'] == 'pmp_accounts'
        selected = session.get('pmp_selected', [])
        buttons = []
        for index in range(page * 6, min(len(choice['items']), (page + 1) * 6)):
            item = choice['items'][index]
            label = ('☑ ' if item['id'] in selected else '☐ ') + item['name'] if accounts else item['name']
            buttons.append((label, {'a': 'pmp_toggle' if accounts else 'pmp_project', 'i': index, 'n': choice['nonce']}))
        pages = []
        if page:
            pages.append(('Назад', {'a': 'pmp_page', 'page': page - 1}))
        if (page + 1) * 6 < len(choice['items']):
            pages.append(('Далее', {'a': 'pmp_page', 'page': page + 1}))
        if pages:
            buttons.append(pages)
        buttons.append(back_footer({'a': 'pmp_projects' if accounts else 'pmp'},
                       ('Сохранить', {'a': 'pmp_save', 'n': choice['nonce'], '_color': 'positive'}) if accounts else None))
        self.store.save_session(user, session)
        title = f"Выберите соцсети. Выбрано: {len(selected)}" if accounts else 'Выберите проект Postmypost'
        if not choice['items']:
            title += '\nДоступных записей нет. Подключите соцсети в кабинете Postmypost.'
        self.vk.send(user, title, buttons)

    def handle(self, user, action, payload, text, session):
        token_input = session.get('mode') == 'pmp_token' and not action
        if not token_input and (not isinstance(action, str) or not action.startswith('pmp')):
            return False
        project = self.require(user, session)
        config = self.config(project)
        if action in {'pmp', 'pmp_token', 'pmp_projects'}:
            for key in ('wizard', 'member_input', 'member_remove', 'delete_confirmation'):
                session.pop(key, None)
            session['mode'] = None
        if action == 'pmp':
            self.store.save_session(user, session)
            self.show(user, project)
        elif action == 'pmp_token':
            session.update(mode='pmp_token', view='pmp_token', pmp_revision=config['revision'])
            self.store.save_session(user, session)
            self.render(user, session)
        elif token_input:
            if not text or len(text) > 8192 or any(c.isspace() for c in text):
                raise ValueError('Отправьте API-токен Postmypost одним сообщением без пробелов')
            self.fetch({'token': text}, 'projects')  # Validate access before saving the secret.
            self.store.save_pmp_connection(project['id'], user, self.admins,
                {'token': text, 'remote_project': None, 'remote_name': 'Не выбран', 'accounts': []}, session['pmp_revision'])
            session['mode'] = None
            self.store.save_session(user, session)
            self.show(user, project)
        elif action == 'pmp_projects':
            if not config.get('token'):
                raise ValueError('Сначала подключите токен Postmypost')
            items = [{'id': item['id'], 'name': str(item.get('name') or item['id'])}
                     for item in self.fetch(config, 'projects') if type(item.get('id')) is int and item['id'] > 0]
            session.update(view='pmp_projects', pmp_page=0, pmp_choices={
                'project': project['id'], 'revision': config['revision'], 'nonce': secrets.token_urlsafe(8), 'items': items})
            self.choices(user, session)
        elif action == 'pmp_page':
            if session.get('view') not in {'pmp_projects', 'pmp_accounts'} or not session.get('pmp_choices'):
                raise ValueError('Откройте список Postmypost заново')
            session['pmp_page'] = int(payload['page'])
            self.choices(user, session)
        elif action in {'pmp_project', 'pmp_toggle', 'pmp_save'}:
            choice = session.get('pmp_choices', {})
            expected_view = 'pmp_projects' if action == 'pmp_project' else 'pmp_accounts'
            if (session.get('view') != expected_view or choice.get('project') != project['id']
                    or choice.get('revision') != config['revision'] or payload.get('n') != choice.get('nonce')):
                raise ValueError('Выбор Postmypost устарел. Откройте список заново')
            if action == 'pmp_save':
                selected = session.get('pmp_selected', [])
                if not selected:
                    raise ValueError('Выберите хотя бы одну соцсеть')
                accounts = [item for item in choice['items'] if item['id'] in selected]
                if len(accounts) != len(selected):
                    raise ValueError('Список соцсетей изменился, выберите заново')
                self.store.save_pmp_connection(project['id'], user, self.admins,
                    {'remote_project': choice['remote_project'], 'remote_name': choice['remote_name'], 'accounts': accounts}, config['revision'])
                self.show(user, project)
            else:
                index = payload.get('i')
                if type(index) is not int or not 0 <= index < len(choice['items']):
                    raise ValueError('Неизвестная запись Postmypost')
                item = choice['items'][index]
                if action == 'pmp_toggle':
                    selected = session.get('pmp_selected', [])
                    session['pmp_selected'] = [i for i in selected if i != item['id']] if item['id'] in selected else [*selected, item['id']]
                else:
                    accounts = self.fetch(config, 'accounts', item['id'])
                    channels = {row['id']: str(row.get('code') or row.get('name') or row['id']) for row in self.fetch(config, 'channels')}
                    items = [{'id': row['id'], 'name': channels.get(row.get('chanel_id', row.get('channel_id')), 'Postmypost') + ' · ' + str(row.get('name') or row['id'])}
                             for row in accounts if row.get('connection_status') == 1 and type(row.get('id')) is int and row['id'] > 0]
                    session.update(view='pmp_accounts', pmp_page=0,
                        pmp_selected=[r['id'] for r in config.get('accounts', []) if r['id'] in {a['id'] for a in items}] if config.get('remote_project') == item['id'] else [],
                        pmp_choices={'project': project['id'], 'revision': config['revision'], 'remote_project': item['id'],
                                     'remote_name': item['name'], 'nonce': secrets.token_urlsafe(8), 'items': items})
                self.choices(user, session)
        else:
            return False
        return True
