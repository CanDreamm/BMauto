"""One personal Postmypost token; projects choose social accounts directly."""
import secrets

from .vk import back_footer

SCREENS = {'pmp_personal', 'pmp', 'pmp_token', 'pmp_accounts'}


def account_key(item):
    return f"{item['remote_project']}:{item['id']}"


class PostmypostMenus:
    def __init__(self, store, vk, admins, scheduler):
        self.store, self.vk, self.admins, self.scheduler = store, vk, admins, scheduler

    def config(self, project):
        return self.store.pmp_connection(project['id']) or {'revision': 0}

    def require(self, user, session):
        ident = session.get('project')
        if not self.store.can_manage(ident, user, self.admins):
            raise ValueError('Нет доступа к соцсетям этого проекта')
        return self.store.projects()[ident]

    def show_personal(self, user):
        credential = self.store.pmp_credential(user)
        session = self.store.session(user)
        session.update(view='pmp_personal', mode=None)
        self.store.save_session(user, session)
        self.vk.send(user, 'Мой Postmypost\n\n' + ('Ваш токен подключён.' if credential else 'Ваш токен не подключён.')
            + '\nСохраните токен один раз. В проектах выбирайте доступные через него соцсети. Другие участники не видят ваш токен.', [
                ('Заменить токен' if credential else 'Подключить Postmypost', {'a': 'pmp_token'}),
                back_footer({'a': 'projects'})])

    def show(self, user, project):
        config = self.config(project)
        session = self.store.session(user)
        session.update(view='pmp', mode=None)
        session.pop('pmp_choices', None)
        self.store.save_session(user, session)
        names = '\n'.join('• ' + item['name'] for item in config.get('accounts', [])) or 'Соцсети ещё не подключены'
        buttons = [('Подключить соцсети', {'a': 'pmp_accounts'})] if self.store.pmp_credential(user) else [('Мой Postmypost', {'a': 'pmp_personal', '_color': 'primary'})]
        self.vk.send(user, f"Соцсети проекта «{project['name']}»\n\n{names}"
            + ('' if self.store.pmp_credential(user) else '\n\nДля добавления соцсетей один раз подключите свой токен в «Мой Postmypost».'),
            buttons + [back_footer({'a': 'settings'})])

    def choice_valid(self, user, session):
        choice = session.get('pmp_choices', {})
        config = self.config(self.require(user, session))
        personal = self.store.pmp_credential(user) or {}
        return (choice.get('project') == session.get('project') and choice.get('revision') == config['revision']
                and choice.get('credential_revision') == personal.get('revision'))

    def render(self, user, session):
        if session.get('view') in {'pmp_personal', 'pmp_token'}:
            if not self.store.can_enter(user, self.admins):
                raise ValueError('Нет доступа к боту')
            if session.get('view') == 'pmp_token':
                self.vk.send(user, 'Отправьте личный API-токен Postmypost одним сообщением. Он сохраняется один раз на вашем пользователе и применяется к соцсетям, которые вы подключаете в проектах. Токен не показывается другим людям. Замена не удаляет выбранные соцсети; ранее открытые задания нужно открыть заново.', [back_footer({'a': 'pmp_personal'})])
            else:
                self.show_personal(user)
            return
        project = self.require(user, session)
        if session.get('view') == 'pmp_accounts' and self.choice_valid(user, session):
            self.choices(user, session)
        else:
            self.show(user, project)

    def choices(self, user, session):
        if not self.choice_valid(user, session):
            raise ValueError('Настройки или личный токен изменились. Откройте список заново')
        choice = session['pmp_choices']
        page = max(0, min(session.get('pmp_page', 0), max(0, (len(choice['items']) - 1) // 6)))
        session['pmp_page'] = page
        selected = session.get('pmp_selected', [])
        buttons = []
        for index in range(page * 6, min(len(choice['items']), (page + 1) * 6)):
            item = choice['items'][index]
            label = ('☑ ' if account_key(item) in selected else '☐ ') + item['name']
            buttons.append((label, {'a': 'pmp_toggle', 'i': index, 'n': choice['nonce']}))
        pages = []
        if page:
            pages.append(('Назад', {'a': 'pmp_page', 'page': page - 1}))
        if (page + 1) * 6 < len(choice['items']):
            pages.append(('Далее', {'a': 'pmp_page', 'page': page + 1}))
        if pages:
            buttons.append(pages)
        buttons.append(back_footer({'a': 'pmp_home'}, ('Сохранить', {'a': 'pmp_save', 'n': choice['nonce'], '_color': 'positive'})))
        self.store.save_session(user, session)
        title = f"Выберите соцсети. Выбрано: {len(selected)}"
        if not choice['items']:
            title += '\nДоступных соцсетей нет. Подключите их в кабинете Postmypost.'
        self.vk.send(user, title, buttons)

    def load_accounts(self, user, project, session):
        credential = self.store.pmp_credential(user)
        if not credential:
            self.show(user, project)
            return
        api = self.scheduler.client_factory(credential['token'])
        try:
            projects = api.projects()
            channels = {r['id']: str(r.get('code') or r.get('name') or r['id']) for r in api.channels()}
            items = []
            for remote in projects:
                if type(remote.get('id')) is not int or remote['id'] <= 0:
                    continue
                for row in api.accounts(remote['id']):
                    if row.get('connection_status') != 1 or type(row.get('id')) is not int or row['id'] <= 0:
                        continue
                    channel = channels.get(row.get('chanel_id', row.get('channel_id')), 'Postmypost')
                    items.append({'id': row['id'], 'remote_project': remote['id'],
                                  'name': channel + ' · ' + str(row.get('name') or row['id']) + ' · ' + str(remote.get('name') or remote['id'])})
        finally:
            api.close()
        config = self.config(project)
        selected = [account_key({**r, 'remote_project': r.get('remote_project', config.get('remote_project'))}) for r in config.get('accounts', [])] if config.get('credential_owner') == user else []
        session.update(view='pmp_accounts', mode=None, pmp_page=0,
            pmp_selected=[i for i in selected if i in {account_key(r) for r in items}],
            pmp_choices={'project': project['id'], 'revision': config['revision'], 'credential_revision': credential['revision'],
                         'nonce': secrets.token_urlsafe(8), 'items': items})
        self.choices(user, session)

    def handle(self, user, action, payload, text, session):
        token_input = session.get('mode') == 'pmp_token' and not action
        if not token_input and (not isinstance(action, str) or not action.startswith('pmp')):
            return False
        if action in {'pmp_personal', 'pmp_token'} or token_input:
            if not self.store.can_enter(user, self.admins):
                raise ValueError('Нет доступа к боту')
            for key in ('project', 'task', 'tasks', 'wizard', 'pmp_choices', 'member_input', 'member_remove', 'delete_confirmation'):
                session.pop(key, None)
            if action == 'pmp_personal':
                session['mode'] = None
                self.store.save_session(user, session)
                self.show_personal(user)
            elif action == 'pmp_token':
                session.update(mode='pmp_token', view='pmp_token', pmp_revision=(self.store.pmp_credential(user) or {}).get('revision', 0))
                self.store.save_session(user, session)
                self.render(user, session)
            else:
                if not text or len(text) > 8192 or any(c.isspace() for c in text):
                    raise ValueError('Отправьте API-токен Postmypost без пробелов')
                api = self.scheduler.client_factory(text)
                try:
                    api.projects()
                finally:
                    api.close()
                self.store.save_pmp_credential(user, text, self.admins, session['pmp_revision'])
                session['mode'] = None
                self.store.save_session(user, session)
                self.show_personal(user)
            return True
        project = self.require(user, session)
        config = self.config(project)
        if action in {'pmp', 'pmp_home', 'pmp_accounts', 'pmp_projects'}:
            for key in ('wizard', 'member_input', 'member_remove', 'delete_confirmation'):
                session.pop(key, None)
            session['mode'] = None
            self.store.save_session(user, session)
            if action == 'pmp_home' or (action == 'pmp' and config.get('accounts')):
                self.show(user, project)
            else:
                self.load_accounts(user, project, session)
        elif action in {'pmp_page', 'pmp_toggle', 'pmp_save'}:
            if session.get('view') != 'pmp_accounts' or not self.choice_valid(user, session):
                raise ValueError('Выбор соцсетей устарел. Откройте список заново')
            choice = session['pmp_choices']
            if action == 'pmp_page':
                session['pmp_page'] = int(payload['page'])
            elif payload.get('n') != choice['nonce']:
                raise ValueError('Выбор соцсетей устарел. Откройте список заново')
            elif action == 'pmp_save':
                selected = session.get('pmp_selected', [])
                if not selected:
                    raise ValueError('Выберите хотя бы одну соцсеть')
                accounts = [row for row in choice['items'] if account_key(row) in selected]
                if len(accounts) != len(selected):
                    raise ValueError('Список соцсетей изменился. Выберите заново')
                self.store.save_pmp_connection(project['id'], user, self.admins,
                    {'credential_owner': user, 'accounts': accounts, 'remote_project': None}, config['revision'], choice['credential_revision'])
                self.show(user, project)
                return True
            else:
                index = payload.get('i')
                if type(index) is not int or not 0 <= index < len(choice['items']):
                    raise ValueError('Неизвестная соцсеть')
                ident = account_key(choice['items'][index])
                selected = session.get('pmp_selected', [])
                session['pmp_selected'] = [i for i in selected if i != ident] if ident in selected else [*selected, ident]
            self.choices(user, session)
        else:
            raise ValueError('Откройте актуальный список соцсетей в настройках проекта')
        return True
