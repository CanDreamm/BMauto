"""Postmypost v4.1, based on CanDreamm/The-Loremaster and the official SDK."""
import hashlib
import ipaddress
import json
import re
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

BASE = 'https://api.postmypost.io/v4.1'


class RequestBudget:
    def __init__(self):
        self.calls, self.lock = defaultdict(deque), threading.Lock()

    def acquire(self, actor, method, path):
        category, limit = ('upload', 10) if path.startswith('/upload/') else (
            ('write', 10) if path.startswith('/publications') and method in {'POST', 'PUT'} else ('read', 30))
        while True:
            with self.lock:
                now = time.monotonic()
                history = self.calls[actor, category]
                while history and now - history[0] >= 60:
                    history.popleft()
                if len(history) < limit:
                    history.append(now)
                    return
                delay = max(.05, 60 - (now - history[0]))
            time.sleep(min(delay, 10))


BUDGET = RequestBudget()


class PostmypostError(ValueError):
    pass


class UnknownPublication(PostmypostError):
    pass


def storage_url(value):
    if not isinstance(value, str) or len(value) > 16384 or '\\' in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return False
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443) or url.fragment:
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            host = url.hostname.rstrip('.').lower()
            return '.' in host and not host.endswith(('.localhost', '.local', '.internal')) and bool(re.fullmatch(r'[a-z0-9.-]+', host)) and not re.fullmatch(r'[0-9.]+', host)
    except ValueError:
        return False


class Postmypost:
    def __init__(self, token, *, http=None, storage_factory=httpx.Client, budget=None):
        self.http = http or httpx.Client(timeout=60, follow_redirects=False)
        self.token, self.storage_factory = token, storage_factory
        self.budget = BUDGET if budget is None else budget
        self.actor = hashlib.sha256(token.encode()).hexdigest()

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        if self.budget:
            self.budget.acquire(self.actor, method, path)
        writing = method in {'POST', 'PUT'} and path.startswith('/publications')
        try:
            response = self.http.request(method, BASE + path,
                                         headers={'Authorization': 'Bearer ' + self.token}, **kwargs)
            if writing and (response.status_code >= 500 or response.status_code == 408):
                raise UnknownPublication('Postmypost: результат отправки неизвестен. Проверьте кабинет; новая публикация автоматически не создаётся.')
            if not 200 <= response.status_code < 300:
                hint = {401: 'Токен недействителен.', 403: 'Проверьте тариф API и права.',
                        404: 'Запись не найдена.', 422: 'Проверьте параметры публикации и требования соцсетей.',
                        429: 'Превышен лимит запросов, повторите позже.'}.get(response.status_code, 'Запрос отклонён.')
                raise PostmypostError(f'Postmypost: HTTP {response.status_code}. {hint}')
            result = response.json()
            if not isinstance(result, (dict, list)):
                raise ValueError
            return result
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, PostmypostError):
                raise
            if writing:
                raise UnknownPublication('Postmypost: результат отправки неизвестен. Проверьте кабинет; новая публикация автоматически не создаётся.') from None
            raise PostmypostError('Postmypost: не удалось получить ответ сервиса. Повторите позже.') from None

    def list_all(self, path, **params):
        items = []
        for page in range(1, 201):
            body = self.request('GET', path, params={**params, 'page': page, 'per_page': 50})
            batch = body if isinstance(body, list) else body.get('data')
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise PostmypostError('Postmypost вернул некорректный список.')
            items.extend(batch)
            pages = body.get('pages', {}) if isinstance(body, dict) else {}
            total = pages.get('total_pages') if isinstance(pages, dict) else None
            if not batch or (type(total) is int and page >= total) or (total is None and len(batch) < 50):
                return items
        raise PostmypostError('Postmypost: список слишком большой, загрузка не завершена.')

    def projects(self):
        return self.list_all('/projects')

    def accounts(self, project):
        return self.list_all('/accounts', project_id=int(project))

    def channels(self):
        return self.list_all('/channels')

    def upload(self, project, path, mime):
        image = Path(path)
        if not image.is_file():
            raise PostmypostError('Фотография черновика не найдена. Публикация не отправлена.')
        init = self.request('POST', '/upload/init', json={'project_id': int(project), 'name': image.name, 'size': image.stat().st_size})
        ident, action, fields = init.get('id'), init.get('action'), init.get('fields')
        if type(ident) is not int or ident <= 0 or not storage_url(action):
            raise PostmypostError('Postmypost: неверные параметры хранилища. Публикация не отправлена.')
        if not isinstance(fields, list) or any(not isinstance(f, dict) or not isinstance(f.get('key'), str)
                or not f['key'] or any(ord(c) < 32 for c in f['key']) or not isinstance(f.get('value'), str) for f in fields):
            raise PostmypostError('Postmypost: неверные поля загрузки. Публикация не отправлена.')
        # A separate client prevents leaking API Authorization or API cookies to storage.
        try:
            with self.storage_factory(timeout=120, follow_redirects=False) as storage, image.open('rb') as content:
                response = storage.post(action, files=[*( (f['key'], (None, f['value'])) for f in fields),
                                                      ('file', (image.name, content, mime))])
                if not 200 <= response.status_code < 300:
                    raise PostmypostError('Postmypost: фотография не загружена. Публикация не отправлена.')
        except (OSError, httpx.HTTPError):
            raise PostmypostError('Postmypost: загрузка фотографии прервана. Публикация не отправлена.') from None
        self.request('POST', '/upload/complete', params={'id': ident})
        for _ in range(15):
            result = self.request('GET', '/upload/status', params={'id': ident})
            file_id = result.get('file_id')
            if result.get('status') == 1 and type(file_id) is int and file_id > 0:
                return file_id
            if result.get('status') == 2:
                break
            time.sleep(2)
        raise PostmypostError('Postmypost: фото ещё не готово. Публикация не отправлена, повторите позже.')

    def create(self, payload):
        return self.request('POST', '/publications', json=payload)

    def update(self, ident, payload):
        return self.request('PUT', '/publications/' + str(int(ident)), json={k: v for k, v in payload.items() if k != 'project_id'})

    def status(self, ident):
        return self.request('GET', '/publications/' + str(int(ident)))


def publication_at(task, now=None):
    clock = str(task.get('time') or task.get('fields', {}).get('Время') or '').strip()
    if not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', clock):
        raise PostmypostError('Укажите в таблице время публикации в формате ЧЧ:ММ и откройте задание заново.')
    zone = ZoneInfo('Europe/Moscow')
    pieces = clock.split(':')
    normalized = f'{int(pieces[0]):02}:' + ':'.join(pieces[1:])
    moment = datetime.fromisoformat(task['day'] + 'T' + normalized).replace(tzinfo=zone)
    if moment <= (now or datetime.now(zone)):
        raise PostmypostError('Дата и время публикации уже прошли. Измените их в таблице и откройте задание заново.')
    return moment.astimezone(timezone.utc).isoformat()


def confirmed_publication(body, ident, payload):
    """Require a pending entry with the requested date, recipients, text and files."""
    try:
        if type(body.get('id')) is not int or body['id'] != ident or body.get('publication_status') != 5:
            return False
        if datetime.fromisoformat(body['post_at'].replace('Z', '+00:00')) != datetime.fromisoformat(payload['post_at']):
            return False
        if sorted(body['account_ids']) != sorted(payload['account_ids']):
            return False
        details = {row['account_id']: row for row in body['details']}
        for wanted in payload['details']:
            got = details[wanted['account_id']]
            if (got.get('content') or '') != wanted['content'] or got.get('publication_type') != 1:
                return False
            if [f['id'] for f in (got.get('files') or [])] != wanted['file_ids']:
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class PostmypostScheduler:
    def __init__(self, store, client_factory=Postmypost, admins=()):
        self.store, self.client_factory, self.admins = store, client_factory, admins

    def settings_stamp(self, project):
        config = self.store.pmp_connection(project['id']) or {}
        owner = config.get('credential_owner')
        credential = self.store.pmp_credential(owner) or {}
        return [config.get('revision', 0), owner, credential.get('revision', 0)]

    def signature(self, project, task):
        return [task['day'], task.get('time', ''), *self.settings_stamp(project)]

    def schedule(self, project, task, key, draft, validate):
        config = self.store.pmp_connection(project['id'])
        if not config or not config.get('accounts'):
            raise PostmypostError('Выберите соцсети в настройках проекта. Токен подключается один раз в «Мой Postmypost».')
        owner = config.get('credential_owner')
        credential = self.store.pmp_credential(owner)
        if not credential or not self.store.can_access(project['id'], owner, self.admins):
            raise PostmypostError('Личное подключение добавившего соцсети пользователя недоступно. Подключите Postmypost заново.')
        def protected_validate():
            if (self.settings_stamp(project) != [config['revision'], owner, credential['revision']]
                    or not self.store.can_access(project['id'], owner, self.admins)):
                raise PostmypostError('Личное подключение или соцсети изменились. Откройте задание заново.')
            validate()
        protected_validate()
        groups = {}
        for item in config['accounts']:
            group = item.get('remote_project', config.get('remote_project'))
            if type(group) is not int or group <= 0:
                raise PostmypostError('Выберите соцсети проекта заново.')
            groups.setdefault(group, []).append(item)
        previous = self.store.pmp_job(key)
        for job_key, body in self.store.db.execute("SELECT key,body FROM pmp_jobs WHERE key=? OR key LIKE ?", (key, key + ':pmp:%')):
            job = json.loads(body)
            if job.get('remote_project') not in groups and (job.get('id') or job.get('state') == 'sending'):
                raise PostmypostError('Для удалённой из проекта соцсети уже есть очередь Postmypost. Сначала проверьте прежнюю запись в кабинете.')
        ids = []
        for number, (group, accounts) in enumerate(sorted(groups.items())):
            group_key = key if ((previous and previous.get('remote_project') == group) or (previous is None and number == 0)) else key + ':pmp:' + str(group)
            settings = {**config, 'remote_project': group, 'accounts': accounts, 'token': credential['token'],
                        'credential_revision': credential['revision']}
            ids.append(self._schedule_group(project, task, group_key, draft, protected_validate, settings))
        return ids[0] if len(ids) == 1 else ids

    def _schedule_group(self, project, task, key, draft, validate, config):
        if draft.get('dirty') or not draft.get('published') or not (draft['text'].strip() or draft['photos']):
            raise PostmypostError('Сначала отправьте актуальный текст и фотографии в таблицу.')
        at = publication_at(task)
        accounts = sorted(int(item['id']) for item in config['accounts'])
        photos = []
        for photo in draft['photos']:
            path = (self.store.root / photo['path']).resolve()
            if not path.is_relative_to(self.store.root.resolve()) or not path.is_file():
                raise PostmypostError('Фотография черновика не найдена.')
            photos.append((path, photo.get('mime', 'image/jpeg'), hashlib.sha256(path.read_bytes()).hexdigest()))
        fingerprint = hashlib.sha256(json.dumps([config['remote_project'], accounts, at, draft['text'], [p[2] for p in photos]], ensure_ascii=False).encode()).hexdigest()
        before = self.store.pmp_job(key)
        if before and before.get('remote_project') != config['remote_project']:
            raise PostmypostError('Для черновика уже есть запись в другом проекте Postmypost. Проверьте её в кабинете перед сменой проекта.')
        if before and before.get('state') == 'sending' and not before.get('id'):
            raise UnknownPublication('Предыдущая отправка имеет неизвестный результат. Проверьте кабинет Postmypost; повторное создание заблокировано во избежание дубля.')
        api = self.client_factory(config['token'])
        try:
            validate()
            if before and before.get('id') and before.get('fingerprint') == fingerprint:
                if confirmed_publication(api.status(before['id']), before['id'], before['payload']):
                    validate()
                    before['state'] = 'scheduled'
                    self.store.save_pmp_job(key, project['id'], before)
                    return before['id']
            remote = api.status(before['id']) if before and before.get('id') else None
            if remote is not None and remote.get('publication_status') not in {4, 5}:
                raise PostmypostError('Запись Postmypost уже отправляется, опубликована или имеет ошибку. Проверьте её в кабинете.')
            available = {int(a['id']) for a in api.accounts(config['remote_project']) if a.get('connection_status') == 1}
            if not set(accounts).issubset(available):
                raise PostmypostError('Одна из выбранных соцсетей отключена в Postmypost. Проверьте настройки проекта.')
            files = [api.upload(config['remote_project'], path, mime) for path, mime, _ in photos]
            payload = {'project_id': config['remote_project'], 'account_ids': accounts, 'post_at': at, 'publication_status': 5,
                       'details': [{'account_id': ident, 'publication_type': 1, 'content': draft['text'], 'file_ids': files} for ident in accounts]}
            validate()
            publication_at(task)  # Uploading may have crossed the requested time.
            job = {'remote_project': config['remote_project'], 'fingerprint': fingerprint,
                   'id': before.get('id') if before else None, 'state': 'sending', 'payload': payload}
            self.store.claim_pmp_job(key, project['id'], before, job, config['revision'], config['credential_revision'])
            try:
                result = api.update(job['id'], payload) if job['id'] else api.create(payload)
                remote_id = result.get('id') if isinstance(result, dict) else None
                if type(remote_id) is not int or remote_id <= 0 or (job['id'] and remote_id != job['id']):
                    raise UnknownPublication('Postmypost не подтвердил ID публикации. Проверьте кабинет; повторное создание заблокировано.')
                job.update(id=remote_id, state='checking')
                self.store.save_pmp_job(key, project['id'], job)
            except UnknownPublication:
                raise
            except PostmypostError:
                job['state'] = 'rejected'
                self.store.save_pmp_job(key, project['id'], job)
                raise
            except Exception:
                raise UnknownPublication('Результат отправки неизвестен. Проверьте кабинет Postmypost; повторное создание заблокировано.') from None
            if not confirmed_publication(api.status(job['id']), job['id'], payload):
                raise PostmypostError('Postmypost пока не подтвердил отложенную публикацию с нужными данными. Статус таблицы не изменён. Повторное нажатие проверит существующую запись.')
            validate()
            job['state'] = 'scheduled'
            self.store.save_pmp_job(key, project['id'], job)
            return job['id']
        finally:
            api.close()
