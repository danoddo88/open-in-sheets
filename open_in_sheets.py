"""Uploads a spreadsheet to Google Drive converted to Google Sheets and opens it.

    python open_in_sheets.py [--account EMAIL] file.csv [other.xlsx ...]
    python open_in_sheets.py --add-account     # authorize another Google account
    python open_in_sheets.py --accounts        # list them, last used first
    python open_in_sheets.py --remove EMAIL
    python open_in_sheets.py --account EMAIL --exists | --open-uploaded |
        --overwrite | --name NAME  file          # if it was already uploaded

"Open in Sheets.app" (see build_mac_app.py) runs this when a .csv, .xlsx or
.parquet file is double-clicked; with several accounts, the app asks which.
Google converts CSV and XLSX; it does not accept parquet, so parquet is
turned into CSV here first (that needs pyarrow, which installer.py installs).
OAuth and the Drive API go through urllib.

Credentials live in ~/.config/open-in-sheets/ (outside any synced folder):
  client.json          the "Desktop app" OAuth client downloaded from Google Cloud
  accounts/<email>.json  one token per account; it gives access to that Drive
  last_account         the last one used, offered first
The requested scope is drive.file: it only reaches the files this tool
creates, not the rest of the Drive.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.path.expanduser("~/.config/open-in-sheets")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
SHEETS_TYPE = "application/vnd.google-apps.spreadsheet"
FOLDER_TYPE = "application/vnd.google-apps.folder"
# New spreadsheets go into this My Drive folder (created the first time)
FOLDER_NAME = "Open in Sheets"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# drive.file: only the files the app creates. openid + email: know which
# account each token belongs to, so there can be several
SCOPE = "openid email https://www.googleapis.com/auth/drive.file"
# Google's limit for converted spreadsheets: 20 million cells or 100 MB
# (support.google.com/drive/answer/37603, checked 2026-10-23)
MAX_BYTES = 100 * 1024 * 1024
# Extension -> upload type; Drive converts these to Sheets
DRIVE_TYPES = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
CHROME_STATE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Local State")
LOGIN_WAIT = 300  # seconds waiting for the browser to come back


class Error(Exception):
    """A problem to show the user, without a traceback."""


def chrome_profile_for(email, chrome_state=CHROME_STATE):
    """The folder of the Chrome profile signed in with that account
    (e.g. "Profile 2"), or None."""
    try:
        with open(chrome_state, encoding="utf-8") as f:
            profiles = json.load(f)["profile"]["info_cache"]
    except (OSError, ValueError, KeyError):
        return None
    for folder, data in profiles.items():
        if (data.get("user_name") or "").lower() == email.lower():
            return folder
    return None


def chrome_profiles(chrome_state=CHROME_STATE):
    """[(folder, label)] of the Chrome profiles, those with an account first.
    The label is what the user sees when choosing."""
    try:
        with open(chrome_state, encoding="utf-8") as f:
            profiles = json.load(f)["profile"]["info_cache"]
    except (OSError, ValueError, KeyError):
        return []
    with_account, without_account = [], []
    for folder, data in sorted(profiles.items()):
        name = data.get("name") or folder
        account = data.get("user_name") or ""
        if account:
            with_account.append((folder, f"{name} ({account})"))
        else:
            without_account.append((folder, f"{name} (no Google account)"))
    return with_account + without_account


def open_command(url, email=None, chrome_state=CHROME_STATE, profile=None):
    """With several accounts, Chrome usually has one profile per account: if
    `open` uses the last active window, the spreadsheet lands in the wrong
    profile (2026-10-23, see NOTES). If a profile was chosen, or one has that
    account, it opens there.
    """
    if not profile and email:
        profile = chrome_profile_for(email, chrome_state)
    if profile:
        return ["open", "-na", "Google Chrome", "--args",
                f"--profile-directory={profile}", url]
    # `open` rather than webbrowser: webbrowser uses AppleScript and, launched
    # from the app, macOS could ask for automation permission
    return ["open", url]


def open_in_browser(url, email=None, profile=None):
    subprocess.run(open_command(url, email, profile=profile), check=False)


def _path(name):
    return os.path.join(CONFIG_DIR, name)


def _request(req):
    """(status, headers, body) without raising on HTTP errors."""
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except urllib.error.URLError as e:
        raise Error(f"Cannot reach Google ({e.reason}).") from None


def _post_form(url, fields):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    status, _, body = _request(req)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {"error": body.decode(errors="replace")}
    return status, data


def read_client():
    path = _path("client.json")
    if not os.path.exists(path):
        raise Error(f"{path} is missing.\nDownload the Desktop app OAuth client "
                    "from Google Cloud and save it under that name (see README).")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "installed" not in data:
        raise Error(f"{path} is not a Desktop app OAuth client.")
    return data["installed"]["client_id"], data["installed"]["client_secret"]


# --------------------------------------------------------------- accounts
# One token per Google account: accounts/<email>.json. "last_account" keeps
# the last one used, which goes first in the list to choose from.

def _account_path(email):
    if not email or "/" in email or email.startswith("."):
        raise Error(f"Invalid account: {email!r}.")
    return os.path.join(CONFIG_DIR, "accounts", email + ".json")


def accounts():
    """Emails of the added accounts, last used first."""
    folder = os.path.join(CONFIG_DIR, "accounts")
    if not os.path.isdir(folder):
        return []
    everyone = sorted(n[:-5] for n in os.listdir(folder) if n.endswith(".json"))
    last = last_account()
    return [last] + [a for a in everyone if a != last] if last in everyone else everyone


def last_account():
    try:
        with open(_path("last_account"), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def mark_last(email):
    with open(_path("last_account"), "w", encoding="utf-8") as f:
        f.write(email)


def remove_account(email):
    """Deletes the account's token and tells Google it is no longer used."""
    path = _account_path(email)
    if not os.path.exists(path):
        raise Error(f"There is no account {email}.")
    with open(path, encoding="utf-8") as f:
        token = json.load(f)
    try:
        _post_form(REVOKE_URL, {"token": token.get("refresh_token", "")})
    except Error:
        pass  # offline: it is still deleted here
    os.remove(path)
    if last_account() == email:
        os.remove(_path("last_account"))


def email_from_token(token):
    """The email in the id_token Google returns along with the access token.

    The signature is not checked: the id_token comes straight from Google
    over HTTPS.
    """
    try:
        payload = token["id_token"].split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data["email"]
    except (KeyError, IndexError, ValueError):
        raise Error("Google did not return the account's email.") from None


def save_token(token, email):
    token = dict(token)
    token["expires_at"] = time.time() + int(token.get("expires_in", 0)) - 60
    path = _account_path(email)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)
    os.chmod(path, 0o600)  # os.open only applies the mode when it creates the file
    return token


def pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(client_id, redirect, challenge, state, hint=None):
    fields = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # offline + consent: so Google hands out the refresh_token.
        # select_account: with several accounts, always ask which one
        "access_type": "offline",
        "prompt": "select_account consent",
    }
    if hint:
        fields["login_hint"] = hint
    return AUTH_URL + "?" + urllib.parse.urlencode(fields)


def login(browser=open_in_browser, hint=None, profile=None):
    """Adds (or re-authorizes) an account in the browser. Returns its email.

    Desktop app loopback flow, with PKCE. `profile`: folder of the Chrome
    profile where Google's page opens; without one, the hinted account's
    profile or else the default browser. Opened in a profile where the
    account is not signed in, it cannot be chosen and this waits in vain
    (2026-10-23, see NOTES).
    """
    client_id, secret = read_client()
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)  # favicon and the like
                self.end_headers()
                return
            received.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<p>Done, you can close this tab.</p>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    try:
        url = authorization_url(client_id, redirect, pkce_challenge(verifier),
                                state, hint)
        if profile:
            browser(url, hint, profile)
        else:
            browser(url, hint)
        deadline = time.monotonic() + LOGIN_WAIT
        while not received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not received:
        raise Error("Google's authorization never arrived (waited 5 minutes).")
    if "error" in received:
        raise Error(f"Google did not grant authorization: {received['error']}.")
    if received.get("state") != state:
        raise Error("Google's answer does not match the request. Please try again.")

    status, token = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": received["code"],
        "client_id": client_id,
        "client_secret": secret,
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if status != 200:
        raise Error(f"Google rejected the code: {token.get('error_description') or token.get('error')}.")
    email = email_from_token(token)
    save_token(token, email)
    mark_last(email)
    return email


def access_token(email, browser=open_in_browser):
    """A valid access token for the account: the saved one or a refreshed one."""
    path = _account_path(email)
    if not os.path.exists(path):
        raise Error(f"There is no account {email}. To add it, open the app by itself.")
    with open(path, encoding="utf-8") as f:
        token = json.load(f)
    if token.get("expires_at", 0) > time.time():
        return token["access_token"]

    client_id, secret = read_client()
    status, new = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": token.get("refresh_token", ""),
        "client_id": client_id,
        "client_secret": secret,
    })
    if status != 200:
        if new.get("error") == "invalid_grant":
            # Access revoked or expired: authorize that account again
            other = login(browser, hint=email)
            if other != email:
                raise Error(f"{other} was authorized instead of {email}. Try "
                            f"again and choose {email}.")
            return access_token(email, browser)
        raise Error(f"Could not refresh access: {new.get('error_description') or new.get('error')}.")
    new.setdefault("refresh_token", token.get("refresh_token"))
    return save_token(new, email)["access_token"]


def link_for_account(link, email):
    """The spreadsheet link forcing the account, in case the browser has
    another Google account signed in as the main one."""
    separator = "&" if "?" in link else "?"
    return f"{link}{separator}authuser={urllib.parse.quote(email)}"


def _api_message(status, body):
    try:
        detail = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = body.decode(errors="replace")[:300]
    return f"Google Drive answered {status}: {detail}"


def parquet_to_csv(path, folder):
    """Turns the parquet file into a CSV with the same name inside `folder`."""
    try:
        import pyarrow.csv
        import pyarrow.parquet
    except ImportError:
        raise Error("Opening .parquet needs pyarrow: run Install.app again.") from None
    name = os.path.splitext(os.path.basename(path))[0]
    target = os.path.join(folder, name + ".csv")
    try:
        pyarrow.csv.write_csv(pyarrow.parquet.read_table(path), target)
    except Exception as e:  # nested columns, broken file, etc.
        raise Error(f"Could not turn {os.path.basename(path)} into CSV: {e}") from None
    if os.path.getsize(target) > MAX_BYTES:
        raise Error(f"{os.path.basename(path)} as CSV is "
                    f"{os.path.getsize(target) / 2**20:.0f} MB and Google Sheets "
                    "converts up to 100 MB.")
    return target


def sheet_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def _drive_quote(text):
    """Escapes text to put it between single quotes in a Drive query."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _list(query, token, mime_type=SHEETS_TYPE):
    req = urllib.request.Request(
        FILES_URL + "?" + urllib.parse.urlencode({
            "q": query + f" and mimeType = '{mime_type}' and trashed = false",
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,webViewLink)",
            "pageSize": 1000,
        }), headers={"Authorization": f"Bearer {token}"})
    status, _, body = _request(req)
    if status != 200:
        raise Error(_api_message(status, body))
    return json.loads(body)["files"]


def find_uploaded(name, token):
    """The spreadsheet with that name this app already uploaded (the most
    recent one), or None.

    With drive.file, Drive only shows the app what it created: a spreadsheet
    made by hand with the same name does not count.
    """
    sheets = _list(f"name = '{_drive_quote(name)}'", token)
    return sheets[0] if sheets else None


def free_name(name, token):
    """'name (2)', or the first number not in use."""
    used = {s["name"] for s in _list(f"name contains '{_drive_quote(name)}'", token)}
    n = 2
    while f"{name} ({n})" in used:
        n += 1
    return f"{name} ({n})"


def upload_folder(token):
    """Id of the FOLDER_NAME folder, created if missing (or trashed).

    With drive.file the app only sees a folder it created itself: a folder
    made by hand with the same name is not used.
    """
    folders = _list(f"name = '{_drive_quote(FOLDER_NAME)}'", token, FOLDER_TYPE)
    if folders:
        return folders[0]["id"]
    req = urllib.request.Request(
        FILES_URL + "?fields=id",
        data=json.dumps({"name": FOLDER_NAME, "mimeType": FOLDER_TYPE}).encode(),
        method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        })
    status, _, body = _request(req)
    if status != 200:
        raise Error(_api_message(status, body))
    return json.loads(body)["id"]


def upload(path, token, name=None, replace=None):
    """Uploads `path` as Google Sheets. Returns {id, name, webViewLink}.

    name:    the spreadsheet's name (otherwise the file's, without extension)
    replace: id of an existing spreadsheet; its content is overwritten and it
             keeps its link and its folder (checked live with csv and xlsx)
    A new spreadsheet goes into the FOLDER_NAME folder.
    """
    if not os.path.isfile(path):
        raise Error(f"The file {path} does not exist.")
    extension = os.path.splitext(path)[1].lower()
    if extension == ".parquet":
        with tempfile.TemporaryDirectory() as tmp:
            return upload(parquet_to_csv(path, tmp), token, name, replace)
    if extension not in DRIVE_TYPES:
        raise Error(f"{os.path.basename(path)}: only .csv, .xlsx and .parquet can be opened.")
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise Error(f"{os.path.basename(path)} is {size / 2**20:.0f} MB and "
                    "Google Sheets converts up to 100 MB.")

    # Resumable upload: metadata first, then the content
    if replace:
        url, method, meta = f"{UPLOAD_URL}/{replace}", "PATCH", {}
    else:
        url, method = UPLOAD_URL, "POST"
        meta = {"name": name or sheet_name(path), "mimeType": SHEETS_TYPE,
                "parents": [upload_folder(token)]}
    req = urllib.request.Request(
        url + "?uploadType=resumable&fields=id,name,webViewLink",
        data=json.dumps(meta).encode(), method=method, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": DRIVE_TYPES[extension],
            "X-Upload-Content-Length": str(size),
        })
    status, headers, body = _request(req)
    if status != 200 or not headers.get("Location"):
        raise Error(_api_message(status, body))

    with open(path, "rb") as f:
        req = urllib.request.Request(
            headers["Location"], data=f, method="PUT", headers={
                "Content-Type": DRIVE_TYPES[extension],
                "Content-Length": str(size),
            })
        status, _, body = _request(req)
    if status not in (200, 201):
        raise Error(_api_message(status, body))
    return json.loads(body)


def _arguments():
    p = argparse.ArgumentParser(
        prog="open_in_sheets.py",
        description="Uploads spreadsheets to Google Sheets and opens them.")
    p.add_argument("files", nargs="*")
    p.add_argument("--account", metavar="EMAIL",
                   help="required when there is more than one account")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--add-account", action="store_true")
    action.add_argument("--profiles", action="store_true",
                        help="list the Chrome profiles: folder<TAB>label")
    p.add_argument("--profile", metavar="FOLDER",
                   help="with --add-account: Chrome profile where Google opens")
    action.add_argument("--accounts", action="store_true",
                        help="list the accounts, last used first")
    action.add_argument("--remove", metavar="EMAIL")
    action.add_argument("--exists", action="store_true",
                        help="if already uploaded, print its name and a free one for a copy")
    action.add_argument("--open-uploaded", action="store_true",
                        help="open the already uploaded spreadsheet, without uploading")
    action.add_argument("--overwrite", action="store_true",
                        help="overwrite the already uploaded spreadsheet (keeps its link)")
    action.add_argument("--name", help="upload a copy with this name")
    return p


def _account_to_use(account, browser):
    if account:
        return account
    available = accounts()
    if not available:
        return login(browser)
    if len(available) == 1:
        return available[0]
    raise Error("There are several accounts; choose one with --account: "
                + ", ".join(available))


def main(argv, browser=open_in_browser):
    parser = _arguments()
    a = parser.parse_args(argv)
    if a.add_account:
        print(login(browser, profile=a.profile))
        return 0
    if a.profiles:
        for folder, label in chrome_profiles():
            print(f"{folder}\t{label}")
        return 0
    if a.accounts:
        for account in accounts():
            print(account)
        return 0
    if a.remove:
        remove_account(a.remove)
        return 0
    if not a.files:
        parser.print_usage(sys.stderr)
        return 2

    account = _account_to_use(a.account, browser)
    token = access_token(account, browser)
    mark_last(account)
    for path in a.files:
        name = sheet_name(path)
        if a.exists:
            sheet = find_uploaded(name, token)
            if sheet:
                print(sheet["name"])
                print(free_name(name, token))
            continue
        if a.open_uploaded or a.overwrite:
            sheet = find_uploaded(name, token)
            if not sheet:
                raise Error(f'There is no spreadsheet "{name}" uploaded to {account}.')
            if a.overwrite:
                sheet = upload(path, token, replace=sheet["id"])
        else:
            sheet = upload(path, token, name=a.name)
        link = link_for_account(sheet["webViewLink"], account)
        browser(link, account)
        print(link)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Error as e:
        print(e, file=sys.stderr)
        sys.exit(1)
