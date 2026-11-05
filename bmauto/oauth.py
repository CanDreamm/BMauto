"""Per-project Google OAuth. Secrets stay on disk; VK receives only consent URLs."""
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from google_auth_oauthlib.flow import Flow

from .auth import save_credentials
from .google import Google, SCOPES


class GoogleConnections:
    def __init__(self, store, admins, client_file, redirect_uri, legacy_file="token.json"):
        self.store, self.admins = store, admins
        self.client_file, self.redirect_uri = client_file, redirect_uri
        self.legacy_file = Path(legacy_file)
        self.root = store.root / "google"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        self.db = sqlite3.connect(self.root / "oauth.sqlite3", timeout=30)
        (self.root / "oauth.sqlite3").chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS flows (state_hash TEXT PRIMARY KEY, project TEXT NOT NULL, actor INTEGER NOT NULL, expires REAL NOT NULL, verifier TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS latest (project TEXT PRIMARY KEY, state_hash TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Freeze legacy eligibility once. New projects never inherit the global account.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('legacy_projects',?)", (json.dumps(list(store.projects())),))
        self.legacy_projects = set(json.loads(self.db.execute("SELECT value FROM metadata WHERE key='legacy_projects'").fetchone()[0]))

    def path_for(self, project):
        connection = project.get("google_connection_id")
        if connection:
            try:
                if uuid.UUID(connection).hex != connection:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Некорректное подключение Google. Подключите аккаунт заново") from exc
            path = self.root / f"{connection}.json"
            if path.is_file():
                return path
            raise ValueError("Файл подключения Google отсутствует. Подключите аккаунт заново")
        if project["id"] in self.legacy_projects and self.legacy_file.is_file():
            return self.legacy_file
        raise ValueError("Google не подключён к этому проекту. Откройте «Настройки» → «Подключить Google»")

    def connected(self, project):
        try:
            self.path_for(project)
            return True
        except ValueError:
            return False

    def flow(self, state=None, verifier=None):
        parsed = urlsplit(self.redirect_uri or "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.path != "/oauth/google/callback" or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("На сервере нужно настроить GOOGLE_OAUTH_REDIRECT_URI: https://bot.itiscat.ru/oauth/google/callback")
        if not self.client_file or not Path(self.client_file).is_file():
            raise ValueError("Нужна разовая настройка Google OAuth на сервере: JSON клиента типа Web application")
        config = json.loads(Path(self.client_file).read_text())
        client = config.get("web", {})
        if client.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth" or client.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError("Используйте JSON Web application, скачанный из Google Cloud")
        return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=self.redirect_uri, state=state,
                                       code_verifier=verifier, autogenerate_code_verifier=verifier is None)

    def begin(self, project_id, actor):
        if not self.store.can_access(project_id, actor, self.admins):
            raise ValueError("Подключать Google могут только участники этого проекта")
        state = secrets.token_urlsafe(32)
        flow = self.flow(state=state)
        url, _ = flow.authorization_url(access_type="offline", prompt="consent select_account")
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        with self.db:
            self.db.execute("DELETE FROM flows WHERE project=? OR expires<?", (project_id, time.time()))
            self.db.execute("INSERT INTO flows VALUES (?,?,?,?,?)", (state_hash, project_id, actor, time.time() + 600, flow.code_verifier))
            self.db.execute("INSERT OR REPLACE INTO latest VALUES (?,?)", (project_id, state_hash))
        return url

    def finish(self, state, code=None, error=None):
        if not state or len(state) > 256:
            raise ValueError("Некорректная ссылка подключения")
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        # Consume once before exchanging the code; a replay cannot rebind a project.
        with self.db:
            row = self.db.execute("DELETE FROM flows WHERE state_hash=? RETURNING project,actor,expires,verifier", (state_hash,)).fetchone()
        if not row or row[2] < time.time():
            raise ValueError("Ссылка истекла или уже использована. Нажмите «Подключить Google» в боте заново")
        project_id, actor, _, verifier = row
        if not self.store.can_access(project_id, actor, self.admins):
            raise ValueError("У инициатора больше нет прав на подключение этого проекта")
        if error or not code:
            raise ValueError("Подключение отменено. Вернитесь в бот, чтобы попробовать снова")
        flow = self.flow(state=state, verifier=verifier)
        flow.fetch_token(code=code, timeout=30)
        granted = flow.credentials.granted_scopes
        if granted is not None and not set(SCOPES).issubset(set(granted)):
            raise ValueError("Нужно разрешить доступ и к Google Диску, и к Google Таблицам")
        connection = uuid.uuid4().hex
        path = self.root / f"{connection}.json"
        bound = False
        try:
            save_credentials(flow.credentials, path)
            self.db.execute("BEGIN IMMEDIATE")
            latest = self.db.execute("SELECT state_hash FROM latest WHERE project=?", (project_id,)).fetchone()
            if not latest or latest[0] != state_hash:
                raise ValueError("Было запрошено более новое подключение. Используйте последнюю ссылку из бота")
            if not self.store.can_access(project_id, actor, self.admins):
                raise ValueError("Доступ к проекту изменился. Подключение отменено")
            project = self.store.projects()[project_id]
            updated = self.store.save_project({**project, "google_connection_id": connection, "google_connected_by": actor,
                                               "draft_scope": uuid.uuid4().hex}, project.get("revision", 1))
            bound = True
            self.db.execute("DELETE FROM latest WHERE project=?", (project_id,))
            self.db.commit()
            return updated["name"]
        except Exception:
            self.db.rollback()
            if not bound:
                path.unlink(missing_ok=True)
            raise


class ProjectGoogle:
    def __init__(self, connections):
        self.connections = connections
        self.clients = {}

    def for_project(self, project):
        path = str(self.connections.path_for(project))
        if path not in self.clients:
            self.clients[path] = Google(path)
        return self.clients[path]


class GoogleMenus:
    def __init__(self, connections, store, vk, admins):
        self.connections, self.store, self.vk, self.admins = connections, store, vk, admins

    def handle(self, user, action, session):
        if action != "google_connect":
            return False
        if not self.store.can_access(session.get("project"), user, self.admins):
            raise ValueError("Подключать Google могут только участники этого проекта")
        if self.connections is None:
            raise ValueError("Google OAuth ещё не настроен на сервере")
        url = self.connections.begin(session["project"], user)
        session.pop("wizard", None)
        session["mode"], session["view"] = None, "google_connect"
        self.store.save_session(user, session)
        project = self.store.projects()[session["project"]]
        self.vk.send(user, f"Подключение Google для проекта «{project['name']}». Войдите в свой аккаунт и разрешите доступ к Диску и Таблицам. Этот аккаунт будет использоваться всеми участниками проекта и заменит прежнее подключение. Ссылка действует 10 минут; не пересылайте её. После подключения вернитесь к датам проекта.",
                     [("Войти в Google", {"open_link": url}), ("К настройкам", {"a": "settings"}), ("К датам проекта", {"a": "project_home"})])
        return True
