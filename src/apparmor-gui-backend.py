import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading

# Saved profiles live in the home directory of the person who opened the window,
# or in a folder that person chose. The NixOS module reads the registry below
# and loads those folders after apparmor.service. APPARMOR_GUI_STATE is only
# for a test.
REGISTRY = "/var/lib/apparmor-gui/directories"
CACHE = "/var/cache/apparmor/logprof"
MODES = ("complain", "enforce", "disable")
RESERVED = {"tunables", "abstractions", "abi"}


def check(cmd):
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise SystemExit((result.stdout or f"{cmd[0]} failed").strip())
    return result.stdout


def run_text(cmd):
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode, result.stdout


def caller():
    # pkexec sets PKEXEC_UID. run0 and sudo set SUDO_UID. Either one is the
    # person who opened the window, which is not a fixed account.
    for key in ("PKEXEC_UID", "SUDO_UID"):
        value = os.environ.get(key, "")
        if value.isdigit() and int(value) != 0:
            try:
                return pwd.getpwuid(int(value))
            except KeyError:
                pass
    if os.getuid() != 0:
        return pwd.getpwuid(os.getuid())
    code, out = run_text(["loginctl", "list-sessions", "--no-legend"])
    if code == 0:
        fallback = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3 or not parts[1].isdigit() or int(parts[1]) < 1000:
                continue
            try:
                record = pwd.getpwuid(int(parts[1]))
            except KeyError:
                continue
            if "active" in parts:
                return record
            fallback = fallback or record
        if fallback:
            return fallback
    return pwd.getpwuid(0)


def user_paths():
    cached = getattr(user_paths, "cached", None)
    if cached is not None:
        return cached
    user = caller()
    home = user.pw_dir
    name = user.pw_name
    app_dirs = [
        "/run/current-system/sw/share/applications",
        os.path.join(home, ".local/share/applications"),
        os.path.join(home, ".local/share/flatpak/exports/share/applications"),
        "/var/lib/flatpak/exports/share/applications",
        os.path.join(home, ".nix-profile/share/applications"),
        f"/etc/profiles/per-user/{name}/share/applications",
    ]
    bin_dirs = [
        "/run/current-system/sw/bin",
        "/run/current-system/sw/sbin",
        os.path.join(home, ".local/bin"),
        os.path.join(home, ".nix-profile/bin"),
        f"/etc/profiles/per-user/{name}/bin",
    ]
    app_dirs.extend(session_app_dirs(user))
    user_paths.cached = (user, app_dirs, bin_dirs)
    return user_paths.cached


def session_app_dirs(user):
    # The host session can add application directories beyond the standard ones.
    code, out = run_text(["systemctl", "--user", "-M", f"{user.pw_name}@", "show-environment"])
    found = []
    if code != 0:
        return found
    for line in out.splitlines():
        if line.startswith("XDG_DATA_DIRS=") or line.startswith("XDG_DATA_HOME="):
            value = line.split("=", 1)[1]
            for item in value.split(":"):
                if item:
                    found.append(os.path.join(item, "applications"))
    return found


def choice_file(user):
    return os.path.join(user.pw_dir, ".config", "apparmor-gui", "directory")


def default_state_dir(user):
    return os.path.join(user.pw_dir, ".local", "share", "apparmor-gui")


def read_choice(user):
    try:
        chosen = open(choice_file(user), encoding="utf-8").read().strip()
    except OSError:
        return None
    if chosen and os.path.isabs(chosen):
        return chosen
    return None


def state_root():
    forced = os.environ.get("APPARMOR_GUI_STATE")
    if forced:
        return forced
    user = caller()
    return read_choice(user) or default_state_dir(user)


def profile_dir():
    return os.path.join(state_root(), "profiles")


def override_dir():
    return os.path.join(state_root(), "overrides")


def give_to_user(path, user):
    try:
        os.chown(path, user.pw_uid, user.pw_gid)
    except OSError:
        pass


def register_state(path, previous=None):
    # Root records the folder so a rebuild can load it. The profiles stay in the folder.
    try:
        os.makedirs(os.path.dirname(REGISTRY), mode=0o755, exist_ok=True)
        lines = []
        if os.path.isfile(REGISTRY):
            lines = [line.strip() for line in open(REGISTRY, encoding="utf-8") if line.strip()]
        if previous and previous != path:
            lines = [line for line in lines if line != previous]
        if path not in lines:
            lines.append(path)
        write_text(REGISTRY, "".join(line + "\n" for line in lines))
    except OSError:
        return


def include_roots():
    roots = []
    path = "/etc/apparmor/parser.conf"
    if not os.path.isfile(path):
        return roots
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line.startswith("Include "):
            roots.append(line.split(None, 1)[1])
    return roots


def link_includes(directory):
    # One include directory does not contain every abstraction. Merge them so
    # aa-logprof and aa-cleanprof can resolve includes next to the profile.
    for name in ("tunables", "abstractions", "abi"):
        dest = os.path.join(directory, name)
        if os.path.islink(dest):
            os.unlink(dest)
        os.makedirs(dest, exist_ok=True)
        for root in include_roots():
            src = os.path.join(root, name)
            if not os.path.isdir(src):
                continue
            for entry in os.listdir(src):
                target = os.path.join(dest, entry)
                if os.path.lexists(target):
                    continue
                os.symlink(os.path.join(src, entry), target)


def ensure_state():
    root = state_root()
    os.makedirs(profile_dir(), mode=0o755, exist_ok=True)
    os.makedirs(override_dir(), mode=0o755, exist_ok=True)
    if os.environ.get("APPARMOR_GUI_STATE"):
        return root
    user = caller()
    for path in (root, profile_dir(), override_dir()):
        give_to_user(path, user)
    register_state(root)
    return root


def set_state(path):
    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        raise SystemExit("choose a folder that exists")
    if os.environ.get("APPARMOR_GUI_STATE"):
        raise SystemExit("APPARMOR_GUI_STATE is set")
    user = caller()
    previous = read_choice(user)
    config = os.path.dirname(choice_file(user))
    os.makedirs(config, mode=0o755, exist_ok=True)
    write_text(choice_file(user), os.path.abspath(path) + "\n")
    give_to_user(config, user)
    give_to_user(choice_file(user), user)
    os.chmod(choice_file(user), 0o644)
    chosen = os.path.abspath(path)
    os.makedirs(os.path.join(chosen, "profiles"), mode=0o755, exist_ok=True)
    os.makedirs(os.path.join(chosen, "overrides"), mode=0o755, exist_ok=True)
    give_to_user(os.path.join(chosen, "profiles"), user)
    give_to_user(os.path.join(chosen, "overrides"), user)
    register_state(chosen, previous)
    return {"state": chosen}


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def read_mode_file(path):
    try:
        mode = open(path, encoding="utf-8").read().strip()
    except OSError:
        return None
    return mode if mode in MODES else None


def installed_text(key):
    path = os.path.join("/etc/apparmor.d", key)
    if not os.path.isfile(path):
        return None
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None


def profile_name(text):
    match = re.search(r"(?m)^[ \t]*profile\s+(\S+)", text or "")
    if match:
        return match.group(1).strip('"')
    match = re.search(r"(?m)^[ \t]*(/\S+)\s+(?:flags=\([^)]*\)\s*)?\{", text or "")
    if match:
        return match.group(1)
    return None


def attachment(text):
    match = re.search(r"(?m)^[ \t]*profile\s+\S+\s+(\S+)", text or "")
    if match and match.group(1).startswith("/"):
        return match.group(1)
    match = re.search(r"(?m)^[ \t]*(/\S+)\s+(?:flags=\([^)]*\)\s*)?\{", text or "")
    if match:
        return match.group(1)
    return None


def audit_on(text):
    match = re.search(r"flags=\(([^)]*)\)", text or "")
    if not match:
        return False
    return "audit" in [part.strip() for part in match.group(1).split(",")]


def set_audit_flag(text, enabled):
    def replace(match):
        flags = [part.strip() for part in match.group(1).split(",") if part.strip() and part.strip() != "audit"]
        if enabled:
            flags.append("audit")
        if not flags:
            return ""
        return " flags=(" + ",".join(flags) + ")"

    new, count = re.subn(r"[ \t]flags=\(([^)]*)\)", replace, text, count=1)
    if count == 0 and enabled:
        new, count = re.subn(r"\{", "flags=(audit) {", text, count=1)
    if count != 1:
        raise SystemExit("could not find the profile flags")
    return new


def policy_record(key, text, mode, managed):
    return {
        "key": key,
        "mode": mode if mode in MODES else "complain",
        "source": text,
        "runtime": text,
        "name": profile_name(text) or key,
        "exec": attachment(text),
        "audit": audit_on(text),
        "managed": managed,
    }


def saved_roots():
    # The current folder is last so it wins when the same profile was saved twice.
    current = state_root()
    roots = []
    if not os.environ.get("APPARMOR_GUI_STATE"):
        try:
            for line in open(REGISTRY, encoding="utf-8"):
                line = line.strip()
                if line and line not in roots and line != current:
                    roots.append(line)
        except OSError:
            pass
    roots.append(current)
    return roots


def saved_policies():
    # gui: this window owns the profile text.
    # override: NixOS owns the text; this window only stores the mode.
    # system: the profile is installed and this window has not changed it.
    owned = {}
    overrides = {}
    for root in saved_roots():
        directory = os.path.join(root, "profiles")
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                if not name.endswith(".profile"):
                    continue
                key = name[: -len(".profile")]
                path = os.path.join(directory, name)
                try:
                    text = open(path, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                mode = read_mode_file(os.path.join(directory, key + ".mode")) or "complain"
                owned[key] = policy_record(key, text, mode, "gui")
        override_root = os.path.join(root, "overrides")
        if os.path.isdir(override_root):
            for name in os.listdir(override_root):
                if name.endswith(".mode"):
                    mode = read_mode_file(os.path.join(override_root, name))
                    if mode:
                        overrides[name[: -len(".mode")]] = mode
    items = list(owned.values())
    base = "/etc/apparmor.d"
    if os.path.isdir(base):
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if name in owned or name in RESERVED or not os.path.isfile(path):
                continue
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if not profile_name(text):
                continue
            override = overrides.get(name)
            items.append(policy_record(name, text, override or "complain", "override" if override else "system"))
    return items


def by_name():
    found = {}
    for item in saved_policies():
        current = found.get(item["name"])
        if current is None or item["managed"] == "gui" or (item["managed"] == "override" and current["managed"] == "system"):
            found[item["name"]] = item
    return found


def file_id(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def policy_files():
    found = {}
    for item in saved_policies():
        found[item["name"]] = item["key"]
    return found


def find_policy(name):
    found = by_name()
    if name in found:
        return found[name]
    for item in saved_policies():
        if item["key"] == name:
            return item
    return None


def live_modes():
    try:
        data = json.loads(check(["aa-status", "--json"]))
    except (SystemExit, json.JSONDecodeError):
        return {}
    raw = data.get("profiles", {})
    if not isinstance(raw, dict):
        return {}
    if raw and all(isinstance(value, str) for value in raw.values()):
        return dict(raw)
    modes = {}
    for mode in MODES:
        for name in raw.get(mode, []):
            modes[name] = mode
    return modes


def effective_mode(item):
    if item["managed"] in ("gui", "override"):
        return item["mode"]
    return live_modes().get(item["name"]) or item["mode"]


def profiles():
    try:
        data = json.loads(check(["aa-status", "--json"]))
    except (SystemExit, json.JSONDecodeError):
        data = {}
    raw = data.get("profiles", {}) if isinstance(data, dict) else {}
    saved = by_name()
    items = []
    if isinstance(raw, dict):
        pairs = list(raw.items()) if raw and all(isinstance(value, str) for value in raw.values()) else []
        if not pairs:
            for mode in MODES:
                for name in raw.get(mode, []):
                    pairs.append((name, mode))
    else:
        pairs = []
    files = policy_files()
    seen = set()
    for name, mode in pairs:
        seen.add(name)
        item = saved.get(name, {})
        managed = item.get("managed")
        if not managed and files.get(name):
            managed = "system"
        items.append({
            "name": name,
            "mode": mode,
            "policy": item.get("key") or files.get(name),
            "audit": bool(item.get("audit")),
            "exec": item.get("exec") or "",
            "saved": managed in ("gui", "override", "system"),
            "managed": managed,
        })
    for item in saved.values():
        if item["name"] in seen:
            continue
        items.append({
            "name": item["name"],
            "mode": "unloaded" if item["managed"] == "system" else item["mode"],
            "policy": item["key"],
            "audit": item["audit"],
            "exec": item.get("exec") or "",
            "saved": True,
            "managed": item["managed"],
        })
    items.sort(key=lambda item: item["name"])
    return items, data.get("processes", {})


def log_lines():
    try:
        out = check(["journalctl", "-b", "-g", "apparmor=", "--no-pager", "-n", "80", "-o", "cat"])
    except SystemExit:
        return []
    decoded = subprocess.run(["aa-decode"], input=out, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    text = decoded.stdout if decoded.returncode == 0 and decoded.stdout.strip() else out
    return [line for line in text.splitlines() if "apparmor=" in line]


def enabled():
    return subprocess.run(["aa-enabled", "-q"]).returncode == 0


def unconfined():
    code, out = run_text(["aa-unconfined", "--paranoid", "--short"])
    lines = [line for line in out.splitlines() if line.strip()] if code == 0 or out.strip() else []
    return lines[:80], len(lines)


def parse_units(text, scope):
    items = []
    current = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current.get("Id") and current.get("exec"):
                items.append({
                    "id": current["Id"],
                    "scope": scope,
                    "exec": current["exec"],
                    "pid": current.get("MainPID", "0"),
                })
            current = {}
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "ExecStart":
            match = re.search(r"path=([^ ;]+)", value)
            current["exec"] = match.group(1) if match else ""
        else:
            current[key] = value
    return items


def services():
    user, _, _ = user_paths()
    items = []
    commands = [
        ("system", ["systemctl", "show", "--type=service", "--state=running", "-p", "Id", "-p", "ExecStart", "-p", "MainPID"]),
        ("user", ["systemctl", "--user", "-M", f"{user.pw_name}@", "show", "--type=service", "--state=running", "-p", "Id", "-p", "ExecStart", "-p", "MainPID"]),
    ]
    for scope, cmd in commands:
        code, out = run_text(cmd)
        if code == 0:
            items.extend(parse_units(out, scope))
    items.sort(key=lambda item: (item["scope"], item["id"]))
    return items


def apps():
    _, app_dirs, _ = user_paths()
    items = []
    seen = set()
    for directory in app_dirs:
        for path in sorted(glob.glob(os.path.join(directory, "*.desktop"))):
            entry = parse_desktop(path)
            if not entry or entry["exec"] in seen:
                continue
            seen.add(entry["exec"])
            items.append(entry)
    items.sort(key=lambda item: item["name"].lower())
    return items


def parse_desktop(path):
    name = None
    exe = None
    hidden = False
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("Name=") and name is None:
            name = line.split("=", 1)[1].strip()
        elif line.startswith("Exec=") and exe is None:
            exe = line.split("=", 1)[1].strip()
        elif line.startswith("NoDisplay=true") or line.startswith("Hidden=true"):
            hidden = True
    if hidden or not name or not exe:
        return None
    try:
        tokens = [token for token in shlex.split(exe) if not token.startswith("%")]
    except ValueError:
        tokens = exe.split()
    while tokens and (tokens[0] == "env" or "=" in tokens[0]):
        tokens.pop(0)
    if not tokens:
        return None
    if tokens[0] == "flatpak" or tokens[0].endswith("/flatpak"):
        app_id = next((token for token in reversed(tokens[1:]) if not token.startswith("-") and "." in token), name)
        return {"name": name, "exec": app_id, "kind": "flatpak"}
    token = tokens[0]
    if not token.startswith("/"):
        token = which(token)
        if not token:
            return None
    if not os.path.isfile(token):
        return None
    return {"name": name, "exec": token, "kind": "app"}


def which(name):
    _, _, bin_dirs = user_paths()
    for directory in bin_dirs:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def index_processes(processes):
    by_pid = {}
    by_exe = {}
    if not isinstance(processes, dict):
        return by_pid, by_exe
    for exe, entries in processes.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("status") == "unconfined" and str(entry.get("pid")) in by_pid:
                continue
            by_pid[str(entry.get("pid"))] = entry
            by_exe[exe] = entry
            if os.path.exists(exe):
                by_exe[os.path.realpath(exe)] = entry
    return by_pid, by_exe


def match_profile(exec_path, pid, by_pid, by_exe, attached, attached_id):
    hit = by_pid.get(str(pid)) if pid and pid != "0" else None
    if not hit:
        hit = by_exe.get(exec_path)
        if not hit and exec_path and os.path.exists(exec_path):
            hit = by_exe.get(os.path.realpath(exec_path))
    if hit and hit.get("status") != "unconfined":
        return hit.get("profile"), hit.get("status")
    real = os.path.realpath(exec_path) if exec_path and os.path.exists(exec_path) else exec_path
    saved = attached.get(exec_path) or attached.get(real)
    if not saved and exec_path:
        saved = attached_id.get(file_id(exec_path))
    if saved:
        return saved["name"], saved["mode"]
    return None, None


def overview():
    loaded, processes = profiles()
    by_pid, by_exe = index_processes(processes)
    attached = {}
    attached_id = {}
    for item in saved_policies():
        if not item.get("exec"):
            continue
        attached[item["exec"]] = item
        if os.path.exists(item["exec"]):
            attached[os.path.realpath(item["exec"])] = item
            ident = file_id(item["exec"])
            if ident:
                attached_id[ident] = item
    service_rows = []
    for item in services():
        profile, mode = match_profile(item["exec"], item["pid"], by_pid, by_exe, attached, attached_id)
        item["profile"] = profile
        item["mode"] = mode
        service_rows.append(item)
    app_rows = []
    for item in apps():
        if item["kind"] == "flatpak":
            item["profile"] = None
            item["mode"] = None
        else:
            profile, mode = match_profile(item["exec"], None, by_pid, by_exe, attached, attached_id)
            item["profile"] = profile
            item["mode"] = mode
        app_rows.append(item)
    lines, count = unconfined()
    return {
        "enabled": enabled(),
        "profiles": loaded,
        "services": service_rows,
        "apps": app_rows,
        "unconfined": lines,
        "unconfined_count": count,
        "log": log_lines(),
        "state": state_root(),
    }


def load_text(text, mode):
    handle = tempfile.NamedTemporaryFile("w", delete=False)
    handle.write(text)
    if not text.endswith("\n"):
        handle.write("\n")
    handle.close()
    try:
        if mode == "disable":
            check(["apparmor_parser", "-R", handle.name])
        elif mode == "complain":
            check(["apparmor_parser", "-r", "-C", handle.name])
        else:
            check(["apparmor_parser", "-r", handle.name])
    finally:
        os.unlink(handle.name)


def write_owned(key, mode, body):
    ensure_state()
    if not body.endswith("\n"):
        body += "\n"
    write_text(os.path.join(profile_dir(), key + ".profile"), body)
    write_text(os.path.join(profile_dir(), key + ".mode"), mode + "\n")
    override = os.path.join(override_dir(), key + ".mode")
    if os.path.isfile(override):
        os.unlink(override)


def write_override(key, mode):
    ensure_state()
    write_text(os.path.join(override_dir(), key + ".mode"), mode + "\n")


def remove_saved(key):
    for path in (
        os.path.join(profile_dir(), key + ".profile"),
        os.path.join(profile_dir(), key + ".mode"),
        os.path.join(override_dir(), key + ".mode"),
    ):
        if os.path.isfile(path):
            os.unlink(path)


def reload_service():
    code, out = run_text(["systemctl", "reload", "apparmor"])
    if code != 0:
        raise SystemExit(out.strip() or "could not reload AppArmor")
    return True


def set_mode(name, mode):
    if mode not in MODES:
        raise SystemExit("mode must be enforce, complain, or disable")
    item = find_policy(name)
    if not item:
        if mode != "disable":
            raise SystemExit(f"{name} is not a saved profile. Disable unloads a profile that was loaded by hand.")
        load_text(unload_stub(name), "disable")
        return {"name": name, "mode": mode, "managed": None}
    if item["managed"] == "gui":
        load_text(item["runtime"], mode)
        write_owned(item["key"], mode, item["runtime"])
        managed = "gui"
    else:
        text = installed_text(item["key"]) or item["runtime"]
        load_text(text, mode)
        write_override(item["key"], mode)
        managed = "override"
    return {"name": item["name"], "mode": mode, "policy": item["key"], "managed": managed}


def unload_stub(name):
    if name.startswith("/"):
        return f"{name} flags=(complain) {{\n  /dev/null r,\n}}\n"
    return f"profile {name} {name} flags=(complain) {{\n  /dev/null r,\n}}\n"


def set_audit(name, state):
    if state not in ("on", "off"):
        raise SystemExit("audit must be on or off")
    item = find_policy(name)
    if not item:
        raise SystemExit(f"{name} is not a saved profile")
    enabled_flag = state == "on"
    body = set_audit_flag(item["runtime"], enabled_flag)
    mode = effective_mode(item)
    if mode != "disable":
        load_text(body, mode)
    write_owned(item["key"], mode, body)
    return {
        "name": item["name"],
        "audit": enabled_flag,
        "copied": item["managed"] != "gui",
        "store": "/nix/store/" in body,
    }


def stable_path(program):
    program = os.path.abspath(program)
    if not os.path.isfile(program):
        raise SystemExit("choose a program path that exists")
    if program.startswith("/run/current-system/sw/"):
        return program, False
    real = os.path.realpath(program)
    for base in ("bin", "sbin", "libexec"):
        root = os.path.join("/run/current-system/sw", base)
        if not os.path.isdir(root):
            continue
        for dirpath, _, names in os.walk(root):
            for filename in names:
                candidate = os.path.join(dirpath, filename)
                if os.path.islink(candidate) and os.path.realpath(candidate) == real:
                    return candidate, False
    return program, program.startswith("/nix/store/")


def policy_key(path):
    parts = [part for part in path.split("/") if part]
    if parts[:3] == ["run", "current-system", "sw"]:
        parts = parts[3:]
    elif len(parts) >= 3 and parts[:2] == ["nix", "store"]:
        parts = parts[3:]
    elif len(parts) >= 3 and parts[:2] == ["run", "wrappers"]:
        parts = parts[3:]
    key = ".".join(parts)
    key = re.sub(r"[^A-Za-z0-9._+-]", ".", key).strip(".")
    if key in RESERVED:
        key = key + ".profile"
    return (key or "profile")[:120]


def unique_key(key):
    candidate = key
    number = 2
    while candidate in RESERVED or os.path.exists(os.path.join(profile_dir(), candidate + ".profile")) or os.path.exists(os.path.join("/etc/apparmor.d", candidate)):
        candidate = f"{key}-{number}"
        number += 1
    return candidate


def existing_for_path(path):
    real = os.path.realpath(path) if os.path.exists(path) else path
    ident = file_id(path)
    for item in saved_policies():
        attach = item.get("exec")
        if not attach:
            continue
        attach_real = os.path.realpath(attach) if os.path.exists(attach) else attach
        if path in (attach, attach_real) or real in (attach, attach_real):
            return item
        if ident and ident == file_id(attach):
            return item
    return None


def create(program):
    if program.endswith("/flatpak") or os.path.basename(program) == "flatpak":
        raise SystemExit("Flatpak apps use the Flatpak sandbox. AppArmor would only wrap the flatpak command, not the app.")
    attach, in_store = stable_path(program)
    found = existing_for_path(attach)
    if found:
        result = set_mode(found["name"], "complain")
        result["existed"] = True
        result["store"] = in_store
        result["exec"] = attach
        return result
    name = os.path.basename(attach)
    if any(item["name"] == name for item in saved_policies()):
        name = policy_key(attach).replace(".", "-")
    key = unique_key(policy_key(attach))
    body = (
        "include <tunables/global>\n"
        f"profile {name} {attach} flags=(attach_disconnected) {{\n"
        "  include <abstractions/base>\n"
        "  include <abstractions/nameservice>\n"
        f"  {attach} mr,\n"
        "}\n"
    )
    load_text(body, "complain")
    write_owned(key, "complain", body)
    return {"name": name, "mode": "complain", "policy": key, "exec": attach, "store": in_store, "existed": False, "managed": "gui"}


def show(name):
    item = find_policy(name)
    if not item:
        raise SystemExit(f"{name} is not a saved profile")
    return {"name": item["name"], "policy": item["key"], "mode": effective_mode(item), "managed": item["managed"], "text": item["runtime"]}


def clean(name):
    item = find_policy(name)
    if not item:
        raise SystemExit(f"{name} is not a saved profile")
    directory = tempfile.mkdtemp(prefix="apparmor-clean-")
    try:
        link_includes(directory)
        path = os.path.join(directory, item["key"])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(item["runtime"])
        program = item.get("exec") or item["name"]
        code, out = run_text(["aa-cleanprof", "-s", "-d", directory, program])
        if code != 0:
            raise SystemExit(out.strip() or "aa-cleanprof failed")
        body = open(path, encoding="utf-8").read()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    mode = effective_mode(item)
    if mode != "disable":
        load_text(body, mode)
    write_owned(item["key"], mode, body)
    return {"name": item["name"], "output": out.strip(), "copied": item["managed"] != "gui", "store": "/nix/store/" in body}


def forget(name):
    item = find_policy(name)
    if not item:
        raise SystemExit(f"{name} is not a saved profile")
    if item["managed"] == "system":
        raise SystemExit(
            f"{name} comes from the NixOS configuration. Disable unloads it until the next rebuild. "
            "This window does not edit that configuration."
        )
    if item["managed"] == "gui":
        try:
            load_text(item["runtime"], "disable")
        except SystemExit:
            pass
    remove_saved(item["key"])
    restored = os.path.isfile(os.path.join("/etc/apparmor.d", item["key"]))
    if restored or item["managed"] == "override":
        reload_service()
    return {"name": item["name"], "removed": item["key"], "restored": restored}


def import_file(path):
    if not os.path.isfile(path):
        raise SystemExit("that profile file does not exist")
    body = open(path, encoding="utf-8", errors="replace").read()
    name = profile_name(body)
    if not name or "{" not in body:
        raise SystemExit("that file does not contain an AppArmor profile")
    attach = attachment(body)
    key = policy_key(attach or name)
    existing = find_policy(name)
    if existing and existing["managed"] == "gui":
        key = existing["key"]
    elif os.path.exists(os.path.join(profile_dir(), key + ".profile")) or os.path.exists(os.path.join("/etc/apparmor.d", key)):
        current = find_policy(name)
        key = current["key"] if current else unique_key(key)
    load_text(body, "complain")
    write_owned(key, "complain", body)
    return {"name": name, "policy": key, "mode": "complain", "store": "/nix/store/" in body}


def load_saved():
    loaded = []
    for item in saved_policies():
        if item["managed"] == "system" or item["mode"] == "disable":
            if item["managed"] == "override" and item["mode"] == "disable":
                text = installed_text(item["key"]) or item["runtime"]
                try:
                    load_text(text, "disable")
                except SystemExit:
                    pass
            continue
        text = item["runtime"] if item["managed"] == "gui" else (installed_text(item["key"]) or item["runtime"])
        load_text(text, item["mode"])
        loaded.append(item["name"])
    return loaded


def remove_unknown():
    code, out = run_text(["aa-remove-unknown"])
    if code != 0:
        raise SystemExit(out.strip() or "aa-remove-unknown failed")
    restored = False
    reload_error = ""
    try:
        reload_service()
        restored = True
    except SystemExit as exc:
        reload_error = str(exc)
        load_saved()
    return {"ok": True, "output": out.strip(), "restored": restored, "error": reload_error}


def teardown():
    code, out = run_text(["aa-teardown"])
    if code != 0:
        raise SystemExit(out.strip() or "aa-teardown failed")
    return {"ok": True, "output": out.strip()}


def status_text():
    return {"text": check(["aa-status"])}


def features_text():
    return {"text": check(["aa-features-abi", "-x", "--stdout"])}


def seed_cache():
    os.makedirs(CACHE, mode=0o700, exist_ok=True)
    link_includes(CACHE)
    for name in os.listdir(CACHE):
        path = os.path.join(CACHE, name)
        if os.path.isfile(path) and not os.path.islink(path):
            os.unlink(path)
    for item in saved_policies():
        if item["mode"] == "disable" and item["managed"] in ("gui", "override"):
            continue
        if item["key"] in RESERVED:
            continue
        with open(os.path.join(CACHE, item["key"]), "w", encoding="utf-8") as handle:
            handle.write(item["runtime"])
    return file_hashes(CACHE)


def file_hashes(directory):
    found = {}
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and not os.path.islink(path):
            found[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return found


def import_learned(before):
    saved = []
    stale = []
    current = {item["key"]: item for item in saved_policies()}
    for name, digest in file_hashes(CACHE).items():
        if name in RESERVED or before.get(name) == digest:
            continue
        body = open(os.path.join(CACHE, name), encoding="utf-8", errors="replace").read()
        if "{" not in body:
            continue
        previous = current.get(name)
        mode = effective_mode(previous) if previous else "complain"
        if mode != "disable":
            load_text(body, mode)
        write_owned(name, mode, body)
        label = profile_name(body) or name
        saved.append(label)
        if "/nix/store/" in body:
            stale.append(label)
    return saved, stale


def normalize_log(text):
    # libapparmor reads "type=AVC msg=audit(time:serial): apparmor=...".
    # The kernel journal writes "audit: type=1400 audit(time:serial): apparmor=...".
    lines = []
    seen = set()
    for line in text.splitlines():
        match = re.search(r"audit\((\d+\.\d+:\d+)\):\s*(apparmor=.*)$", line)
        if not match:
            continue
        event = f"type=AVC msg=audit({match.group(1)}): {match.group(2)}"
        if event not in seen:
            seen.add(event)
            lines.append(event)
    return "\n".join(lines) + ("\n" if lines else "")


def logprof():
    before = seed_cache()
    log = subprocess.run(
        ["journalctl", "-b", "-k", "-g", "apparmor=", "--no-pager", "-o", "cat"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    handle = tempfile.NamedTemporaryFile("w", delete=False)
    handle.write(normalize_log(log.stdout or ""))
    handle.close()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    child = subprocess.Popen(
        ["aa-logprof", "-d", CACHE, "-f", handle.name, "-j"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    def pump_in():
        try:
            for line in sys.stdin:
                if child.poll() is not None:
                    break
                child.stdin.write(line)
                child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            child.stdin.close()
        except OSError:
            pass

    threading.Thread(target=pump_in, daemon=True).start()
    error = ""
    try:
        for line in child.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({"dialog": "info", "data": line}), flush=True)
                continue
            if message.get("dialog") == "changes" and message.get("filename"):
                try:
                    message["text"] = open(message["filename"], encoding="utf-8", errors="replace").read()[:20000]
                except OSError as exc:
                    message["text"] = str(exc)
            print(json.dumps(message), flush=True)
        error = child.stderr.read()[-4000:]
        code = child.wait()
    finally:
        os.unlink(handle.name)
    saved, stale = import_learned(before) if code == 0 else ([], [])
    print(json.dumps({
        "dialog": "done",
        "code": code,
        "saved": saved,
        "stale": stale,
        "error": error.strip(),
    }), flush=True)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("missing command")
    command = sys.argv[1]
    if command == "overview":
        print(json.dumps(overview()))
    elif command == "state":
        print(json.dumps({"state": state_root()}))
    elif command == "state-set" and len(sys.argv) == 3:
        print(json.dumps(set_state(sys.argv[2])))
    elif command == "log":
        print(json.dumps(log_lines()))
    elif command == "set" and len(sys.argv) == 4:
        print(json.dumps(set_mode(sys.argv[2], sys.argv[3])))
    elif command == "audit" and len(sys.argv) == 4:
        print(json.dumps(set_audit(sys.argv[2], sys.argv[3])))
    elif command == "create" and len(sys.argv) == 3:
        print(json.dumps(create(sys.argv[2])))
    elif command == "show" and len(sys.argv) == 3:
        print(json.dumps(show(sys.argv[2])))
    elif command == "clean" and len(sys.argv) == 3:
        print(json.dumps(clean(sys.argv[2])))
    elif command == "forget" and len(sys.argv) == 3:
        print(json.dumps(forget(sys.argv[2])))
    elif command == "import-file" and len(sys.argv) == 3:
        print(json.dumps(import_file(sys.argv[2])))
    elif command == "reload":
        try:
            reload_service()
            print(json.dumps({"reloaded": True}))
        except SystemExit:
            print(json.dumps({"reloaded": False, "loaded": load_saved()}))
    elif command == "remove-unknown":
        print(json.dumps(remove_unknown()))
    elif command == "teardown":
        print(json.dumps(teardown()))
    elif command == "status":
        print(json.dumps(status_text()))
    elif command == "features":
        print(json.dumps(features_text()))
    elif command == "logprof":
        logprof()
    else:
        raise SystemExit("unknown command")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr or exc.stdout or f"command failed: {exc.returncode}")
