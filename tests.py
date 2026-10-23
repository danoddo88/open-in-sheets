"""Open in Sheets smoke tests. No network, no Google, nothing to install.

    python tests.py

A local server plays Google (token and Drive upload) to test the whole path:
authorize, refresh and upload.
"""

import http.server
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import build_mac_app  # noqa: E402
import installer  # noqa: E402
import open_in_sheets  # noqa: E402


class FakeGoogle(http.server.BaseHTTPRequestHandler):
    requests = []
    token_response = (200, {"access_token": "new", "expires_in": 3600})
    upload_response = None  # (status, dict) to force an error
    in_drive = []  # spreadsheets the app "already uploaded": {id, name, webViewLink}
    folders = []  # folders the app created: {id, name}

    def _answer(self, status, data, headers=()):
        body = json.dumps(data).encode()
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_POST(self):
        body = self._body()
        self.requests.append((self.command, self.path, dict(self.headers), body))
        if self.path.startswith("/token"):
            self._answer(*self.token_response)
        elif self.path.startswith("/revoke"):
            self._answer(200, {})
        elif self.path.startswith("/files"):  # creating a folder
            folder = {"id": f"folder{len(self.folders) + 1}", **json.loads(body)}
            self.folders.append(folder)
            self._answer(200, {"id": folder["id"]})
        elif self.upload_response:
            self._answer(*self.upload_response)
        else:
            session = f"http://127.0.0.1:{self.server.server_port}/session"
            self._answer(200, {}, [("Location", session)])

    do_PATCH = do_POST  # replacing a spreadsheet: same resumable upload

    def do_PUT(self):
        body = self._body()
        self.requests.append(("PUT", self.path, dict(self.headers), body))
        self._answer(200, {"id": "abc", "name": "x",
                           "webViewLink": "https://docs.google.com/spreadsheets/d/abc"})

    def do_GET(self):
        # Drive file list: understands "name = '...'" and "name contains '...'"
        self.requests.append(("GET", self.path, dict(self.headers), b""))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["q"][0]
        m = re.match(r"name (=|contains) '((?:[^'\\]|\\.)*)'", q)
        value = re.sub(r"\\(.)", r"\1", m.group(2))
        if "google-apps.folder" in q:
            self._answer(200, {"files": [f for f in self.folders if f["name"] == value]})
            return
        if m.group(1) == "=":
            sheets = [s for s in self.in_drive if s["name"] == value]
        else:
            sheets = [s for s in self.in_drive if value in s["name"]]
        self._answer(200, {"files": sheets})

    def log_message(self, *args):
        pass


def _setup():
    """Fake server + temporary config folder. Returns the folder."""
    FakeGoogle.requests = []
    FakeGoogle.token_response = (200, {"access_token": "new", "expires_in": 3600})
    FakeGoogle.upload_response = None
    FakeGoogle.in_drive = []
    FakeGoogle.folders = []
    folder = tempfile.mkdtemp()
    open_in_sheets.CONFIG_DIR = folder
    with open(os.path.join(folder, "client.json"), "w") as f:
        json.dump({"installed": {"client_id": "id", "client_secret": "secret"}}, f)
    return folder


def _write(folder, name, content):
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _uploads():
    """The upload requests (metadata and content), without folder lookups."""
    return [r for r in FakeGoogle.requests if r[1].startswith(("/upload", "/session"))]


def _compiled_app(tmp, python="/bin/echo", script="/s.py"):
    """The real AppleScript of the app, compiled, to run its handlers."""
    source = os.path.join(tmp, "app.applescript")
    compiled = os.path.join(tmp, "app.scpt")
    with open(source, "w", encoding="utf-8") as f:
        f.write(build_mac_app.applescript_source(python, script))
    subprocess.run(["osacompile", "-o", compiled, source], check=True)
    return compiled


# ------------------------------------------------------------------- tests

def the_requested_scope_is_drive_file_plus_the_email():
    url = open_in_sheets.authorization_url("id", "http://127.0.0.1:1", "c", "s")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert sorted(q["scope"][0].split()) == [
        "email", "https://www.googleapis.com/auth/drive.file", "openid"], q["scope"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"], "without offline there is no refresh_token"


def uploading_a_csv_converts_it_to_sheets_with_the_file_name():
    folder = _setup()
    content = "item,count\nalpha,30\nñandú,41\n".encode()
    path = _write(folder, "october sales.csv", content)
    sheet = open_in_sheets.upload(path, "TOKEN")
    assert sheet["webViewLink"].startswith("https://docs.google.com/"), sheet
    (_, url, headers, meta), (method, _, put_headers, body) = _uploads()
    assert "uploadType=resumable" in url
    assert headers["Authorization"] == "Bearer TOKEN"
    meta = json.loads(meta)
    assert meta == {"name": "october sales",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "parents": ["folder1"]}, meta
    assert method == "PUT" and put_headers["Content-Type"] == "text/csv"
    assert body == content, "the content arrived different"


def a_csv_of_several_megabytes_arrives_whole():
    folder = _setup()
    content = b"a,b,c\n" + b"1234567890,abcdefghij,xyz\n" * 300_000  # ~8 MB
    path = _write(folder, "big.csv", content)
    open_in_sheets.upload(path, "TOKEN")
    assert FakeGoogle.requests[-1][3] == content


def a_csv_over_100_mb_is_rejected_without_uploading_anything():
    folder = _setup()
    path = os.path.join(folder, "huge.csv")
    with open(path, "wb") as f:
        f.truncate(open_in_sheets.MAX_BYTES + 1)
    try:
        open_in_sheets.upload(path, "TOKEN")
    except open_in_sheets.Error as e:
        assert "100 MB" in str(e), e
    else:
        raise AssertionError("it uploaded a file over 100 MB")
    assert FakeGoogle.requests == []


def a_drive_error_is_shown_with_googles_message():
    folder = _setup()
    FakeGoogle.upload_response = (403, {"error": {"message": "Drive API has not been used"}})
    path = _write(folder, "a.csv", b"x\n")
    try:
        open_in_sheets.upload(path, "TOKEN")
    except open_in_sheets.Error as e:
        assert "403" in str(e) and "Drive API has not been used" in str(e), e
    else:
        raise AssertionError("it did not report the error")


def _save_account(folder, email, **token):
    path = os.path.join(folder, "accounts", email + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(token, f)
    return path


def _id_token(email):
    """An id_token shaped like Google's (the signature is not checked)."""
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=")
    return "header." + payload.decode() + ".signature"


def _browser_that_authorizes(email, urls=None):
    """Plays Google in the browser: goes back to the redirect with a code, and
    the fake token server answers with the chosen email."""
    FakeGoogle.token_response = (200, {"access_token": "A", "refresh_token": "R",
                                       "expires_in": 3600, "id_token": _id_token(email)})

    def browser(url, email=None, profile=None):
        if urls is not None:
            urls.append(url)
        browser.profile = profile
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "redirect_uri" not in q:
            return  # the spreadsheet link
        back = q["redirect_uri"][0] + "/?" + urllib.parse.urlencode(
            {"code": "CODE", "state": q["state"][0]})
        threading.Thread(target=lambda: urllib.request.urlopen(back).read()).start()
    return browser


def a_valid_token_is_used_without_asking_anything():
    folder = _setup()
    _save_account(folder, "first@example.com", access_token="old", expires_at=time.time() + 600)
    assert open_in_sheets.access_token("first@example.com") == "old"
    assert FakeGoogle.requests == []


def an_expired_token_is_refreshed_and_keeps_the_refresh_token():
    folder = _setup()
    path = _save_account(folder, "first@example.com", access_token="old",
                         refresh_token="R", expires_at=0)
    assert open_in_sheets.access_token("first@example.com") == "new"
    fields = urllib.parse.parse_qs(FakeGoogle.requests[0][3].decode())
    assert fields["grant_type"] == ["refresh_token"] and fields["refresh_token"] == ["R"]
    with open(path) as f:
        saved = json.load(f)
    assert saved["refresh_token"] == "R", "the refresh_token was lost"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def revoked_access_asks_that_account_to_authorize_again():
    folder = _setup()
    _save_account(folder, "first@example.com", access_token="old", refresh_token="R", expires_at=0)
    urls = []
    browser = _browser_that_authorizes("first@example.com", urls)
    # The refresh fails; the login that follows gets a good token ("A")
    original = open_in_sheets._post_form
    answers = [(400, {"error": "invalid_grant"})]
    open_in_sheets._post_form = lambda url, fields: (
        answers.pop(0) if answers else original(url, fields))
    try:
        assert open_in_sheets.access_token("first@example.com", browser) == "A"
    finally:
        open_in_sheets._post_form = original
    q = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
    assert q["login_hint"] == ["first@example.com"], "it did not hint the account to re-authorize"


def authorizing_saves_the_token_under_the_accounts_email():
    folder = _setup()
    urls = []
    assert open_in_sheets.login(_browser_that_authorizes("first@example.com", urls)) == "first@example.com"
    fields = urllib.parse.parse_qs(FakeGoogle.requests[0][3].decode())
    assert fields["code"] == ["CODE"] and "code_verifier" in fields
    with open(os.path.join(folder, "accounts", "first@example.com.json")) as f:
        assert json.load(f)["refresh_token"] == "R"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
    assert "select_account" in q["prompt"][0], "with several accounts it must ask which"


def several_accounts_can_be_added_and_the_last_used_goes_first():
    _setup()
    open_in_sheets.login(_browser_that_authorizes("first@example.com"))
    open_in_sheets.login(_browser_that_authorizes("work@example.com"))
    assert open_in_sheets.accounts() == ["work@example.com", "first@example.com"]
    open_in_sheets.mark_last("first@example.com")
    assert open_in_sheets.accounts() == ["first@example.com", "work@example.com"]


def removing_an_account_deletes_it_and_tells_google():
    folder = _setup()
    _save_account(folder, "first@example.com", refresh_token="R")
    _save_account(folder, "second@example.com", refresh_token="B")
    open_in_sheets.mark_last("first@example.com")
    assert open_in_sheets.main(["--remove", "first@example.com"]) == 0
    assert open_in_sheets.accounts() == ["second@example.com"]
    (method, url, _, body), = FakeGoogle.requests
    assert url.startswith("/revoke") and urllib.parse.parse_qs(body.decode())["token"] == ["R"]


def with_several_accounts_and_none_chosen_it_does_not_upload_blindly():
    folder = _setup()
    _save_account(folder, "first@example.com", access_token="T", expires_at=time.time() + 600)
    _save_account(folder, "second@example.com", access_token="T", expires_at=time.time() + 600)
    path = _write(folder, "a.csv", b"x\n")
    try:
        open_in_sheets.main([path], browser=lambda u, c=None: None)
    except open_in_sheets.Error as e:
        assert "first@example.com" in str(e) and "second@example.com" in str(e), e
    else:
        raise AssertionError("it uploaded without knowing to which account")
    assert FakeGoogle.requests == []


def double_click_with_a_chosen_account_uploads_there_and_opens_the_sheet_as_it():
    folder = _setup()
    _save_account(folder, "first@example.com", access_token="FIRSTS", expires_at=time.time() + 600)
    _save_account(folder, "second@example.com", access_token="SECONDS", expires_at=time.time() + 600)
    path = _write(folder, "a.csv", b"x\n")
    opened = []
    assert open_in_sheets.main(["--account", "second@example.com", path],
                               browser=lambda u, c=None: opened.append(u)) == 0
    assert FakeGoogle.requests[0][2]["Authorization"] == "Bearer SECONDS"
    assert opened == ["https://docs.google.com/spreadsheets/d/abc?authuser=second%40example.com"], opened
    assert open_in_sheets.last_account() == "second@example.com"


def without_the_credential_the_error_explains_what_is_missing():
    folder = _setup()
    os.remove(os.path.join(folder, "client.json"))
    try:
        open_in_sheets.read_client()
    except open_in_sheets.Error as e:
        assert "client.json" in str(e) and "README" in str(e), e
    else:
        raise AssertionError("it did not report the missing credential")


def double_click_with_a_single_account_uploads_without_asking():
    folder = _setup()
    _save_account(folder, "first@example.com", access_token="T", expires_at=time.time() + 600)
    path = _write(folder, "a.csv", b"x\n")
    opened = []
    assert open_in_sheets.main([path], browser=lambda u, c=None: opened.append(u)) == 0
    assert opened == ["https://docs.google.com/spreadsheets/d/abc?authuser=first%40example.com"]


class _FakeActions:
    """What the assistant would do to the Mac and to Google, pretended."""
    def __init__(self):
        self.account_list, self.client, self.urls = [], False, []
        self.profile_used = "not called"

    def prepare(self, report):
        report("preparing")

    def open_url(self, url, email=None):
        self.urls.append(url)

    def copy(self, text):
        pass

    def find_downloaded_client(self):
        return None

    def install_client(self, path):
        self.client = True

    def client_installed(self):
        return self.client

    def accounts(self):
        return list(self.account_list)

    def profiles(self):
        return [("Profile 2", "Work (first@example.com)"), ("Profile 1", "Personal (third@example.com)")]

    def add_account(self, profile=None):
        self.profile_used = profile
        self.account_list.append("first@example.com")
        return "first@example.com"

    def remove_account(self, email):
        self.account_list.remove(email)


def _wait(root, condition, seconds=5):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        root.update()
        time.sleep(0.02)
    assert condition(), "it did not finish in time"


def the_assistant_needs_each_step_done_to_continue_and_goes_back_and_forth():
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    act = _FakeActions()
    a = installer.Assistant(root, act)
    enabled = lambda: str(a.next_button["state"]) == "normal"  # noqa: E731
    try:
        assert a.page == "welcome" and str(a.back_button["state"]) == "disabled"
        a.next()
        assert a.page == "prepare"
        _wait(root, lambda: a.prepared)
        root.update()
        assert enabled(), "prepared, but Next is disabled"
        a.next()
        assert a.page == "credential" and not enabled(), "continued without a credential"
        a.project.set("my-project")
        a.console_buttons[0].invoke()
        assert act.urls[-1].endswith("?project=my-project"), act.urls
        a._install_json("/x/client_secret_1.json")
        assert enabled(), "with a credential, Next is disabled"
        a.next()
        assert a.page == "accounts" and not enabled(), "continued without accounts"
        assert a.chosen_profile.get() == "Work (first@example.com)"
        a.chosen_profile.current(1)
        a.add_account()
        _wait(root, lambda: not a.busy)
        assert act.profile_used == "Profile 1", "it did not use the chosen Chrome profile"
        assert enabled() and a.account_list.get(0) == "first@example.com"
        a.next()
        assert a.page == "done" and a.next_button["text"] == "Finish"
        a.back()
        assert a.page == "accounts"
    finally:
        root.destroy()


def the_assistant_launcher_has_no_paths_of_this_mac():
    source = build_mac_app.installer_source()
    assert os.path.expanduser("~") not in source, "HOME is hard-coded"
    assert "path to me" in source and "installer.py" in source
    with tempfile.TemporaryDirectory() as tmp:
        target = build_mac_app.build_installer(os.path.join(tmp, "Install.app"))
        with open(os.path.join(target, "Contents", "Info.plist"), "rb") as f:
            assert plistlib.load(f)["CFBundleIdentifier"] == "local.open-in-sheets.installer"


def an_xlsx_is_uploaded_as_excel_for_google_to_convert():
    folder = _setup()
    path = _write(folder, "Book.XLSX", b"PK\x03\x04fake")
    open_in_sheets.upload(path, "TOKEN")
    (_, _, headers, meta), (_, _, put_headers, _) = _uploads()
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert headers["X-Upload-Content-Type"] == xlsx and put_headers["Content-Type"] == xlsx
    assert json.loads(meta)["name"] == "Book"


def a_parquet_is_turned_into_csv_before_uploading():
    try:
        import pyarrow
        import pyarrow.parquet
    except ImportError:
        raise AssertionError("pyarrow is missing: run installer.py") from None
    folder = _setup()
    path = os.path.join(folder, "data.parquet")
    pyarrow.parquet.write_table(
        pyarrow.table({"item": ["alpha", "ñandú"], "count": [30, 41]}), path)
    open_in_sheets.upload(path, "TOKEN")
    (_, _, headers, meta), (_, _, _, body) = _uploads()
    assert headers["X-Upload-Content-Type"] == "text/csv"
    assert json.loads(meta)["name"] == "data"
    lines = body.decode().splitlines()
    assert lines[0].replace('"', "") == "item,count", lines
    assert lines[2].replace('"', "") == "ñandú,41", lines


def without_pyarrow_a_parquet_explains_how_to_fix_it():
    folder = _setup()
    path = _write(folder, "data.parquet", b"PAR1")
    saved = {k: sys.modules.get(k) for k in ("pyarrow", "pyarrow.csv", "pyarrow.parquet")}
    sys.modules.update({k: None for k in saved})  # import -> ImportError
    try:
        open_in_sheets.upload(path, "TOKEN")
    except open_in_sheets.Error as e:
        assert "pyarrow" in str(e) and "Install.app" in str(e), e
    else:
        raise AssertionError("it did not report the missing pyarrow")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def any_other_file_type_is_rejected_without_uploading_anything():
    folder = _setup()
    path = _write(folder, "notes.txt", b"hello")
    try:
        open_in_sheets.upload(path, "TOKEN")
    except open_in_sheets.Error as e:
        assert ".csv, .xlsx and .parquet" in str(e), e
    else:
        raise AssertionError("it uploaded a .txt")
    assert FakeGoogle.requests == []


def the_app_opens_csv_xlsx_and_parquet_and_runs_this_script():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "Test.app")
        build_mac_app.build_app(target=target, register=False)
        with open(os.path.join(target, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        assert info["CFBundleIdentifier"] == "local.open-in-sheets"
        types = [t for d in info["CFBundleDocumentTypes"] for t in d["LSItemContentTypes"]]
        assert types == ["public.comma-separated-values-text",
                         "org.openxmlformats.spreadsheetml.sheet",
                         "org.apache.parquet"], types
        parquet = info["UTImportedTypeDeclarations"][0]
        assert parquet["UTTypeTagSpecification"]["public.filename-extension"] == ["parquet"]
    source = build_mac_app.applescript_source("/py", os.path.join(BASE_DIR, "open_in_sheets.py"))
    assert os.path.join(BASE_DIR, "open_in_sheets.py") in source
    assert "on open droppedFiles" in source and '"--add-account' in source and '"--accounts"' in source


def the_installer_takes_the_newest_client_secret_from_downloads():
    with tempfile.TemporaryDirectory() as tmp:
        old = _write(tmp, "client_secret_1.json", b"{}")
        os.utime(old, (1, 1))
        new = _write(tmp, "client_secret_2.json", b"{}")
        _write(tmp, "something_else.json", b"{}")
        assert installer.find_downloaded_client(tmp) == new
    with tempfile.TemporaryDirectory() as tmp:
        assert installer.find_downloaded_client(tmp) is None


def _account_and_file(file_name, in_drive=()):
    """An account ready, a local file and what is "already" in Drive."""
    folder = _setup()
    _save_account(folder, "first@example.com", access_token="T", expires_at=time.time() + 600)
    FakeGoogle.in_drive = [
        {"id": f"id{i}", "name": n, "webViewLink": f"https://docs.google.com/spreadsheets/d/id{i}"}
        for i, n in enumerate(in_drive)]
    return _write(folder, file_name, b"a,b\n1,2\n")


def _run(*argv):
    """main() returning (what it printed, links opened)."""
    import contextlib
    import io
    output, opened = io.StringIO(), []
    with contextlib.redirect_stdout(output):
        assert open_in_sheets.main(list(argv), browser=lambda u, c=None: opened.append(u)) == 0
    return output.getvalue(), opened


def if_not_uploaded_yet_it_asks_nothing():
    path = _account_and_file("sales.csv", in_drive=["something else"])
    output, opened = _run("--account", "first@example.com", "--exists", path)
    assert output == "" and opened == []
    assert all(r[0] == "GET" for r in FakeGoogle.requests), "it uploaded while only asking"


def if_already_uploaded_it_says_so_and_offers_a_free_name_for_a_copy():
    path = _account_and_file("sales.csv", in_drive=["sales", "sales (2)"])
    output, _ = _run("--account", "first@example.com", "--exists", path)
    assert output.splitlines() == ["sales", "sales (3)"], output


def a_name_with_a_quote_is_found_anyway():
    path = _account_and_file("it's.csv", in_drive=["it's"])
    output, _ = _run("--account", "first@example.com", "--exists", path)
    assert output.splitlines()[0] == "it's", output


def opening_the_uploaded_one_uploads_nothing_and_opens_its_link():
    path = _account_and_file("sales.csv", in_drive=["sales"])
    _, opened = _run("--account", "first@example.com", "--open-uploaded", path)
    assert opened == ["https://docs.google.com/spreadsheets/d/id0?authuser=first%40example.com"], opened
    assert all(r[0] == "GET" for r in FakeGoogle.requests), "it uploaded something"


def overwriting_replaces_the_uploaded_one_without_creating_another():
    path = _account_and_file("sales.csv", in_drive=["sales"])
    _run("--account", "first@example.com", "--overwrite", path)
    (method, url, _, meta), = [r for r in _uploads() if r[0] in ("POST", "PATCH")]
    assert method == "PATCH" and url.startswith("/upload/id0?"), (method, url)
    assert json.loads(meta) == {}, "it must not rename it"
    assert FakeGoogle.requests[-1][3] == b"a,b\n1,2\n"


def opening_a_copy_uploads_another_with_the_chosen_name():
    path = _account_and_file("sales.csv", in_drive=["sales"])
    _run("--account", "first@example.com", "--name", "sales (2)", path)
    (method, _, _, meta), = [r for r in _uploads() if r[0] in ("POST", "PATCH")]
    assert method == "POST" and json.loads(meta)["name"] == "sales (2)", meta


def new_sheets_go_into_the_app_folder_created_the_first_time():
    folder = _setup()
    first = _write(folder, "a.csv", b"x\n")
    second = _write(folder, "b.csv", b"y\n")
    open_in_sheets.upload(first, "TOKEN")
    open_in_sheets.upload(second, "TOKEN")
    assert FakeGoogle.folders == [{"id": "folder1", "name": "Open in Sheets",
                                   "mimeType": "application/vnd.google-apps.folder"}], \
        "it must create the folder once and reuse it"
    parents = [json.loads(r[3])["parents"] for r in _uploads() if r[0] == "POST"]
    assert parents == [["folder1"], ["folder1"]], parents


def overwriting_leaves_the_sheet_where_it_was():
    path = _account_and_file("sales.csv", in_drive=["sales"])
    _run("--account", "first@example.com", "--overwrite", path)
    assert FakeGoogle.folders == [], "it created a folder just to overwrite"
    (_, _, _, meta), = [r for r in _uploads() if r[0] == "PATCH"]
    assert "parents" not in json.loads(meta)


def the_app_offers_opening_the_uploaded_one_first():
    source = build_mac_app.applescript_source("/py", "/s.py")
    assert "default items {OPEN_UPLOADED}" in source
    for option in ('"--exists "', '"--open-uploaded "', '"--overwrite "', '"--name "'):
        assert option in source, option


def the_sheet_opens_in_the_chrome_profile_of_that_account():
    # It opened in Chrome's last active window, which could belong to another
    # profile (another account), and the sheet was not visible (2026-10-23)
    with tempfile.TemporaryDirectory() as tmp:
        state = _write(tmp, "Local State", json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Your Chrome", "user_name": ""},
            "Profile 1": {"name": "Personal", "user_name": "personal@example.com"},
            "Profile 2": {"name": "Work", "user_name": "Work@Example.com"},
        }}}).encode())
        url = "https://docs.google.com/x"
        assert open_in_sheets.open_command(url, "work@example.com", state) == [
            "open", "-na", "Google Chrome", "--args", "--profile-directory=Profile 2", url]
        # Account without a profile of its own, or no Chrome: the default browser
        assert open_in_sheets.open_command(url, "other@example.com", state) == ["open", url]
        assert open_in_sheets.open_command(url, "a@b.c", os.path.join(tmp, "no")) == ["open", url]


FAKE_CHROME = {"profile": {"info_cache": {
    "Default": {"name": "Your Chrome", "user_name": ""},
    "Profile 1": {"name": "Personal", "user_name": "personal@example.com"},
    "Profile 2": {"name": "Work", "user_name": "work@example.com"},
}}}


def chrome_profiles_are_listed_with_their_account_those_with_one_first():
    with tempfile.TemporaryDirectory() as tmp:
        state = _write(tmp, "Local State", json.dumps(FAKE_CHROME).encode())
        assert open_in_sheets.chrome_profiles(state) == [
            ("Profile 1", "Personal (personal@example.com)"),
            ("Profile 2", "Work (work@example.com)"),
            ("Default", "Your Chrome (no Google account)"),
        ]
        assert open_in_sheets.chrome_profiles(os.path.join(tmp, "no")) == []


def adding_an_account_opens_google_in_the_chosen_chrome_profile():
    # It opened in Chrome's last active window; if the account was in another
    # profile it could not be chosen and the login waited in vain (2026-10-23)
    with tempfile.TemporaryDirectory() as tmp:
        state = _write(tmp, "Local State", json.dumps(FAKE_CHROME).encode())
        url = "https://accounts.google.com/x"
        # the chosen profile wins, even if the hinted account is in another one
        assert open_in_sheets.open_command(
            url, "work@example.com", state, profile="Profile 1") == [
            "open", "-na", "Google Chrome", "--args", "--profile-directory=Profile 1", url]
    _setup()
    browser = _browser_that_authorizes("third@example.com")
    assert open_in_sheets.login(browser, profile="Profile 1") == "third@example.com"
    assert browser.profile == "Profile 1"


def the_app_understands_the_profile_list():
    # labelsOf and folderOf, the real AppleScript run
    with tempfile.TemporaryDirectory() as tmp:
        compiled = _compiled_app(tmp)
        lines = '{"Profile 1" & tab & "Personal (personal@example.com)", "Default" & tab & "Your Chrome (no Google account)"}'
        r = subprocess.run(
            ["osascript", "-e", f'set s to load script POSIX file "{compiled}"',
             "-e", f"set l to {lines}",
             "-e", "set AppleScript's text item delimiters to \"|\"",
             "-e", '(labelsOf(l) of s as text) & "=" & folderOf(l, "Your Chrome (no Google account)") of s'],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "Personal (personal@example.com)|Your Chrome (no Google account)=Default", r.stdout


def the_app_passes_the_arguments_to_the_script():
    # It failed with "Can't make quoted form of script into type Unicode
    # text": the property was called SCRIPT, an AppleScript reserved word
    # (2026-10-23). The real handler runs with echo standing in for Python.
    with tempfile.TemporaryDirectory() as tmp:
        compiled = _compiled_app(tmp, script="/path with spaces/s.py")
        r = subprocess.run(
            ["osascript", "-e",
             f'tell (load script POSIX file "{compiled}") to runTool("--accounts")'],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "/path with spaces/s.py --accounts", r.stdout


def the_app_handlers_run_without_tripping_on_reserved_words():
    # chooseAccount with a single account (echo prints one line) and
    # whatToDo for a file not uploaded yet: no dialogs, but all their code runs
    with tempfile.TemporaryDirectory() as tmp:
        compiled = _compiled_app(tmp)
        r = subprocess.run(
            ["osascript", "-e", f'set s to load script POSIX file "{compiled}"',
             "-e", '(chooseAccount() of s) & "|" & (whatToDo({}, "a@b.c") of s) & "|"'],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "/s.py --accounts||", r.stdout


def the_program_that_sets_the_default_app_compiles():
    # It failed silently because of a missing import (2026-10-23)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "p.swift")
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_mac_app.swift_source('/Apps/With "quotes".app'))
        r = subprocess.run(["swiftc", "-typecheck", path], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def the_installer_opens_each_page_in_the_chosen_project():
    assert installer.valid_project_id("open-in-sheets-476512")
    for bad in ("Open In Sheets", "abc", "1project", "ends-", ""):
        assert not installer.valid_project_id(bad), bad
    for _, _, page, _ in installer.CONSOLE_STEPS:
        url = installer.console_url(page, "my-project")
        assert url.startswith("https://console.cloud.google.com/")
        assert url.endswith("?project=my-project"), url


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    open_in_sheets.TOKEN_URL = base + "/token"
    open_in_sheets.UPLOAD_URL = base + "/upload"
    open_in_sheets.REVOKE_URL = base + "/revoke"
    open_in_sheets.FILES_URL = base + "/files"

    tests = [v for k, v in globals().items()
             if callable(v) and not k.startswith("_") and k != "main"
             and not isinstance(v, type) and v.__module__ == __name__]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ok    {test.__name__}")
        except Exception:
            failures += 1
            print(f"  FAIL  {test.__name__}")
            traceback.print_exc()
    server.shutdown()
    print(f"\n{len(tests) - failures}/{len(tests)} ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
