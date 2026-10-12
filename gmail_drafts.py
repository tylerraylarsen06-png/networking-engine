"""Gmail drafts, never sends.

Uses the Gmail API with the gmail.compose scope only, and only calls users.drafts.create. There is
no function in this project that sends mail: you open Gmail, read each draft, and send it yourself.

One-time setup (see README, "Gmail drafts"):
  1. Create an OAuth client of type "Desktop app" in Google Cloud and enable the Gmail API.
  2. Save its JSON as gmail_credentials.json next to this file (gitignored).
  3. python3 gmail_drafts.py --setup     # opens a browser for the consent click, writes gmail_token.json

Both files are gitignored and never printed.
"""
import argparse
import base64
import os
import sys
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
CREDENTIALS_PATH = Path(os.environ.get("GMAIL_CREDENTIALS_FILE") or HERE / "gmail_credentials.json")
TOKEN_PATH = Path(os.environ.get("GMAIL_TOKEN_FILE") or HERE / "gmail_token.json")


class GmailNotConfigured(RuntimeError):
    pass


def is_configured():
    return TOKEN_PATH.exists()


def _credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise GmailNotConfigured("Gmail libraries missing: pip3 install -r requirements.txt") from e
    if not TOKEN_PATH.exists():
        raise GmailNotConfigured("No Gmail token. Run: python3 gmail_drafts.py --setup")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            os.chmod(TOKEN_PATH, 0o600)
        else:
            raise GmailNotConfigured("Gmail token is invalid. Run: python3 gmail_drafts.py --setup")
    return creds


def build_service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


def raw_message(to, subject, body):
    msg = EmailMessage()
    if to:
        msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def create_draft(service, to, subject, body):
    """Create one Gmail draft. Returns the draft id. A missing address leaves To blank for you to fill."""
    result = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw_message(to, subject, body)}}).execute()
    return result.get("id")


def setup():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the Gmail libraries first: pip3 install -r requirements.txt", file=sys.stderr)
        return 2
    if not CREDENTIALS_PATH.exists():
        print(f"Save your Desktop OAuth client JSON as {CREDENTIALS_PATH.name} first (see README).", file=sys.stderr)
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)
    print(f"Gmail drafts enabled. Token saved to {TOKEN_PATH.name} (gitignored).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gmail drafts setup (compose scope, drafts only).")
    ap.add_argument("--setup", action="store_true", help="run the one-time OAuth consent")
    a = ap.parse_args(argv)
    if a.setup:
        return setup()
    print("configured" if is_configured() else "not configured: python3 gmail_drafts.py --setup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
