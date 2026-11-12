import json
import os
import re
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk


def executable(name, extras=()):
    # Prefer a path that works when nix run replaces the command path.
    # On NixOS the setuid pkexec lives in /run/wrappers/bin, not the Nix store.
    for candidate in extras:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name)


def backend(*args, root=True):
    program = executable("apparmor-gui-backend")
    if not program:
        raise RuntimeError("apparmor-gui-backend is not installed")
    if root:
        pkexec = executable("pkexec", ("/run/wrappers/bin/pkexec", "/usr/bin/pkexec"))
        if not pkexec:
            raise RuntimeError("pkexec was not found. Polkit provides it.")
        command = [pkexec, program, *args]
    else:
        # Services, applications, and installed profiles can be read as the
        # person who opened the window. A password is only needed to change them.
        command = [program, *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "The request was canceled.").strip()
        raise RuntimeError(message)
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="AppArmor")
        self.set_default_size(1040, 720)
        self.profile_names = []
        self.service_rows = []
        self.app_rows = []
        self.learn_proc = None

        self.banner = Gtk.Label(xalign=0, wrap=True)
        self.banner.add_css_class("notice")
        self.banner.set_visible(False)
        self.banner.set_margin_top(8)
        self.banner.set_margin_start(12)
        self.banner.set_margin_end(12)
        self.profiles = self.list_box()
        self.services = self.list_box()
        self.apps = self.list_box()
        self.log = self.text_view()
        self.unconfined = self.text_view()
        self.enabled = Gtk.Label(xalign=0, wrap=True)
        self.search = Gtk.SearchEntry(placeholder_text="Filter services and applications")
        self.search.connect("search-changed", lambda *_: self.fill_machine())
        self.path_entry = Gtk.Entry(placeholder_text="Program name or /path/to/program")
        self.path_entry.set_hexpand(True)
        self.path_note = Gtk.Label(xalign=0, wrap=True)
        self.path_note.add_css_class("dim-label")
        self.path_entry.connect("changed", self.on_path_changed)
        self.exec_profile = Gtk.DropDown.new_from_strings([])
        self.exec_command = Gtk.Entry(placeholder_text="command to run under the profile")
        self.exec_command.set_hexpand(True)
        self.save_dir = Gtk.Label(xalign=0, wrap=True)
        self.save_dir.set_hexpand(True)
        self.save_dir.set_text("Saved profiles go in your home folder.")

        header = Gtk.HeaderBar()
        stack = Gtk.Stack()
        switcher = Gtk.StackSwitcher(stack=stack)
        header.set_title_widget(switcher)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.reload(clear=True))
        header.pack_start(refresh)

        stack.add_titled(self.machine_page(), "machine", "Machine")
        stack.add_titled(self.profiles_page(), "profiles", "Profiles")
        stack.add_titled(self.log_page(), "log", "Log")
        stack.add_titled(self.commands_page(), "commands", "Commands")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        box.append(stack)
        stack.set_vexpand(True)
        self.set_titlebar(header)
        self.set_child(box)
        self.install_style()
        self.reload(clear=True)

    def install_style(self):
        # These class names are drawn by this program. The session theme is left alone.
        provider = Gtk.CssProvider()
        provider.load_from_string(
            ".title-1 { font-size: 20px; font-weight: 700; }"
            ".title-2 { font-size: 16px; font-weight: 700; }"
            ".dim-label { opacity: 0.72; }"
            ".notice { padding: 8px 12px; }"
        )
        display = self.get_display()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def action_row(self, title, subtitle=""):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_margin_top(8)
        row.set_margin_bottom(8)
        row.set_margin_start(12)
        row.set_margin_end(12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        text.append(Gtk.Label(label=title, xalign=0, wrap=True))
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0, wrap=True)
            sub.add_css_class("dim-label")
            text.append(sub)
        row.append(text)
        return row

    def text_view(self):
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.set_vexpand(True)
        return view

    def list_box(self):
        box = Gtk.ListBox()
        box.add_css_class("rows")
        box.set_selection_mode(Gtk.SelectionMode.NONE)
        return box

    def machine_page(self):
        watch = Gtk.Button(label="Watch")
        watch.connect("clicked", self.on_watch_path)
        path_row = Gtk.Box(spacing=8)
        path_row.append(self.path_entry)
        path_row.append(watch)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.page_box([
            self.heading(
                "This machine",
                "Running services and installed applications from this machine. Profiles already loaded by AppArmor are on the Profiles page. Flatpak apps stay in the Flatpak sandbox.",
            ),
            path_row,
            self.path_note,
            self.search,
            Gtk.Label(label="Running services", xalign=0, css_classes=["title-2"]),
            self.services,
            Gtk.Label(label="Installed applications", xalign=0, css_classes=["title-2"]),
            self.apps,
        ]))
        return scroll

    def profiles_page(self):
        choose = Gtk.Button(label="Choose folder")
        choose.connect("clicked", self.on_choose_folder)
        folder_row = Gtk.Box(spacing=8)
        folder_row.append(self.save_dir)
        folder_row.append(choose)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.page_box([
            self.heading(
                "Profiles",
                "These are the profiles on this machine, including ones NixOS installed and ones loaded now. Complain, enforce, disable, and audit apply immediately. A profile this window saves is written to the folder below. The usual folder is in your home directory.",
            ),
            folder_row,
            self.profiles,
        ]))
        self.profiles.set_vexpand(True)
        return scroll

    def log_page(self):
        learn = Gtk.Button(label="Learn from the log")
        learn.connect("clicked", lambda *_: self.start_logprof())
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.page_box([
            self.heading(
                "Log",
                "Kernel lines from this boot, passed through aa-decode. ALLOWED means complain mode let it through. Learn from the log is aa-logprof: it asks before each new rule and saves the answer on this machine.",
            ),
            learn,
            self.log,
        ]))
        return scroll

    def commands_page(self):
        catalog = self.list_box()
        actions = [
            ("aa-status", "List profiles and confined processes.", "Show status", self.on_status),
            ("aa-enabled", "Report whether the kernel is enforcing AppArmor policy.", None, None),
            ("aa-enforce", "Block what a profile does not allow. Use Enforce on a profile.", None, None),
            ("aa-complain", "Log what a profile does not allow, and let it through. Use Complain or Watch.", None, None),
            ("aa-disable", "Unload a profile. Use Disable on a profile.", None, None),
            ("aa-audit", "Log allowed actions as well as denials. Use Audit on a profile.", None, None),
            ("aa-autodep", "Write a starting profile. Watch does this without following the program into an old Nix store path.", None, None),
            ("aa-genprof", "Watch a program, use it, then learn from the log. aa-genprof itself follows Nix store links, so the two steps here are the durable version.", "Learn from the log", self.start_logprof),
            ("aa-logprof", "Turn log lines into rules, one question at a time.", "Learn from the log", self.start_logprof),
            ("aa-cleanprof", "Remove duplicate rules. Use Clean on a profile.", None, None),
            ("aa-decode", "Turn hex-encoded paths in the log back into text. The log page already does this.", None, None),
            ("aa-easyprof", "Not installed. Watch writes the starting profile instead.", None, None),
            ("aa-mergeprof", "Bring a profile file into this machine.", "Import profile file", self.on_import),
            ("aa-notify", "Not used. It needs Tk, which is not installed. The log page is the notifier.", None, None),
            ("aa-remove-unknown", "Unload profiles that are not saved, then load the NixOS profiles and the ones this window saved.", "Remove unknown", self.on_remove_unknown),
            ("aa-teardown", "Unload every profile until Reload or the next rebuild. Saved profiles are not deleted.", "Unload all", self.on_teardown),
            ("apparmor_parser", "Load the NixOS profiles and the profiles saved on this machine into the kernel again.", "Reload profiles", self.on_reload),
            ("aa-features-abi", "Show the AppArmor feature set of this kernel.", "Show features", self.on_features),
            ("aa-load", "Load a precompiled policy cache. This machine loads text profiles with apparmor_parser instead.", None, None),
            ("aa-unconfined", "List network processes that have no profile. Shown below.", None, None),
            ("aa-exec", "Run a command inside a profile. Use the row below. It runs as you, not as root.", None, None),
        ]
        for title, subtitle, label, callback in actions:
            row = self.action_row(title, subtitle)
            if label and callback:
                button = Gtk.Button(label=label)
                button.set_valign(Gtk.Align.CENTER)
                button.connect("clicked", lambda *_b, cb=callback: cb())
                row.append(button)
            catalog.append(row)

        run = Gtk.Button(label="Run")
        run.connect("clicked", self.on_exec)
        run_row = Gtk.Box(spacing=8)
        run_row.append(self.exec_profile)
        run_row.append(self.exec_command)
        run_row.append(run)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.page_box([
            self.heading("Commands", "Every AppArmor command on this machine is listed here. Buttons run the command. The ones without a button are the profile, machine, or log controls named in the description."),
            self.enabled,
            catalog,
            Gtk.Label(label="aa-exec", xalign=0, css_classes=["title-2"]),
            run_row,
            Gtk.Label(label="aa-unconfined", xalign=0, css_classes=["title-2"]),
            self.unconfined,
        ]))
        return scroll

    def heading(self, title, description):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("title-1")
        note = Gtk.Label(label=description, xalign=0, wrap=True)
        note.add_css_class("dim-label")
        box.append(label)
        box.append(note)
        return box

    def page_box(self, children):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(18)
        page.set_margin_end(18)
        for child in children:
            page.append(child)
        return page

    def show_error(self, message):
        self.banner.set_text(message.splitlines()[0][:240] if message else "The command failed.")
        self.banner.set_visible(True)

    def note(self, message):
        self.banner.set_text(message)
        self.banner.set_visible(True)

    def reload(self, clear=False):
        if clear:
            self.banner.set_visible(False)
        try:
            data = backend("overview", root=False)
        except (RuntimeError, json.JSONDecodeError, OSError) as exc:
            self.show_error(str(exc))
            return
        state = "AppArmor is enabled." if data.get("enabled") else "AppArmor is not enabled."
        self.enabled.set_text(state + " aa-enabled checks the same thing.")
        self.service_rows = data.get("services", [])
        self.app_rows = data.get("apps", [])
        self.fill_profiles(data.get("profiles", []))
        self.fill_machine()
        self.log.get_buffer().set_text("\n".join(data.get("log", [])) or "No AppArmor lines this boot.")
        count = data.get("unconfined_count", 0)
        lines = data.get("unconfined", [])
        extra = "" if count <= len(lines) else f"\n… {count - len(lines)} more"
        self.unconfined.get_buffer().set_text(("\n".join(lines) + extra) if lines else "No unconfined network processes reported.")
        self.profile_names = [item["name"] for item in data.get("profiles", [])]
        self.exec_profile.set_model(Gtk.StringList.new(self.profile_names or [" "]))
        if data.get("state"):
            self.save_dir.set_text("Saved profiles: " + data["state"])

    def fill_profiles(self, profiles):
        self.clear(self.profiles)
        if not profiles:
            self.profiles.append(self.action_row("No profiles loaded"))
            return
        for item in profiles:
            bits = [item["mode"]]
            if item.get("audit"):
                bits.append("audit")
            if item.get("policy"):
                bits.append(item["policy"])
            managed = item.get("managed")
            if managed == "gui":
                bits.append("saved in the chosen folder")
            elif managed == "override":
                bits.append("mode saved in the chosen folder")
            elif managed == "system":
                bits.append("NixOS profile")
            elif not item.get("saved"):
                bits.append("loaded by hand")
            row = self.action_row(item["name"], " · ".join(bits))
            row.append(self.mode_buttons(item))
            self.profiles.append(row)

    def fill_machine(self):
        query = self.search.get_text().strip().lower()
        self.fill_targets(self.services, self.service_rows, "id", query)
        self.fill_targets(self.apps, self.app_rows, "name", query)

    def fill_targets(self, box, rows, title_key, query):
        self.clear(box)
        shown = 0
        for item in rows:
            blob = " ".join(str(item.get(key, "")) for key in (title_key, "exec", "scope", "kind", "profile")).lower()
            if query and query not in blob:
                continue
            shown += 1
            if item.get("kind") == "flatpak":
                state = "Flatpak sandbox"
            elif item.get("profile"):
                state = f"{item.get('profile')} ({item.get('mode') or 'saved'})"
            else:
                state = "not confined"
            scope = item.get("scope")
            title = item[title_key] if not scope else f"{item[title_key]}  ·  {scope}"
            row = self.action_row(title, f"{item.get('exec', '')}  ·  {state}")
            if item.get("kind") == "flatpak":
                pass
            elif item.get("profile") and item.get("mode"):
                row.append(self.small_modes(item["profile"], item["mode"]))
            elif item.get("exec"):
                button = Gtk.Button(label="Watch")
                button.set_valign(Gtk.Align.CENTER)
                button.connect("clicked", self.on_create, item["exec"])
                row.append(button)
            box.append(row)
        if shown == 0:
            box.append(self.action_row("Nothing matches"))

    def small_modes(self, name, current):
        box = Gtk.Box(spacing=6)
        for mode in ("complain", "enforce", "disable"):
            button = Gtk.Button(label=mode.capitalize())
            button.set_valign(Gtk.Align.CENTER)
            button.set_sensitive(current != mode)
            button.connect("clicked", self.on_mode, name, mode)
            box.append(button)
        return box

    def mode_buttons(self, item):
        box = Gtk.Box(spacing=6)
        name = item["name"]
        for mode in ("complain", "enforce", "disable"):
            button = Gtk.Button(label=mode.capitalize())
            button.set_valign(Gtk.Align.CENTER)
            button.set_sensitive(item["mode"] != mode and (item.get("saved") or mode == "disable"))
            button.connect("clicked", self.on_mode, name, mode)
            box.append(button)
        more = Gtk.MenuButton(label="More")
        more.set_valign(Gtk.Align.CENTER)
        pop = Gtk.Popover()
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        actions.set_margin_top(8)
        actions.set_margin_bottom(8)
        actions.set_margin_start(8)
        actions.set_margin_end(8)
        audit_label = "Audit off" if item.get("audit") else "Audit on"
        managed = item.get("managed")
        if managed == "system":
            remove_label = "About this NixOS profile"
            remove_heading = f"{name} comes from NixOS"
            remove_body = "This window does not edit your NixOS configuration. Disable unloads it until the next rebuild. Complain or Enforce saves that mode on this machine and applies it after the NixOS profile."
            remove_ok = "OK"
            remove_call = None
        elif not item.get("saved"):
            remove_label = "Loaded by hand"
            remove_heading = f"{name} is not saved"
            remove_body = "This profile was loaded by hand. Disable unloads it. It is not kept after a rebuild."
            remove_ok = "OK"
            remove_call = None
        elif managed == "override":
            remove_label = "Clear saved mode"
            remove_heading = f"Clear the saved mode for {name}?"
            remove_body = "The mode saved by this window is removed. The NixOS profile comes back on reload."
            remove_ok = "Clear"
            remove_call = ["forget", name]
        else:
            remove_label = "Remove saved profile"
            remove_heading = f"Remove {name}?"
            remove_body = "This unloads the profile and deletes the copy this window saved. A NixOS profile with the same name comes back on reload."
            remove_ok = "Remove"
            remove_call = ["forget", name]
        for label, callback in (
            (audit_label, lambda n=name, on=not item.get("audit"): self.on_audit(n, on)),
            ("Clean duplicate rules", lambda n=name: self.confirm(
                "Clean this profile?",
                "aa-cleanprof removes duplicate rules and saves the profile on this machine. A NixOS profile becomes a copy kept by this window. A Nix store path in that copy can go stale after a system update.",
                "Clean",
                lambda: self.call(["clean", n], f"Cleaned {n}."),
            )),
            ("View profile", lambda n=name: self.on_view(n)),
            (remove_label, lambda n=name, heading=remove_heading, body=remove_body, ok=remove_ok, call=remove_call: self.confirm(
                heading,
                body,
                ok,
                (lambda: self.call(call, f"Removed {n}.")) if call else (lambda: None),
                destructive=call is not None,
            )),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda *_b, cb=callback, pop=pop: (pop.popdown(), cb()))
            actions.append(button)
        pop.set_child(actions)
        more.set_popover(pop)
        box.append(more)
        return box

    def on_path_changed(self, *_args):
        path = self.resolved_path()
        typed = self.path_entry.get_text().strip()
        if not typed:
            self.path_note.set_text("")
        elif path.startswith("/") and os_path_exists(path):
            self.path_note.set_text(path)
        else:
            self.path_note.set_text("No program with that name is on the system path.")

    def resolved_path(self):
        text = self.path_entry.get_text().strip()
        if not text:
            return ""
        if text.startswith("/"):
            return text
        return shutil.which(text) or text

    def clear(self, box):
        while child := box.get_first_child():
            box.remove(child)

    def confirm(self, heading, body, ok, action, destructive=True):
        dialog = Gtk.AlertDialog()
        dialog.set_message(heading)
        dialog.set_detail(body)
        dialog.set_buttons(["Cancel", ok])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)
        dialog.set_modal(True)

        def chosen(_dialog, result):
            try:
                selected = dialog.choose_finish(result)
            except GLib.Error:
                return
            if selected == 1:
                action()

        dialog.choose(self, None, chosen)

    def on_mode(self, _button, name, mode):
        if mode == "complain":
            self.call(["set", name, mode], f"{name} is now complain. Saved on this machine.")
            return
        heading = f"Enforce {name}?" if mode == "enforce" else f"Disable {name}?"
        body = (
            "Enforce blocks anything the profile does not allow. A new profile only allows the program file itself, so enforce can stop the program. The choice is saved on this machine."
            if mode == "enforce"
            else "Disable unloads the profile now and keeps it unloaded after a rebuild."
        )
        self.confirm(heading, body, mode.capitalize(), lambda: self.call(["set", name, mode], f"{name} is now {mode}. Saved on this machine."))

    def on_audit(self, name, enabled):
        state = "on" if enabled else "off"
        self.confirm(
            f"Turn audit {state} for {name}?",
            "Audit logs actions the profile already allows. The journal can grow quickly. The profile text is saved on this machine.",
            "Audit " + state,
            lambda: self.call(["audit", name, state], f"Audit is {state} for {name}."),
        )

    def on_create(self, _button, path):
        self.call(["create", path], self.created_message)

    def created_message(self, data):
        if not isinstance(data, dict):
            return "Watching that program in complain mode."
        path = data.get("exec") or data.get("name")
        if data.get("existed"):
            text = f"{path} already had a profile. It is in complain mode."
        else:
            text = f"Watching {path} in complain mode. Saved on this machine and loaded again after each rebuild."
        if data.get("store"):
            text += " The path is in the Nix store, so after a system update you may need to watch it again."
        return text

    def on_watch_path(self, _button):
        path = self.resolved_path()
        if not path:
            self.show_error("Type a program name or a path first.")
            return
        if not path.startswith("/"):
            self.show_error("No program with that name is on the system path.")
            return
        self.on_create(None, path)

    def on_view(self, name):
        try:
            data = backend("show", name)
        except (RuntimeError, json.JSONDecodeError) as exc:
            self.show_error(str(exc))
            return
        self.text_dialog(data.get("name") or name, data.get("text") or "")

    def on_status(self):
        self.show_command(["status"], "aa-status")

    def on_features(self):
        self.show_command(["features"], "aa-features-abi")

    def show_command(self, args, title):
        try:
            data = backend(*args)
        except (RuntimeError, json.JSONDecodeError) as exc:
            self.show_error(str(exc))
            return
        self.text_dialog(title, (data or {}).get("text") or "")

    def text_dialog(self, title, text):
        window = Gtk.Window(transient_for=self, modal=True, title=title, default_width=760, default_height=520)
        view = self.text_view()
        view.get_buffer().set_text(text)
        window.set_child(Gtk.ScrolledWindow(child=view))
        window.present()

    def on_choose_folder(self, *_args):
        dialog = Gtk.FileDialog(title="Choose where to save profiles")
        dialog.select_folder(self, None, self.folder_chosen)

    def folder_chosen(self, dialog, result):
        try:
            chosen = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = chosen.get_path()
        if path:
            self.call(["state-set", path], f"Profiles will be saved in {path}.")

    def on_import(self):
        dialog = Gtk.FileDialog(title="Import an AppArmor profile")
        dialog.open(self, None, self.import_chosen)

    def import_chosen(self, dialog, result):
        try:
            chosen = dialog.open_finish(result)
        except GLib.Error:
            return
        path = chosen.get_path()
        if path:
            self.call(["import-file", path], "Imported the profile in complain mode and saved it on this machine.")

    def on_reload(self):
        self.call(["reload"], "Reloaded the NixOS profiles and the profiles saved on this machine.")

    def on_remove_unknown(self):
        self.confirm(
            "Remove unknown profiles?",
            "This unloads profiles that are not saved, then loads the NixOS profiles and the ones this window saved.",
            "Remove",
            lambda: self.call(["remove-unknown"], "Removed unknown profiles and reloaded the saved ones."),
        )

    def on_teardown(self, *_args):
        self.confirm(
            "Unload every profile?",
            "This runs aa-teardown. Programs keep running with no profile until you press Reload profiles or rebuild. Saved profiles are not deleted.",
            "Unload",
            lambda: self.call(["teardown"], "All profiles were unloaded. Reload profiles puts the saved ones back."),
        )

    def on_exec(self, _button):
        if not self.profile_names:
            self.show_error("There is no profile to run under.")
            return
        selected = self.exec_profile.get_selected()
        if selected < 0 or selected >= len(self.profile_names):
            self.show_error("Choose a profile first.")
            return
        profile = self.profile_names[selected]
        command = self.exec_command.get_text().strip()
        if not command:
            self.show_error("Type a command to run.")
            return
        program = shutil.which("aa-exec")
        if not program:
            self.show_error("aa-exec is not installed.")
            return
        subprocess.Popen([program, "-p", profile, "--", "sh", "-c", command])
        self.note(f"Started under {profile}.")

    def call(self, args, success):
        self.banner.set_visible(False)
        try:
            data = backend(*args)
        except (RuntimeError, json.JSONDecodeError) as exc:
            self.show_error(str(exc))
            return
        self.reload()
        message = success(data) if callable(success) else success
        self.note(message)

    def start_logprof(self, *_args):
        if self.learn_proc and self.learn_proc.poll() is None:
            self.show_error("Log learning is already running.")
            return
        program = executable("apparmor-gui-backend")
        if not program:
            self.show_error("apparmor-gui-backend is not installed.")
            return
        run0 = executable("run0", ("/run/current-system/sw/bin/run0", "/usr/bin/run0"))
        if not run0:
            self.show_error("run0 was not found. It comes with systemd.")
            return
        self.learn_proc = subprocess.Popen(
            [run0, "--pipe", program, "logprof"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.learn_window()
        threading.Thread(target=self.read_logprof, daemon=True).start()

    def learn_window(self):
        self.learn = Gtk.Window(transient_for=self, modal=True, title="Learn from the log", default_width=820, default_height=640)
        self.learn.connect("close-request", self.stop_logprof)
        self.learn_text = self.text_view()
        self.learn_buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4)
        self.learn_options = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.learn_options.add_css_class("boxed-list")
        self.learn_entry = Gtk.Entry()
        self.learn_entry.set_hexpand(True)
        cancel = Gtk.Button(label="Stop")
        cancel.connect("clicked", lambda *_: self.stop_logprof())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(Gtk.Label(label="aa-logprof. Allow adds a rule. Deny adds a block. Glob widens a path. Finish saves. Abort throws this pass away.", xalign=0, wrap=True))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.learn_text)
        box.append(scroll)
        box.append(self.learn_options)
        box.append(self.learn_entry)
        box.append(self.learn_buttons)
        box.append(cancel)
        self.learn.set_child(box)
        self.learn_entry.set_visible(False)
        self.learn.present()
        self.append_learn("Reading the AppArmor log…")

    def append_learn(self, text):
        if not text:
            return
        buffer = self.learn_text.get_buffer()
        buffer.insert(buffer.get_end_iter(), text.rstrip() + "\n\n")

    def read_logprof(self):
        proc = self.learn_proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    GLib.idle_add(self.append_learn, line)
                    continue
                GLib.idle_add(self.on_learn_message, message)
        finally:
            err = ""
            if proc.stderr:
                err = proc.stderr.read() or ""
            code = proc.wait()
            GLib.idle_add(self.learn_finished, code, err)

    def on_learn_message(self, message):
        kind = message.get("dialog")
        if kind in ("info", "important"):
            self.append_learn(message.get("data") or "")
            return
        if kind == "apparmor-json-version":
            return
        if kind == "done":
            saved = message.get("saved") or []
            stale = message.get("stale") or []
            if message.get("code") == 0:
                text = "Saved: " + (", ".join(saved) if saved else "no profile text changed.")
                if stale:
                    text += " These profiles point at a Nix store path, so a system update may require watching them again: " + ", ".join(stale) + "."
                self.append_learn(text)
            elif message.get("error"):
                self.append_learn(message["error"])
            return
        self.clear(self.learn_buttons)
        self.clear(self.learn_options)
        self.learn_entry.set_visible(False)
        if kind == "promptuser":
            self.show_prompt(message)
        elif kind == "yesno":
            self.show_yesno(message, cancel=False)
        elif kind == "yesnocancel":
            self.show_yesno(message, cancel=True)
        elif kind == "getstring":
            self.show_string(message)
        elif kind == "getfile":
            self.show_file(message)
        elif kind == "changes":
            self.append_learn(message.get("header") or "Changes")
            self.append_learn(message.get("text") or "")
            self.add_learn_button("Continue", lambda: self.reply({"dialog": "changes", "response": "ok"}))
        else:
            self.append_learn(json.dumps(message, indent=2))
            self.add_learn_button("Continue", lambda: self.reply({"dialog": kind, "response": "ok", "response_key": "y", "selected": 0}))

    def show_prompt(self, message):
        lines = [message.get("title") or "", message.get("explanation") or ""]
        headers = message.get("headers") or {}
        if isinstance(headers, dict):
            lines.extend(f"{key}: {value}" for key, value in headers.items())
        self.append_learn("\n".join(line for line in lines if line))
        options = message.get("options") or []
        for option in options:
            option_row = Gtk.Label(label=str(option), xalign=0, wrap=True)
            option_row.set_margin_top(6)
            option_row.set_margin_bottom(6)
            option_row.set_margin_start(8)
            option_row.set_margin_end(8)
            self.learn_options.append(option_row)
        selected = message.get("selected") or 0
        if options:
            row = self.learn_options.get_row_at_index(selected)
            if row:
                self.learn_options.select_row(row)
        for label in message.get("menu_items") or []:
            key = hotkey(label)
            self.add_learn_button(label, lambda k=key: self.reply({
                "dialog": "promptuser",
                "response_key": k,
                "selected": self.selected_option(),
            }))

    def show_yesno(self, message, cancel):
        self.append_learn(message.get("text") or "")
        self.add_learn_button("Yes", lambda: self.reply({"dialog": "yesnocancel" if cancel else "yesno", "response_key": "y"}))
        self.add_learn_button("No", lambda: self.reply({"dialog": "yesnocancel" if cancel else "yesno", "response_key": "n"}))
        if cancel:
            self.add_learn_button("Cancel", lambda: self.reply({"dialog": "yesnocancel", "response_key": "c"}))

    def show_string(self, message):
        self.append_learn(message.get("text") or "")
        self.learn_entry.set_text(message.get("default") or "")
        self.learn_entry.set_visible(True)
        self.add_learn_button("OK", lambda: self.reply({"dialog": "getstring", "response": self.learn_entry.get_text()}))

    def show_file(self, message):
        self.append_learn(message.get("text") or "Choose a file")
        self.learn_entry.set_visible(True)
        self.add_learn_button("Choose file", self.choose_learn_file)
        self.add_learn_button("OK", lambda: self.reply({"dialog": "getfile", "response": self.learn_entry.get_text()}))

    def choose_learn_file(self):
        dialog = Gtk.FileDialog()
        dialog.open(self.learn, None, self.learn_file_chosen)

    def learn_file_chosen(self, dialog, result):
        try:
            chosen = dialog.open_finish(result)
        except GLib.Error:
            return
        if chosen.get_path():
            self.learn_entry.set_text(chosen.get_path())

    def selected_option(self):
        row = self.learn_options.get_selected_row()
        return row.get_index() if row else 0

    def add_learn_button(self, label, callback):
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda *_: callback())
        self.learn_buttons.append(button)

    def reply(self, payload):
        proc = self.learn_proc
        if not proc or not proc.stdin:
            return
        self.clear(self.learn_buttons)
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.append_learn(str(exc))

    def stop_logprof(self, *_args):
        proc = self.learn_proc
        if proc and proc.poll() is None:
            proc.terminate()
        return False

    def learn_finished(self, code, err):
        if code not in (0, None) and err.strip():
            self.append_learn(err.strip()[-2000:])
        self.append_learn("Closed.")
        self.reload()
        self.note("Finished learning from the log." if code == 0 else "Learning from the log stopped.")


def os_path_exists(path):
    import os
    return os.path.exists(path)


def hotkey(label):
    match = re.search(r"\(([^)]+)\)", label)
    if match:
        return match.group(1).lower()
    return label[:1].lower()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.nixos.AppArmorGui", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = Window(self)
        win.present()


if __name__ == "__main__":
    raise SystemExit(App().run())
