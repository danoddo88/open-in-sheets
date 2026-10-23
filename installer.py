"""Open in Sheets setup assistant: a window with Back and Next.

    double-click Install.app     (or: python installer.py)

Steps:
1. Welcome.
2. Prepare: installs pyarrow (for .parquet) in the global Python if missing,
   runs the tests (tests.py), builds ~/Applications/Open in Sheets.app and
   makes it the app for .csv, .xlsx and .parquet.
3. Google credential: one button per Google Cloud console page (already
   pointing at the project) and waits for client_secret_*.json to show up in
   Downloads; moves it to ~/.config/open-in-sheets/client.json.
4. Google accounts: add (chosen in the browser) and remove.
5. Done.
It can be opened as many times as needed: whatever is done shows as done.
"""

import glob
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import build_mac_app
import open_in_sheets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.expanduser("~/Downloads")
CONSOLE = "https://console.cloud.google.com"

# After creating the project, one button per page:
# (button, what to do there, page, what to leave on the clipboard)
CONSOLE_STEPS = [
    ("Enable the Drive API", "Click Enable.",
     "/apis/library/drive.googleapis.com", None),
    ("Consent screen",
     f"App name (copied when opening: {build_mac_app.APP_NAME}, paste it with "
     "Cmd+V), your email and audience Internal. With Internal there is no "
     "need to publish and the authorization does not expire (if an account "
     "is outside your organization, see the README).",
     "/auth/overview", build_mac_app.APP_NAME),
    ("Check Audience", "It should say Internal; if it says External, switch it to Internal.",
     "/auth/audience", None),
    ("Create the credential",
     "In Clients: Create client, type Desktop app, and Download JSON. "
     "The assistant picks it up from Downloads by itself.",
     "/auth/clients", None),
]


def console_url(page, project):
    return f"{CONSOLE}{page}?project={project}"


def valid_project_id(text):
    # Google's rule: 6 to 30 characters, lowercase letters, digits and
    # hyphens, starts with a letter and does not end with a hyphen
    return bool(re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", text))


def copy_to_clipboard(text):
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


def find_downloaded_client(folder=DOWNLOADS):
    """The newest client_secret_*.json in the folder, or None."""
    candidates = glob.glob(os.path.join(folder, "client_secret_*.json"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def install_client(source):
    os.makedirs(open_in_sheets.CONFIG_DIR, mode=0o700, exist_ok=True)
    target = os.path.join(open_in_sheets.CONFIG_DIR, "client.json")
    shutil.move(source, target)
    os.chmod(target, 0o600)
    open_in_sheets.read_client()  # checks that it is a Desktop app client
    return target


def prepare(report):
    """pyarrow, tests and app. `report(text)` tells how it is going."""
    try:
        import pyarrow  # noqa: F401
        report("pyarrow is already installed.")
    except ImportError:
        report("Installing pyarrow in the global Python...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user",
                            "--break-system-packages", "pyarrow"],
                           capture_output=True, text=True)
        if r.returncode:
            raise open_in_sheets.Error("Could not install pyarrow:\n" + r.stderr[-800:])

    report("Running the tests...")
    r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "tests.py")],
                       capture_output=True, text=True)
    if r.returncode:
        raise open_in_sheets.Error("The tests failed; nothing was installed.\n"
                                   + (r.stdout + r.stderr)[-1500:])
    report("Tests: " + r.stdout.strip().splitlines()[-1])

    report("Building the app...")
    report(f"Built {build_mac_app.build_app()}")
    missing = build_mac_app.missing_defaults(build_mac_app.make_default())
    if missing:
        report(f"It did not become the default for: {', '.join(missing)}. By "
               "hand: right-click a file of that type > Get Info > Open with > "
               f"{build_mac_app.APP_NAME} > Change All.")
    else:
        report(".csv, .xlsx and .parquet files now open with it.")


class Actions:
    """Everything the assistant does to the Mac or to Google; the tests
    replace it with a fake one."""
    prepare = staticmethod(prepare)
    open_url = staticmethod(open_in_sheets.open_in_browser)
    copy = staticmethod(copy_to_clipboard)
    find_downloaded_client = staticmethod(find_downloaded_client)
    install_client = staticmethod(install_client)
    accounts = staticmethod(open_in_sheets.accounts)
    remove_account = staticmethod(open_in_sheets.remove_account)
    profiles = staticmethod(open_in_sheets.chrome_profiles)

    @staticmethod
    def client_installed():
        return os.path.exists(os.path.join(open_in_sheets.CONFIG_DIR, "client.json"))

    @staticmethod
    def add_account(profile=None):
        return open_in_sheets.login(profile=profile)


class Assistant:
    PAGES = ["welcome", "prepare", "credential", "accounts", "done"]
    TITLES = {
        "welcome": "Open in Sheets",
        "prepare": "Prepare the Mac",
        "credential": "Google credential",
        "accounts": "Google accounts",
        "done": "Done",
    }
    TEXT_WIDTH = 540

    def __init__(self, root, actions=None):
        self.root = root
        self.act = actions or Actions()
        self.queue = queue.Queue()
        self.busy = False
        self.prepared = False
        self.i = 0
        self.project = tk.StringVar()
        self.project.trace_add("write", lambda *_: self._project_changed())

        root.title("Install Open in Sheets")
        root.geometry("620x540")
        root.minsize(580, 480)
        self.title = ttk.Label(root, font=("Helvetica", 20, "bold"))
        self.title.pack(anchor="w", padx=22, pady=(20, 8))
        self.body = ttk.Frame(root)
        self.body.pack(fill="both", expand=True, padx=22)
        footer = ttk.Frame(root)
        footer.pack(fill="x", padx=22, pady=16)
        self.step_label = ttk.Label(footer, foreground="gray")
        self.step_label.pack(side="left")
        self.next_button = ttk.Button(footer, text="Next", command=self.next)
        self.next_button.pack(side="right")
        self.back_button = ttk.Button(footer, text="Back", command=self.back)
        self.back_button.pack(side="right", padx=8)

        self.show()
        self._poll_queue()

    # ------------------------------------------------------------ navigation

    @property
    def page(self):
        return self.PAGES[self.i]

    def can_continue(self):
        if self.busy:
            return False
        if self.page == "prepare":
            return self.prepared
        if self.page == "credential":
            return self.act.client_installed()
        if self.page == "accounts":
            return bool(self.act.accounts())
        return True

    def update_buttons(self):
        last = self.i == len(self.PAGES) - 1
        self.next_button.config(text="Finish" if last else "Next",
                                state="normal" if self.can_continue() else "disabled")
        self.back_button.config(state="normal" if self.i > 0 and not self.busy else "disabled")
        self.step_label.config(text=f"Step {self.i + 1} of {len(self.PAGES)}")

    def next(self):
        if not self.can_continue():
            return
        if self.i == len(self.PAGES) - 1:
            self.root.destroy()
            return
        self.i += 1
        self.show()

    def back(self):
        if self.i > 0 and not self.busy:
            self.i -= 1
            self.show()

    def show(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.title.config(text=self.TITLES[self.page])
        getattr(self, "_page_" + self.page)()
        self.update_buttons()

    def _text(self, text, **kw):
        label = ttk.Label(self.body, text=text, wraplength=self.TEXT_WIDTH,
                          justify="left", **kw)
        label.pack(anchor="w", pady=(0, 10))
        return label

    # ------------------------------------------------------ background work
    # Slow work (tests, waiting for the browser) runs in a thread; the window
    # is only touched from the main thread, reading the queue (Tk must only
    # be used from the thread that created it).

    def in_background(self, work, when_done):
        self.busy = True
        self.update_buttons()

        def run():
            try:
                self.queue.put(("done", when_done, work(), None))
            except Exception as e:  # shown in the window
                self.queue.put(("done", when_done, None, e))
        threading.Thread(target=run, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, *data = self.queue.get_nowait()
                if kind == "report":
                    self._log(data[0])
                else:
                    when_done, result, error = data
                    self.busy = False
                    when_done(result, error)
                    self.update_buttons()
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_queue)

    # ---------------------------------------------------------------- pages

    def _page_welcome(self):
        self._text("With this, double-clicking a .csv, .xlsx or .parquet file "
                   "uploads it to your Google Drive converted to Google Sheets "
                   "and opens it in the browser.")
        self._text("The assistant prepares the Mac, guides you through creating "
                   "the Google credential (only once, a few minutes) and adds "
                   "your accounts.")
        self._text("If you installed before, whatever is done shows as done.",
                   foreground="gray")

    def _page_prepare(self):
        self._text("Installs what is missing, runs the tests and builds the "
                   f"{build_mac_app.APP_NAME} app.")
        self.log = tk.Text(self.body, height=14, wrap="word", state="disabled",
                           relief="flat", background="#f4f4f4")
        self.log.pack(fill="both", expand=True)
        self.retry_button = ttk.Button(self.body, text="Retry", command=self._prepare)
        if self.prepared:
            self._log("Already prepared.")
        elif not self.busy:
            self._prepare()

    def _log(self, text):
        if self.page != "prepare" or not self.log.winfo_exists():
            return
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _prepare(self):
        self.retry_button.pack_forget()
        report = lambda text: self.queue.put(("report", text))  # noqa: E731
        self.in_background(lambda: self.act.prepare(report), self._prepared)

    def _prepared(self, _, error):
        if error:
            self._log(f"\nError: {error}")
            if self.page == "prepare":
                self.retry_button.pack(anchor="w", pady=8)
            return
        self.prepared = True
        self._log("\nDone.")

    def _page_credential(self):
        if self.act.client_installed():
            self._text("The Google credential is already installed.")
            self._text("If it ever needs to change, delete "
                       "~/.config/open-in-sheets/client.json and come back to this step.",
                       foreground="gray")
            return
        self._text("This is done once in the Google Cloud console. Each button "
                   "opens the right page; Google's button names may vary a bit.")

        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(0, 4))
        ttk.Button(row, text="1. Create project",
                   command=lambda: self.act.open_url(CONSOLE + "/projectcreate")
                   ).pack(side="left")
        ttk.Label(row, text="  Project ID:").pack(side="left")
        ttk.Entry(row, textvariable=self.project, width=26).pack(side="left", padx=4)
        self.id_hint = ttk.Label(self.body, foreground="gray")
        self.id_hint.pack(anchor="w", pady=(0, 8))

        self.console_buttons = []
        for n, (label, text, page, to_copy) in enumerate(CONSOLE_STEPS, 2):
            row = ttk.Frame(self.body)
            row.pack(fill="x", pady=3)
            b = ttk.Button(row, text=f"{n}. {label}", width=26,
                           command=lambda p=page, c=to_copy: self._open_step(p, c))
            b.pack(side="left", anchor="n")
            ttk.Label(row, text=text, wraplength=self.TEXT_WIDTH - 230,
                      justify="left").pack(side="left", padx=8)
            self.console_buttons.append(b)

        self.json_status = ttk.Label(self.body, text="Waiting for the JSON in Downloads...",
                                     foreground="gray")
        self.json_status.pack(anchor="w", pady=(12, 2))
        ttk.Button(self.body, text="Choose the JSON by hand...",
                   command=self._choose_json).pack(anchor="w")
        self._project_changed()
        self._look_for_json()

    def _project_changed(self):
        if self.page != "credential" or not hasattr(self, "id_hint") \
                or not self.id_hint.winfo_exists():
            return
        valid = valid_project_id(self.project.get().strip())
        for b in self.console_buttons:
            b.config(state="normal" if valid else "disabled")
        if not self.project.get():
            self.id_hint.config(text="Paste the ID once you create it: the buttons "
                                     "below open the pages in that project.")
        elif not valid:
            self.id_hint.config(text="That does not look like a project ID (lowercase "
                                     "letters, digits and hyphens; it shows under the name).")
        else:
            self.id_hint.config(text="")

    def _open_step(self, page, to_copy):
        if to_copy:
            self.act.copy(to_copy)
        self.act.open_url(console_url(page, self.project.get().strip()))

    def _install_json(self, path):
        try:
            self.act.install_client(path)
        except (open_in_sheets.Error, OSError, ValueError, KeyError) as e:
            messagebox.showerror("Credential", f"That file does not work: {e}",
                                 parent=self.root)
            return
        self.show()

    def _look_for_json(self):
        if self.page != "credential" or self.act.client_installed():
            return
        found = self.act.find_downloaded_client()
        if found:
            # one more second, in case the browser is still writing it
            self.root.after(1000, lambda: self._install_json(found))
        else:
            self.root.after(2000, self._look_for_json)

    def _choose_json(self):
        path = filedialog.askopenfilename(parent=self.root, initialdir=DOWNLOADS,
                                          filetypes=[("JSON", "*.json")])
        if path:
            self._install_json(path)

    def _page_accounts(self):
        self._text("The accounts the spreadsheets can open in. With more than "
                   "one, double-clicking asks which.")
        self.account_list = tk.Listbox(self.body, height=7, activestyle="none")
        self.account_list.pack(fill="x")
        for account in self.act.accounts():
            self.account_list.insert("end", account)
        ttk.Button(self.body, text="Remove selected",
                   command=self._remove_account).pack(anchor="w", pady=(6, 14))

        # Google opens in the chosen Chrome profile: in another profile the
        # account does not show up and the login waits in vain (2026-10-23)
        self.profiles = self.act.profiles()
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Add account...", command=self.add_account).pack(side="left")
        if self.profiles:
            ttk.Label(row, text="  in Chrome profile:").pack(side="left")
            self.chosen_profile = ttk.Combobox(
                row, state="readonly", width=38,
                values=[label for _, label in self.profiles])
            self.chosen_profile.current(0)
            self.chosen_profile.pack(side="left", padx=4)
        self.account_status = ttk.Label(self.body, foreground="gray",
                                        wraplength=self.TEXT_WIDTH, justify="left")
        self.account_status.pack(anchor="w")

    def add_account(self):
        if self.busy:
            return
        profile = None
        if self.profiles:
            profile = self.profiles[self.chosen_profile.current()][0]
        self.account_status.config(text="Chrome opened: choose the account and accept. "
                                        "This window waits up to 5 minutes.")
        self.in_background(lambda: self.act.add_account(profile), self._account_added)

    def _account_added(self, account, error):
        if self.page != "accounts":
            return
        if error:
            self.account_status.config(text="")
            messagebox.showerror("Accounts", str(error), parent=self.root)
            return
        self.show()
        self.account_status.config(text=f"Added {account}.")

    def _remove_account(self):
        selected = self.account_list.curselection()
        if not selected:
            return
        account = self.account_list.get(selected[0])
        if messagebox.askyesno("Accounts", f"Remove {account}?", parent=self.root):
            self.act.remove_account(account)
            self.show()

    def _page_done(self):
        self._text("Double-clicking a .csv, .xlsx or .parquet file opens it in Google Sheets.")
        self._text("If the file was already uploaded, it asks whether to open "
                   "the uploaded one, overwrite it or open a copy.")
        self._text(f"To add or remove accounts later: open {build_mac_app.APP_NAME} "
                   "by itself, without a file (it is in your user's Applications "
                   "folder).", foreground="gray")


def main():
    if sys.platform != "darwin":
        sys.exit("macOS only.")
    root = tk.Tk()
    Assistant(root)
    # Launched from Install.app the window can end up behind others: raise it
    root.lift()
    root.attributes("-topmost", True)
    root.after(600, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
