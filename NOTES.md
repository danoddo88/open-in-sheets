# Notes — Open in Sheets

The project log: what it is, the decisions taken and why, what changed, the
bugs found and what is still unverified. Read it before changing anything;
update it in the same change.

## What it is

Double-clicking a `.csv`, `.xlsx` or `.parquet` file on a Mac opens it in
Google Sheets. Pieces:

- `open_in_sheets.py`: OAuth (loopback + PKCE) and resumable upload to the
  Drive API asking for conversion to Sheets. Standard library only, except
  `pyarrow` to turn parquet into CSV.
- `build_mac_app.py`: builds the `~/Applications/Open in Sheets.app` applet
  (osacompile), declares the file types and makes it the default. Also
  builds `Install.app`.
- `installer.py`: setup assistant window (tkinter) with Back / Next.
- `Install.app`: launcher for the assistant (applet, no fixed paths).
- `tests.py`: smoke tests without network; a local server plays Google.

## Decisions

- **Official API with your own Google Cloud project** (2026-10-23).
  Discarded:
  - copy and paste: breaks with files of several MB;
  - a Chrome extension: it also needs a Cloud credential, or automating the
    Sheets UI, which breaks when Google changes it;
  - Google Drive for desktop: needs an admin password, waiting for sync, and
    (unverified) opens a CSV as a preview, not as a spreadsheet;
  - Apps Script with clasp: less setup, but it leaves a public URL that
    creates spreadsheets, and has smaller limits.

  Nothing ready-made was found on GitHub: `jlevers/open-in-sheets` (cited by
  a guide) returns 404, `csv2gsheets` works per folder rather than per file,
  and `spreadit` appends data to an existing spreadsheet.
- **`drive.file` scope**: the minimum; the app only sees what it creates.
- **Credential and tokens in `~/.config/open-in-sheets/`** (mode 600), out of
  any synced folder: they give access to the Drive.
- **An app of its own** (AppleScript applet built with `osacompile`), with
  its own bundle id (`local.open-in-sheets`).
- **Parquet is converted on the Mac**: Drive does not convert it.
- **No dependencies for OAuth/Drive** (urllib): nothing to install besides
  `pyarrow`.
- **Several accounts**: one token per account in `accounts/<email>.json`.
  The email comes from the `id_token`, hence the extra `openid email`
  scopes. Authorization uses `prompt=select_account` so Google always asks
  for the account. With one account the double-click does not ask; with
  several, `choose from list` with the last used first. Without `--account`
  and with several accounts, the script refuses to upload instead of
  guessing.
- **Audience Internal by default**: Google would not let the app be
  published (see History), and in Testing the refresh token expires after 7
  days. Internal needs every account to belong to the project's Workspace
  organization; for outside accounts the README explains External.
- **Already uploaded file**: before uploading, the app asks Drive
  (`--exists`) whether a spreadsheet with the same name created by the app
  exists. If so, `choose from list` with: open the uploaded one (first and
  preselected), overwrite, copy with another name (suggests a free
  "name (N)"), or Cancel. A list because `display dialog` allows at most 3
  buttons. Overwrite does a resumable PATCH on the existing id: checked live
  with csv and xlsx that Drive converts and keeps the id and the link.
- **Setup assistant in a window** (no Terminal, Back / Next): tkinter (Tk
  9.0 from uv's Python) with 5 steps. Slow work (tests, waiting for the
  browser) runs in a thread and the window updates through a queue, because
  Tk must only be touched from the thread that created it. Next stays
  disabled until the step is done (Mac prepared, credential, at least one
  account). `Install.app` looks for `installer.py` next to itself and for
  Python under HOME, no fixed paths; errors go to
  `~/Library/Logs/open-in-sheets-installer.log`.
- **New spreadsheets go into a folder** (`FOLDER_NAME`, "Open in Sheets")
  instead of the root of My Drive. The app creates it the first time and
  reuses it; with `drive.file` it only sees the folder it created, so a
  hand-made folder with the same name is not used, and a deleted one is
  created again. Overwrite keeps the spreadsheet in its folder. The
  already-uploaded check still looks everywhere, so spreadsheets uploaded
  to the root before this change are found too. Checked live.
- **Everything in English** (code, UI, docs) for the public repository.

## History

### 2026-10-23
- First version; app built and made default for csv, xlsx and parquet,
  checked with `NSWorkspace.urlForApplication`. `pyarrow` 26.0.0 in the
  global Python's user site.
- Several accounts, already-uploaded check, Chrome profiles, setup assistant
  window.
- Real install: Google would not publish the app ("To publish your app, you
  must complete your configuration on the Branding page", with Branding
  complete). It is a Google issue: several October 2026 threads on
  discuss.google.dev report the same, unanswered. The audience was switched
  to **Internal**.
- Real use: uploads, already-uploaded detection and overwrite checked live
  against Drive.
- Translated to English and renamed (from a Spanish first version): files,
  app (`Open in Sheets.app`, `Install.app`), config folder
  (`~/.config/open-in-sheets`, with `client.json`, `accounts/`,
  `last_account`). The existing credential and account were moved over and
  kept working without re-authorizing (the token field `vence` became
  `expires_at`; the old token simply refreshed). 38/38 tests.
- New spreadsheets go into the "Open in Sheets" folder. 40/40 tests;
  checked live that the folder is created once and reused.

## Bugs found

- **The refreshed token did not keep mode 600** when the file already
  existed: `os.open` only applies the mode on creation. Fixed with `chmod`;
  test `an_expired_token_is_refreshed_and_keeps_the_refresh_token`.
- **It did not become the default app and did not say why**: the Swift
  script was missing `import Foundation` (did not compile) and the error was
  discarded. Also, `LSSetDefaultRoleHandlerForContentType` returns OK but no
  longer changes anything on current macOS. Switched to
  `NSWorkspace.setDefaultApplication`, and Swift's error is now shown. Test
  `the_program_that_sets_the_default_app_compiles`.
- **First real double-click: "Can't make quoted form of script into type
  Unicode text"**. The applet property was called `SCRIPT`, an AppleScript
  reserved word (`script`, case-insensitive): it compiles, but at run time
  it is not the path. Renamed to `SCRIPT_PATH`. Test
  `the_app_passes_the_arguments_to_the_script` compiles the applet and runs
  its `runTool` handler for real (with `echo` instead of Python), and
  `the_app_handlers_run_without_tripping_on_reserved_words` runs
  `chooseAccount` and `whatToDo`. Lesson: compiling with `osacompile` is not
  enough; it has to run.
- **First real use: the spreadsheet "did not open"**. It was uploaded, but
  `open <url>` sent it to Chrome's last active window, which belonged to the
  profile of another account (one Chrome profile per account). Now the
  profile whose `user_name` is the chosen account is looked up in Chrome's
  `Local State` and the link opens with `open -na "Google Chrome" --args
  --profile-directory=<profile>`. Without such a profile (or without
  Chrome), the default browser as before. Re-authorizing an account also
  opens in its profile. Test
  `the_sheet_opens_in_the_chrome_profile_of_that_account`.
- **Adding an account kept waiting**: Google's page opened in Chrome's last
  active window; if the account to add was in another profile it could not
  be chosen and the login waited in vain (the process had to be killed).
  Now, before opening Google, the app lists the Chrome profiles
  (`--profiles`, read from `Local State`, with their account) and you choose
  where to open (`--add-account --profile <folder>`); with one profile it
  does not ask. In the assistant it is a dropdown next to "Add account".
  Tests `adding_an_account_opens_google_in_the_chosen_chrome_profile` and
  `the_app_understands_the_profile_list` (AppleScript run for real).

## Pending or unverified

- The console addresses (`/auth/overview`, `/auth/audience`,
  `/auth/clients`) and button names come from what is known about the
  console; not checked one by one.
- CSV files separated by `;`: unknown whether Google splits them into
  columns.
- Whether `authuser=<email>` is enough when the account has no Chrome
  profile of its own. Opening in the account's profile ran without errors
  on 2026-10-23; not yet confirmed by eye with another profile in front.
- Whether a Google Workspace account lets an unverified app be authorized:
  it depends on what its admin allows.
- With audience Internal only accounts of the organization can be added; an
  outside account (for example a personal gmail) is rejected by Google.
- The account list, the app's own buttons, the "already uploaded" list and
  the copy-name box: their code runs in the tests, but they were not tried
  with real clicks.
- The assistant with real clicks: that the window comes to the front when
  opened from `Install.app`, and the credential step from scratch (only
  tried with the credential already installed and with fake actions).
