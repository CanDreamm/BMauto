import json
import stat

import google.auth
import pytest
from google.oauth2.credentials import Credentials

from bmauto.auth import save_credentials
from bmauto.google import SCOPES


def test_saved_oauth_file_can_be_loaded_by_bot(tmp_path):
    credentials = Credentials(token="fake-access-token", refresh_token="fake-refresh-token",
                              token_uri="https://oauth2.googleapis.com/token", client_id="test-client",
                              client_secret="fake-client-secret", scopes=SCOPES)
    path = tmp_path / "token.json"
    save_credentials(credentials, path)
    loaded, _ = google.auth.load_credentials_from_file(str(path), scopes=SCOPES)
    assert loaded.refresh_token == credentials.refresh_token
    assert json.loads(path.read_text())["type"] == "authorized_user"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        save_credentials(credentials, path)


def test_incomplete_authorization_does_not_create_file(tmp_path):
    path = tmp_path / "token.json"
    with pytest.raises(ValueError, match="refresh token"):
        save_credentials(Credentials(token="fake-access-token"), path)
    assert not path.exists()


def test_ssh_flow_binds_loopback_and_saves_usable_file(tmp_path, monkeypatch):
    from bmauto import auth
    credentials = Credentials(token="fake-access-token", refresh_token="fake-refresh-token",
                              token_uri="https://oauth2.googleapis.com/token", client_id="test-client",
                              client_secret="fake-client-secret", scopes=SCOPES)
    calls = []
    class Flow:
        def run_local_server(self, **kwargs):
            calls.append(kwargs)
            return credentials
    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", lambda path, scopes: Flow())
    path = tmp_path / "token.json"
    monkeypatch.setattr("sys.argv", ["auth", "credentials-client.json", "--port", "8765", "--no-browser", "--output", str(path)])
    auth.main()
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8765
    assert calls[0]["open_browser"] is False
    assert google.auth.load_credentials_from_file(str(path), scopes=SCOPES)[0].refresh_token
