# Open in Sheets

Double-click a `.csv`, `.xlsx` or `.parquet` file on a Mac → it is uploaded to
your Google Drive converted to Google Sheets and opened in the browser.

## Requirements

- macOS on Apple Silicon.
- Python 3.14 installed with [uv](https://docs.astral.sh/uv/)
  (`uv python install 3.14`), at
  `~/.local/share/uv/python/cpython-3.14-macos-aarch64-none/`. No virtual
  environment: `pyarrow` goes into that Python's user site.
- A Google account and a Google Cloud project of your own (the assistant
  guides you through creating it).
- Google Chrome is optional: if present, each spreadsheet opens in the
  profile signed in with the chosen account.

If `Install.app` was downloaded from the internet, macOS blocks it the first
time (it is not signed by a registered developer): right-click → Open, or
build it again locally with
`python3.14 -c "import build_mac_app; build_mac_app.build_installer()"`.

## Install (or reinstall)

Double-click **`Install.app`**: it opens an assistant window with
**Back / Next**. Whatever is already done shows as done.

1. **Welcome**.
2. **Prepare the Mac**: installs `pyarrow` in the global Python (for
   `.parquet`), runs the tests (`tests.py`) and builds
   `~/Applications/Open in Sheets.app` as the default app for the three types.
3. **Google credential**: one button per console page (create project,
   enable the Drive API, consent screen with audience **Internal**, create
   the *Desktop app* client), opened inside the project whose ID you paste
   there. It picks up `client_secret_*.json` from Downloads by itself (or you
   choose it by hand).
4. **Google accounts**: add (chosen in the browser, in the Chrome profile you
   pick) and remove.
5. **Done**.

Step 3 is the only manual part: Google does not allow creating the credential
without going through its console. It is done once.

`Install.app` stores no paths of your Mac: it looks for `installer.py` next to
itself and for uv's Python under HOME. If nothing opens, the error is in
`~/Library/Logs/open-in-sheets-installer.log`.

### Internal or External

**Internal** (what the assistant suggests) works if the project was created
with a company account (Google Workspace) and every account you will use
belongs to that organization: no need to publish, and the authorization does
not expire.

If you need an account from outside (for example an @gmail.com one), the
audience must be **External**: either publish the app (In production), or
leave it in Testing adding the accounts under *Test users*; then the
authorization expires every 7 days (the app asks for it again by itself). In
October 2026 Google was not letting personal-use apps be published (see
NOTES).

## If the file was already uploaded

When you double-click, if that account already has a spreadsheet with the
same name uploaded by the app, a list shows up:

- **Open the uploaded one** (preselected): opens that spreadsheet, uploads nothing.
- **Overwrite it with this file**: replaces its content; it keeps its link.
- **Open a copy with another name**: suggests "name (2)" (editable).
- **Cancel**: does nothing.

Only spreadsheets uploaded by the app are detected: with the `drive.file`
scope it cannot see the others.

## Several accounts

- **Double-click** with a single account: uploads right away. With several:
  asks which one, with the last used preselected, and offers "Add another
  account...".
- **Open the app by itself** (no file): shows the accounts and lets you add
  or remove. Before opening Google to add one, it asks which Chrome profile
  to use: the account must be signed in (or able to sign in) there. Removing
  one also asks Google to revoke the access.
- The spreadsheet opens in the Chrome profile of that account, with
  `authuser=<email>` in the link.

## Where everything lives

| What | Where | Why there |
|---|---|---|
| Code | this folder | |
| App | `~/Applications/Open in Sheets.app` | it stores paths of this Mac |
| Credential and one token per account | `~/.config/open-in-sheets/` (`accounts/<email>.json`, mode 600) | they give access to your Drive: keep them out of synced folders |
| `pyarrow` | user site of uv's global Python | no virtual environments |

## Command line

```
python3.14 open_in_sheets.py --account me@example.com file.csv other.xlsx
python3.14 open_in_sheets.py --add-account [--profile "Profile 1"]
python3.14 open_in_sheets.py --accounts
python3.14 open_in_sheets.py --profiles
python3.14 open_in_sheets.py --remove me@example.com
python3.14 tests.py                      # tests, no network
```

## Limits

- Google converts up to **100 MB or 20 million cells** per spreadsheet
  ([source](https://support.google.com/drive/answer/37603)). A `.parquet`
  file is measured after turning it into CSV, which is usually much bigger.
- New spreadsheets go into an **Open in Sheets** folder in My Drive, created
  the first time (and again if it is deleted). Overwriting leaves a
  spreadsheet where it was. To use another folder name, change
  `FOLDER_NAME` in `open_in_sheets.py`.
- Requested scopes: `drive.file` (the app only sees what it created) and the
  account's email (to tell the accounts apart).
