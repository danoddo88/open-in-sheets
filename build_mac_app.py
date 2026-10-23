"""Builds ~/Applications/Open in Sheets.app and makes it the spreadsheet app.

    python build_mac_app.py

It is an AppleScript applet (osacompile), an app of its own:
  - double-click a .csv, .xlsx or .parquet -> runs open_in_sheets.py; with
    several accounts it asks which one (the last used is preselected) and
    lets you add another on the spot. If that file was already uploaded (a
    spreadsheet with the same name), it asks: open the uploaded one (first
    option), overwrite it, open a copy with another name, or cancel
  - open the app by itself -> shows the accounts and lets you add or remove
If something fails, it shows the message in a dialog.

macOS does not know the .parquet type, so the app declares it itself
(UTImportedTypeDeclarations) to be able to become the default.

It lives in ~/Applications because it stores absolute paths of this Mac. If
this folder moves, run this script again.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Open in Sheets"
BUNDLE_ID = "local.open-in-sheets"
PARQUET_TYPE = "org.apache.parquet"
TYPES = [
    "public.comma-separated-values-text",
    "org.openxmlformats.spreadsheetml.sheet",
    PARQUET_TYPE,
]
TARGET = os.path.expanduser(f"~/Applications/{APP_NAME}.app")
PYTHON = os.path.expanduser(
    "~/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin/python3.14")
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def _quote(text):
    """AppleScript string quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Names inside the AppleScript avoid its reserved words (title, path, name,
# list, text, button, number, script...): a property called SCRIPT compiled
# fine and then failed at run time (2026-10-23, see NOTES).
def applescript_source(python, script):
    return f"""\
property PY : {_quote(python)}
property SCRIPT_PATH : {_quote(script)}
property APP_TITLE : {_quote(APP_NAME)}

property ADD_OTHER : "Add another account..."

on run
	-- The app by itself, no files: manage the Google accounts
	set accountList to paragraphs of runTool("--accounts")
	if (count of accountList) is 0 then
		set message to "There are no Google accounts yet."
	else
		set AppleScript's text item delimiters to return
		set message to "Google accounts:" & return & return & (accountList as text)
		set AppleScript's text item delimiters to ""
	end if
	activate
	set pressed to button returned of (display dialog message with title APP_TITLE buttons {{"Close", "Remove one...", "Add account"}} default button "Add account")
	if pressed is "Add account" then
		addAccount()
	else if pressed is "Remove one..." and (count of accountList) > 0 then
		set picked to choose from list accountList with title APP_TITLE with prompt "Which account should be removed?"
		if picked is not false then
			runTool("--remove " & quoted form of (item 1 of picked))
			display notification (item 1 of picked) with title APP_TITLE subtitle "Account removed"
		end if
	end if
end run

property OPEN_UPLOADED : "Open the uploaded one"
property OVERWRITE : "Overwrite it with this file"
property MAKE_COPY : "Open a copy with another name"

on open droppedFiles
	set account to chooseAccount()
	set accountArg to "--account " & quoted form of account & " "
	repeat with droppedFile in droppedFiles
		set filePath to quoted form of (POSIX path of droppedFile)
		set theAction to whatToDo(paragraphs of runTool(accountArg & "--exists " & filePath), account)
		if theAction is not "cancel" then
			if theAction is not "--open-uploaded " then
				display notification (do shell script "basename " & filePath) with title APP_TITLE subtitle "Uploading to " & account & "..."
			end if
			runTool(accountArg & theAction & filePath)
		end if
	end repeat
end open

-- existing: empty if not uploaded yet; otherwise (name, free name for a copy)
on whatToDo(existing, account)
	if (count of existing) is 0 then return ""
	activate
	set picked to choose from list {{OPEN_UPLOADED, OVERWRITE, MAKE_COPY}} with title APP_TITLE with prompt (item 1 of existing) & " is already uploaded to " & account & "." default items {{OPEN_UPLOADED}} OK button name "Continue" cancel button name "Cancel"
	if picked is false then return "cancel"
	set choice to item 1 of picked
	if choice is OPEN_UPLOADED then return "--open-uploaded "
	if choice is OVERWRITE then return "--overwrite "
	try
		set copyName to text returned of (display dialog "Name of the copy:" default answer (item 2 of existing) with title APP_TITLE)
	on error number -128
		return "cancel"
	end try
	return "--name " & quoted form of copyName & " "
end whatToDo

on addAccount()
	set account to runTool("--add-account" & chooseProfile())
	display notification account with title APP_TITLE subtitle "Account added"
	return account
end addAccount

-- Google opens in the chosen Chrome profile: in another profile the account
-- does not show up and the login waits in vain. With one profile, no question.
on chooseProfile()
	set profileLines to paragraphs of runTool("--profiles")
	if (count of profileLines) < 2 then return ""
	set profileLabels to labelsOf(profileLines)
	activate
	set picked to choose from list profileLabels with title APP_TITLE with prompt "Which Chrome profile should Google open in? The account to add must be signed in (or able to sign in) there." default items {{item 1 of profileLabels}}
	if picked is false then error number -128
	return " --profile " & quoted form of folderOf(profileLines, item 1 of picked)
end chooseProfile

-- profileLines: "folder<TAB>label", as --profiles prints them
on labelsOf(profileLines)
	set profileLabels to {{}}
	repeat with i from 1 to count of profileLines
		set oneLine to item i of profileLines
		set end of profileLabels to text ((offset of tab in oneLine) + 1) thru -1 of oneLine
	end repeat
	return profileLabels
end labelsOf

on folderOf(profileLines, profileLabel)
	repeat with i from 1 to count of profileLines
		set oneLine to item i of profileLines
		if text ((offset of tab in oneLine) + 1) thru -1 of oneLine is profileLabel then
			return text 1 thru ((offset of tab in oneLine) - 1) of oneLine
		end if
	end repeat
	error "Profile not found: " & profileLabel
end folderOf

on chooseAccount()
	set accountList to paragraphs of runTool("--accounts")
	if (count of accountList) is 0 then return addAccount()
	if (count of accountList) is 1 then return item 1 of accountList
	-- The first one is the last used
	activate
	set picked to choose from list (accountList & {{ADD_OTHER}}) with title APP_TITLE with prompt "Which Google account should it open in?" default items {{item 1 of accountList}}
	if picked is false then error number -128
	if item 1 of picked is ADD_OTHER then return addAccount()
	return item 1 of picked
end chooseAccount

on runTool(args)
	try
		with timeout of 3600 seconds
			return do shell script quoted form of PY & " " & quoted form of SCRIPT_PATH & " " & args
		end timeout
	on error errText number errNum
		if errNum is -128 then error number -128
		activate
		display dialog errText with title APP_TITLE buttons {{"OK"}} default button 1 with icon stop
		error number -128
	end try
end runTool
"""


def build_app(target=TARGET, python=PYTHON, folder=BASE_DIR, register=True):
    script = os.path.join(folder, "open_in_sheets.py")
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "open-in-sheets.applescript")
        with open(source, "w", encoding="utf-8") as f:
            f.write(applescript_source(python, script))
        if os.path.exists(target):
            shutil.rmtree(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        subprocess.run(["osacompile", "-o", target, source], check=True)

    info_path = os.path.join(target, "Contents", "Info.plist")
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "LSUIElement": True,
        # Spreadsheets only: osacompile declares the applet for any file
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Spreadsheet",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": TYPES,
        }],
        "UTImportedTypeDeclarations": [{
            "UTTypeIdentifier": PARQUET_TYPE,
            "UTTypeDescription": "Apache Parquet",
            "UTTypeConformsTo": ["public.data"],
            "UTTypeTagSpecification": {"public.filename-extension": ["parquet"]},
        }],
    })
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)

    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", target],
                   check=True, capture_output=True)
    if register:
        subprocess.run([LSREGISTER, "-f", target], check=True)
    return target


INSTALLER = os.path.join(BASE_DIR, "Install.app")
INSTALLER_LOG = "~/Library/Logs/open-in-sheets-installer.log"


def installer_source():
    """Applet that opens the setup assistant (installer.py) without Terminal.

    No fixed paths: it looks for installer.py next to itself and for uv's
    Python under HOME, so it keeps working if the folder moves.
    """
    python = PYTHON.replace(os.path.expanduser("~"), "", 1)
    return f"""\
set here to do shell script "dirname " & quoted form of POSIX path of (path to me)
set py to (do shell script "echo $HOME") & {_quote(python)}
try
	do shell script "test -x " & quoted form of py
on error
	display dialog "uv's Python 3.14 is missing (" & py & "). See the README." with title "Install" buttons {{"OK"}} default button 1 with icon stop
	return
end try
do shell script "cd " & quoted form of here & " && " & quoted form of py & " installer.py > {INSTALLER_LOG} 2>&1 &"
"""


def build_installer(target=INSTALLER):
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "install.applescript")
        with open(source, "w", encoding="utf-8") as f:
            f.write(installer_source())
        if os.path.exists(target):
            shutil.rmtree(target)
        subprocess.run(["osacompile", "-o", target, source], check=True)
    info_path = os.path.join(target, "Contents", "Info.plist")
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleIdentifier": BUNDLE_ID + ".installer",
        "CFBundleName": "Install",
        "LSUIElement": True,  # it only launches the window and quits
    })
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", target],
                   check=True, capture_output=True)
    return target


def swift_source(app, types=TYPES):
    """Swift program that sets the app for each type and prints which app ended up.

    Uses NSWorkspace (macOS 12+): LSSetDefaultRoleHandlerForContentType
    returns OK but no longer changes anything (2026-10-23, see NOTES).
    """
    calls = "\n".join(f"setDefault({json.dumps(t)})" for t in types)
    return f"""\
import AppKit
import UniformTypeIdentifiers
let app = URL(fileURLWithPath: {json.dumps(app)})
func setDefault(_ id: String) {{
    guard let type = UTType(id) else {{ print(id + " -"); return }}
    let done = DispatchSemaphore(value: 0)
    NSWorkspace.shared.setDefaultApplication(at: app, toOpen: type) {{ _ in done.signal() }}
    _ = done.wait(timeout: .now() + 30)
    let current = NSWorkspace.shared.urlForApplication(toOpen: type)
    let currentId = current.flatMap {{ Bundle(url: $0)?.bundleIdentifier }} ?? "-"
    print(id + " " + currentId)
}}
{calls}
"""


def make_default(app=TARGET, types=TYPES):
    """Makes the app the default for each type.

    Returns {type: bundle id of the app that ended up as default}.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "set_default.swift")
        with open(path, "w", encoding="utf-8") as f:
            f.write(swift_source(app, types))
        r = subprocess.run(["swift", path], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr.strip()[-1000:], file=sys.stderr)
    return dict(line.split(" ", 1) for line in r.stdout.split("\n") if " " in line)


def missing_defaults(result):
    return [t for t in TYPES if result.get(t, "").lower() != BUNDLE_ID]


def report_default(result):
    missing = missing_defaults(result)
    if not missing:
        print(".csv, .xlsx and .parquet files now open with it.")
        return True
    print(f"It did not become the default for: {', '.join(missing)}.")
    print("By hand: right-click a file of that type > Get Info > Open with >")
    print(f"{APP_NAME} > Change All.")
    return False


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("macOS only.")
    print(f"Built {build_app()}")
    report_default(make_default())
