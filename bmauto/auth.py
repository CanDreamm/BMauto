"""Run on your own computer to grant access to the target Google account."""
import argparse
import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

from .google import SCOPES


def save_credentials(credentials, output):
    if not credentials.refresh_token:
        raise ValueError("Google не выдал refresh token. Повторите авторизацию с подтверждением доступа")
    body = json.loads(credentials.to_json())
    body["type"] = "authorized_user"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(body, file)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("client_file", help="OAuth Desktop client JSON from Google Cloud")
    parser.add_argument("--output", default="token.json")
    parser.add_argument("--port", type=int, default=0, help="Fixed loopback port for an SSH tunnel")
    parser.add_argument("--no-browser", action="store_true", help="Print the authorization URL without opening a browser")
    args = parser.parse_args()
    if os.path.exists(args.output):
        raise SystemExit("Output already exists; choose a different path")
    flow = InstalledAppFlow.from_client_secrets_file(args.client_file, SCOPES)
    credentials = flow.run_local_server(host="127.0.0.1", port=args.port, open_browser=not args.no_browser,
                                        timeout_seconds=300, access_type="offline", prompt="consent")
    save_credentials(credentials, args.output)
    print("Google подключён. Файл авторизации сохранён; не отправляйте его в чат и не добавляйте в Git.")


if __name__ == "__main__":
    main()
