"""Run on your own computer to grant access to the target Google account."""
import argparse
import os

from google_auth_oauthlib.flow import InstalledAppFlow

from .google import SCOPES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("client_file", help="OAuth Desktop client JSON from Google Cloud")
    parser.add_argument("--output", default="token.json")
    args = parser.parse_args()
    if os.path.exists(args.output):
        raise SystemExit("Output already exists; choose a different path")
    flow = InstalledAppFlow.from_client_secrets_file(args.client_file, SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(credentials.to_json())


if __name__ == "__main__":
    main()
