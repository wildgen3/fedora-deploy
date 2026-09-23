#!/usr/bin/env python3
"""lxqtpostinstall.py - one-time setup for the Fedora LXQt spin on a Dell PowerEdge R710.

Run it as your normal (wheel/administrator) user, never as root:

    python3 lxqtpostinstall.py            # do everything
    python3 lxqtpostinstall.py --dry-run  # only print what would change

Outline of what it does, in order:
  0. Safety checks: not root, Fedora (read from /etc/os-release), user is in
     wheel, one `sudo -v` password prompt, then a background thread keeps
     sudo alive until the end.
  1. System update: `dnf upgrade --refresh`. If this fails, the script stops.
  2. Third-party repos: `fedora-third-party enable`, then enable any of its
     DNF repos that are still disabled, then `dnf makecache`.
  3. Helper tools (curl, wget, unzip, ...).
  4. Server packages (Cockpit, SSH, containers/VMs, monitoring, storage tools).
     Every package is checked with `dnf info` first; missing ones are skipped.
  5. Services: sshd + Cockpit, libvirt, tuned, fail2ban (sshd jail), LVM
     monitor, and masking sleep/suspend/hibernate.
  6. Time: America/Chicago, NTP on, 24-hour time for the command line and LXQt.
  7. Remote desktop: xrdp + xorgxrdp, enabled at boot, each RDP login gets
     its own LXQt desktop. No screen lock and no idle power actions.
  8. Read-only drive report (nothing is created, wiped or formatted).
  9. Summary (including network interfaces), then "Reboot now? [y/N]".

Everything is logged to ~/postinstall-logs/. Running it twice is harmless.
Only the Python standard library is used.
"""

import argparse
import datetime
import grp
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

# ----------------------------------------------------------------- settings

SCRIPT = "lxqtpostinstall.py"

TIMEZONE = "America/Chicago"

# Locale used only for time/date formats (LC_TIME), both system-wide and in LXQt.
#   en_GB.UTF-8 -> 24-hour time, but dates switch to day-first order (23/09/2026).
#   en_DK.UTF-8 -> 24-hour time with ISO dates (2026-09-23).
TIME_LOCALE = "en_GB.UTF-8"

TUNED_PROFILE = "throughput-performance"

# Packages, grouped so a problem in one group doesn't block the others.
HELPER_PACKAGES = ["curl", "wget", "unzip", "ethtool", "pciutils", "util-linux"]
SERVER_PACKAGES = {
    # dnf expands 'cockpit*' to every Cockpit package. Commands are passed as a
    # list (no shell), so the shell never sees the '*'.
    "remote management": ["cockpit*", "openssh-server", "tmux"],
    "containers and VMs": ["podman", "distrobox", "qemu-kvm", "libvirt", "virt-manager"],
    "monitoring and maintenance": ["btop", "htop", "smartmontools", "tuned", "rsync"],
    "file sharing (install only)": ["nfs-utils", "samba"],
    "security": ["fail2ban"],
    "basics": ["git", "vim", "nano"],
    "storage tools": ["mdadm", "lvm2", "xfsprogs"],
}

# fail2ban reads jail.conf, then overrides from jail.d/*.local. A separate file
# leaves jail.conf untouched, and rewriting the whole file avoids duplicate lines.
FAIL2BAN_JAIL = "/etc/fail2ban/jail.d/sshd.local"
FAIL2BAN_CONTENT = f"""\
# Managed by {SCRIPT}. Enables the sshd jail.
[sshd]
enabled  = true
# Ban an address for 1 hour after 5 failed logins within 10 minutes.
maxretry = 5
findtime = 10m
bantime  = 1h
"""

RDP_PORT = 3389  # xrdp default; the firewall is not changed

# xrdp starts a desktop by running /etc/X11/xinit/Xsession, which runs the
# user's ~/.Xclients. A copy in /etc/skel means accounts created later
# (e.g. a friend's) get LXQt too.
XRDP_PACKAGES = ["xrdp", "xorgxrdp", "xorg-x11-xinit"]
XSESSION = "/etc/X11/xinit/Xsession"
SKEL_XCLIENTS = "/etc/skel/.Xclients"

# Logs and reports go to the home directory, not next to the script.
HOME = Path.home()
LOG_DIR = HOME / "postinstall-logs"
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOG_DIR / f"{SCRIPT[:-3]}-{STAMP}.log"
REPORT_FILE = LOG_DIR / f"drive-report-{STAMP}.txt"

# ------------------------------------------------------------------ state

DRY_RUN = False
LOG = None            # open log file handle
FAILURES = []         # things that went wrong but didn't stop the script
SKIPPED = []          # things skipped on purpose (with the reason)
NOTES = []            # reminders for the final summary
PACKAGES = []         # (package, result) for the summary
FACTS = {}            # values collected along the way for the summary


# ---------------------------------------------------------------- output

def log(msg):
    """Write a line to the log file only."""
    if LOG:
        LOG.write(msg + "\n")
        LOG.flush()


def say(msg=""):
    """Print a line and log it."""
    print(msg, flush=True)
    log(msg)


def warn(msg):
    say(f"WARNING: {msg}")


def failed(what):
    """Record a non-fatal failure; the script keeps going."""
    FAILURES.append(what)
    say(f"FAILED: {what}")


def skipped(what):
    SKIPPED.append(what)
    warn(f"skipped: {what}")


def fatal(msg):
    """Stop the whole script. Only for problems that make continuing pointless."""
    say(f"\nFATAL: {msg}")
    if LOG:
        say(f"Log: {LOG_FILE}")
    sys.exit(1)


def step(title):
    say(f"\n==== {title} ====")


def run(cmd, changes_system=True, input_text=None):
    """Print, run and log one command. Returns a CompletedProcess.

    changes_system=True: the command modifies something. With --dry-run it is
    only printed. changes_system=False: a read-only lookup (dnf info, lsblk,
    wipefs --no-act, ...). Those run even in a dry run so the preview can make
    the same decisions a real run would.
    """
    shown = shlex.join(cmd)
    if DRY_RUN and changes_system:
        say(f"[dry-run] {shown}")
        if input_text:
            say("[dry-run]   with this content:")
            for line in input_text.splitlines():
                say(f"[dry-run]     {line}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    say(f"$ {shown}")
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            # Empty stdin so nothing waits for input (sudo prompts via the terminal).
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except FileNotFoundError as err:
        result = subprocess.CompletedProcess(cmd, 127, "", str(err))

    # Full output goes to the log; the terminal only sees output on failure.
    if result.stdout.strip():
        log(result.stdout.rstrip())
    if result.stderr.strip():
        log(result.stderr.rstrip())
    log(f"(exit code {result.returncode})")
    if result.returncode != 0 and changes_system:
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        for line in tail:
            say(f"    {line}")
    return result


def output_of(cmd):
    """Run a read-only command and return its stripped stdout ('' on failure)."""
    r = run(cmd, changes_system=False)
    return r.stdout.strip() if r.returncode == 0 else ""


# ------------------------------------------------------ startup checks

def read_os_release():
    """Parse /etc/os-release (KEY=value lines) into a dict."""
    info = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return info


def check_fedora():
    info = read_os_release()
    if info.get("ID") != "fedora":
        fatal(f"this is {info.get('PRETTY_NAME', 'an unknown system')}, not Fedora.")
    FACTS["release"] = info.get("PRETTY_NAME", f"Fedora {info.get('VERSION_ID', '?')}")
    FACTS["kernel"] = os.uname().release
    say(f"Detected: {FACTS['release']} (kernel {FACTS['kernel']})")
    if info.get("VARIANT_ID") != "lxqt":
        warn(f"expected the LXQt spin, found variant '{info.get('VARIANT_ID', 'none')}'. Continuing.")


def check_wheel():
    """Administrators on Fedora are members of the 'wheel' group."""
    try:
        wheel_gid = grp.getgrnam("wheel").gr_gid
    except KeyError:
        fatal("there is no 'wheel' group on this system.")
    if wheel_gid not in os.getgroups() and wheel_gid != os.getgid():
        fatal("your user is not in the 'wheel' group, so it can't use sudo.\n"
              "Make it an administrator (or: usermod -aG wheel <you> as root), log out and in, then rerun.")


def keep_sudo_alive(stop):
    """Background thread: refresh sudo's timestamp every 60 s until told to stop.
    -n means 'never prompt', so this can't hang if the timestamp is gone."""
    while not stop.wait(60):
        subprocess.run(["sudo", "-n", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_sudo():
    say("This script needs your sudo password once for system-level steps.")
    say("$ sudo -v")
    # Read-only checks later (dnf info, wipefs --no-act, smartctl) need sudo
    # too, so this also happens in a dry run. `sudo -v` changes nothing.
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        fatal("sudo -v failed (wrong password, or no sudo rights).")
    stop = threading.Event()
    threading.Thread(target=keep_sudo_alive, args=(stop,), daemon=True).start()
    return stop


# ---------------------------------------------------------------- dnf helpers

def is_dnf5():
    """Fedora 41+ ships dnf5 as `dnf`. Its command syntax differs from dnf4."""
    dnf_path = shutil.which("dnf")
    if dnf_path and "dnf5" in os.path.realpath(dnf_path):
        return True
    r = run(["dnf", "--version"], changes_system=False)
    return "dnf5" in (r.stdout + r.stderr).lower()


def package_available(name):
    """True if dnf can find this package (installed or in an enabled repo).

    `dnf info` only matches package names. Some names are provided by another
    package (`vim` comes from vim-enhanced), so fall back to --whatprovides.
    """
    if run(["sudo", "dnf", "-q", "info", name], changes_system=False).returncode == 0:
        return True
    r = run(["sudo", "dnf", "-q", "repoquery", "--whatprovides", name], changes_system=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def install_packages(names, label):
    """Install a group of packages. Missing ones are skipped with a warning.
    If the group transaction fails, retry one by one to isolate the bad one."""
    say(f"-- {label}")
    wanted = []
    for name in names:
        if package_available(name):
            wanted.append(name)
        else:
            warn(f"{name}: not found in the enabled repositories, skipping")
            PACKAGES.append((name, "skipped: not found in the enabled repositories"))
    if not wanted:
        return

    ok_word = "would install" if DRY_RUN else "installed"
    if run(["sudo", "dnf", "install", "-y", *wanted]).returncode == 0:
        PACKAGES.extend((name, ok_word) for name in wanted)
        return

    warn(f"installing the '{label}' group failed; retrying one package at a time")
    for name in wanted:
        cmd = ["sudo", "dnf", "install", "-y"]
        if "*" in name:
            # A wildcard can match a package that conflicts with another;
            # --skip-broken installs the rest instead of failing them all.
            cmd.append("--skip-broken")
        if run(cmd + [name]).returncode == 0:
            PACKAGES.append((name, ok_word))
        else:
            PACKAGES.append((name, "failed (see log)"))
            failed(f"install {name}")


def repo_states():
    """Map repo id -> 'enabled' / 'disabled' from `dnf repolist --all`.
    Each row is: repo id, repo name (may contain spaces), status."""
    states = {}
    r = run(["sudo", "dnf", "repolist", "--all"], changes_system=False)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] in ("enabled", "disabled"):
            states[parts[0]] = parts[-1]
    return states


def third_party_repo_ids():
    """Repo ids that fedora-third-party manages: every [section] in the .repo
    files shipped by fedora-workstation-repositories."""
    ids = []
    r = run(["rpm", "-ql", "fedora-workstation-repositories"], changes_system=False)
    for path in r.stdout.split():
        if not path.endswith(".repo"):
            continue
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        for m in re.finditer(r"^\s*\[([^\]]+)\]", text, re.M):
            repo_id = m.group(1).strip()
            # Only real package repos, not their debug/source companions.
            if not repo_id.endswith(("-debuginfo", "-source")):
                ids.append(repo_id)
    return ids


# ------------------------------------------------------------ systemd helpers

def unit_exists(unit):
    r = run(["systemctl", "list-unit-files", "--no-legend", unit], changes_system=False)
    return r.returncode == 0 and unit in r.stdout


def enable_now(units):
    """`systemctl enable --now` the units that exist; record missing ones."""
    present = []
    for unit in units:
        if unit_exists(unit):
            present.append(unit)
        elif DRY_RUN:
            say(f"(dry run: {unit} isn't installed yet; a real run installs it first)")
            present.append(unit)
        else:
            failed(f"enable {unit}: unit not found")
    if present and run(["sudo", "systemctl", "enable", "--now", *present]).returncode != 0:
        failed(f"enable {' '.join(present)}")


def unit_state(unit):
    enabled = output_of(["systemctl", "is-enabled", unit]) or "not enabled"
    # is-active prints 'inactive' with a non-zero exit, so read stdout directly.
    active = run(["systemctl", "is-active", unit], changes_system=False).stdout.strip() or "unknown"
    return f"{enabled}, {active}"


# ---------------------------------------------------------------- the steps

def step1_update():
    step("Step 1: system update")
    say("This can take a while on a fresh install.")
    if run(["sudo", "dnf", "upgrade", "--refresh", "-y"]).returncode != 0:
        fatal("`dnf upgrade` failed. Fix networking/repos and rerun.")


def step2_third_party_repos(dnf5):
    step("Step 2: Fedora third-party repositories")

    # The `fedora-third-party` tool comes with fedora-workstation-repositories.
    if not shutil.which("fedora-third-party"):
        install_packages(["fedora-workstation-repositories"], "third-party repo definitions")

    # config-manager is a plugin: dnf5-plugins for dnf5, dnf-plugins-core for dnf4.
    if run(["dnf", "config-manager", "--help"], changes_system=False).returncode != 0:
        install_packages(["dnf5-plugins" if dnf5 else "dnf-plugins-core"], "dnf config-manager plugin")

    # Enables the configured DNF repos and creates the Flathub Flatpak remote.
    if run(["sudo", "fedora-third-party", "enable"]).returncode != 0:
        failed("fedora-third-party enable")

    # Repos can end up added but still disabled. Enable any that are.
    states = repo_states()
    for repo_id in third_party_repo_ids():
        if states.get(repo_id) == "enabled":
            say(f"{repo_id}: already enabled")
            continue
        if dnf5:
            # dnf5 syntax: writes /etc/dnf/repos.override.d/99-config_manager.repo
            cmd = ["sudo", "dnf", "config-manager", "setopt", f"{repo_id}.enabled=1"]
        else:
            cmd = ["sudo", "dnf", "config-manager", "--set-enabled", repo_id]
        if run(cmd).returncode != 0:
            failed(f"enable repo {repo_id}")

    if run(["sudo", "dnf", "makecache"]).returncode != 0:
        failed("dnf makecache")


def step3_helpers():
    step("Step 3: helper tools")
    install_packages(HELPER_PACKAGES, "helper tools")


def step4_server_packages():
    step("Step 4: server packages")
    for label, names in SERVER_PACKAGES.items():
        install_packages(names, label)


def write_root_file(path, content, mode=None):
    """Write a root-owned file via `sudo tee`, only if its content differs.
    mode (e.g. "755") is applied with chmod after writing."""
    try:
        if Path(path).read_text() == content:
            say(f"{path}: already up to date")
            return
    except OSError:
        pass  # doesn't exist yet (or unreadable): write it
    run(["sudo", "mkdir", "-p", os.path.dirname(path)])
    # tee copies stdin into the file; its own stdout copy is just discarded.
    if run(["sudo", "tee", path], input_text=content).returncode != 0:
        failed(f"write {path}")
    elif mode and run(["sudo", "chmod", mode, path]).returncode != 0:
        failed(f"chmod {mode} {path}")


def step5_services():
    step("Step 5: services")

    # SSH and Cockpit first (remote management). Fedora's unit is sshd.service;
    # some distributions call it ssh.service.
    ssh_unit = next((u for u in ("sshd.service", "ssh.service") if unit_exists(u)), "sshd.service")
    enable_now([ssh_unit, "cockpit.socket"])
    FACTS["ssh_unit"] = ssh_unit

    # libvirt: Fedora (since 35) runs one small daemon per driver, started on
    # demand by sockets (virtqemud.socket etc.). The old all-in-one libvirtd is
    # still packaged, but it conflicts with the modular daemons, so it's only
    # used when the modular units don't exist.
    modular = [f"{d}.socket" for d in ("virtqemud", "virtnetworkd", "virtstoraged", "virtnodedevd",
                                       "virtsecretd", "virtinterfaced", "virtnwfilterd", "virtproxyd")]
    if unit_exists("virtqemud.socket"):
        libvirt_units = [u for u in modular if unit_exists(u)]
        say("libvirt: using the modular daemons (virtqemud.socket and friends)")
    elif unit_exists("libvirtd.service"):
        libvirt_units = ["libvirtd.service"]
        say("libvirt: modular daemons not found, using libvirtd")
    else:
        libvirt_units = ["virtqemud.socket"] if DRY_RUN else []
        if not DRY_RUN:
            failed("libvirt: no libvirt units found")
    if libvirt_units:
        enable_now(libvirt_units)
    FACTS["libvirt_units"] = libvirt_units

    # tuned: tunes the kernel for a workload. throughput-performance favors
    # raw speed over power saving, which suits a server.
    enable_now(["tuned.service"])
    if TUNED_PROFILE in output_of(["tuned-adm", "active"]):
        say(f"tuned: profile already {TUNED_PROFILE}")
    elif run(["sudo", "tuned-adm", "profile", TUNED_PROFILE]).returncode != 0:
        failed(f"tuned-adm profile {TUNED_PROFILE}")
    if unit_exists("tuned-ppd.service"):
        NOTES.append("tuned-ppd (the desktop power-profile bridge) is installed. If the power profile is "
                     "changed it can switch tuned away from throughput-performance; check `tuned-adm active`.")

    # fail2ban: bans IPs that keep failing SSH logins.
    write_root_file(FAIL2BAN_JAIL, FAIL2BAN_CONTENT)
    enable_now(["fail2ban.service"])

    # LVM monitoring (snapshots/mirrors) for volumes created later.
    enable_now(["lvm2-monitor.service"])

    say("mdmonitor: not enabled yet. It needs a real array; enable it after you create one "
        "(sudo systemctl enable --now mdmonitor).")
    NOTES.append("mdmonitor is not enabled. After creating an mdadm array: sudo systemctl enable --now mdmonitor")

    # Masking points a unit at /dev/null, so nothing (not even the power
    # button or an idle timer) can put the server to sleep.
    if run(["sudo", "systemctl", "mask", "sleep.target", "suspend.target",
            "hibernate.target", "hybrid-sleep.target"]).returncode != 0:
        failed("mask sleep targets")


def norm_locale(name):
    """'en_GB.UTF-8' and 'en_GB.utf8' are the same locale; compare them loosely."""
    return name.lower().replace("-", "")


def locale_installed():
    return any(norm_locale(l) == norm_locale(TIME_LOCALE)
               for l in output_of(["locale", "-a"]).split())


def read_locale_conf():
    """Current system locale settings from /etc/locale.conf (KEY=value lines)."""
    values = {}
    try:
        for line in Path("/etc/locale.conf").read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key] = value.strip('"')
    except OSError:
        pass
    return values


def step6_time():
    step("Step 6: time zone and 24-hour time")
    if run(["sudo", "timedatectl", "set-timezone", TIMEZONE]).returncode != 0:
        failed(f"set time zone {TIMEZONE}")
    if run(["sudo", "timedatectl", "set-ntp", "true"]).returncode != 0:
        failed("turn on NTP sync")

    # The locale must exist before anything can use it. English locales come
    # from glibc-langpack-en (the langpack is named after the language code).
    FACTS["locale_ok"] = locale_installed()
    if not FACTS["locale_ok"]:
        install_packages([f"glibc-langpack-{TIME_LOCALE.split('_')[0]}"], "locale data")
        FACTS["locale_ok"] = DRY_RUN or locale_installed()
    if not FACTS["locale_ok"]:
        failed(f"locale {TIME_LOCALE} is not available; skipping 24-hour time settings")
        return

    # System-wide (command-line tools). `localectl set-locale` replaces the
    # whole locale setting, so pass the current values (LANG etc.) back in
    # along with the new LC_TIME, or LANG could be dropped.
    current = read_locale_conf()
    if current.get("LC_TIME") == TIME_LOCALE:
        say(f"system LC_TIME already {TIME_LOCALE}")
    else:
        current["LC_TIME"] = TIME_LOCALE
        args = [f"{k}={v}" for k, v in current.items()]
        if run(["sudo", "localectl", "set-locale", *args]).returncode != 0:
            failed("localectl set-locale")


# ------------------------------------------------ LXQt (per-user, no sudo)

def package_has_keys(package, names):
    """Check an installed package really uses these config names by looking
    for them inside its files (Qt programs keep them as plain or UTF-16
    strings). Returns the names NOT found (all of them if not installed)."""
    missing = set(names)
    r = run(["rpm", "-ql", package], changes_system=False)
    if r.returncode != 0:
        return missing
    for path in r.stdout.split():
        if not missing:
            break
        # Only libraries and programs can contain them.
        if not (".so" in path or "/bin/" in path or "/libexec/" in path):
            continue
        p = Path(path)
        if p.is_symlink() or not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        for name in list(missing):
            if name.encode() in data or name.encode("utf-16-le") in data:
                missing.discard(name)
    return missing


def verified(package, names, what):
    """Only write settings the installed package actually uses."""
    missing = package_has_keys(package, names)
    if missing:
        skipped(f"{what}: {', '.join(sorted(missing))} not found in the installed {package} package")
        return False
    return True


def write_user_file(path, text, what, mode=None):
    """Write a file in the home directory (no sudo), only if it changed."""
    try:
        if path.read_text() == text and (mode is None or path.stat().st_mode & 0o777 == mode):
            say(f"{path}: already up to date")
            return True
    except OSError:
        pass
    if DRY_RUN:
        say(f"[dry-run] write {path} ({what})")
        for line in text.splitlines():
            say(f"[dry-run]     {line}")
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode is not None:
            path.chmod(mode)
    except OSError as err:
        failed(f"write {path}: {err}")
        return False
    say(f"wrote {path} ({what})")
    return True


def set_ini(path, section, key, value):
    """Set key=value inside [section] of an INI-style file (LXQt's .conf
    files), creating the file, section or key as needed. Other lines are
    kept, and an existing key is replaced rather than repeated."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    header, entry = f"[{section}]", f"{key}={value}"
    out, in_section, done = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not done:
                # Section ended without the key: add it before the blank lines.
                blanks = 0
                while out and not out[-1].strip():
                    out.pop()
                    blanks += 1
                out += [entry] + [""] * blanks
                done = True
            in_section = stripped == header
        elif in_section and stripped.split("=", 1)[0].strip() == key:
            if not done:
                out.append(entry)
                done = True
            continue  # drop the old value (and any duplicates)
        out.append(line)
    if not done:
        if in_section:
            out.append(entry)
        else:
            if out and out[-1].strip():
                out.append("")
            out += [header, entry]
    return write_user_file(path, "\n".join(out) + "\n", f"[{section}] {entry}")


def set_line(path, prefix, line):
    """Make sure the file has exactly one line starting with prefix."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    out = [l for l in lines if not l.startswith(prefix)]
    first = next((n for n, l in enumerate(lines) if l.startswith(prefix)), len(out))
    out.insert(min(first, len(out)), line)
    return write_user_file(path, "\n".join(out) + "\n", line.replace("\t", " "))


def step6_lxqt_time():
    step("Step 6b: LXQt 24-hour time")
    if not FACTS.get("locale_ok"):
        return
    # LXQt's Locale settings export LC_TIME through the [Environment] group
    # of session.conf; lxqt-session applies it at login. The panel clock
    # uses the locale's time format, so this makes it 24-hour too.
    if verified("lxqt-session", ["Environment"], "LXQt session locale"):
        FACTS["lxqt_lc_time"] = set_ini(HOME / ".config/lxqt/session.conf", "Environment",
                                        "LC_TIME", TIME_LOCALE)


# ------------------------------------------- remote desktop / unattended session

def lxqt_x11_command():
    """The command that starts LXQt on X11, read from its session file.
    xrdp runs an X11 desktop, so a Wayland-only session can't be used."""
    for f in sorted(Path("/usr/share/xsessions").glob("*.desktop")):
        if "lxqt" not in f.name.lower():
            continue
        for line in f.read_text().splitlines():
            if line.startswith("Exec="):
                return line[len("Exec="):].strip()
    return None


def step7_remote_session():
    step("Step 7: remote desktop (xrdp) and unattended session")

    # xrdp is the RDP server; xorgxrdp is the X display it draws each
    # session on; xorg-x11-xinit provides the Xsession script xrdp runs.
    install_packages(XRDP_PACKAGES, "xrdp remote desktop")

    # Tell xrdp logins to start LXQt: ~/.Xclients for you, /etc/skel for
    # accounts made later. Without it, Xsession falls back to another desktop.
    command = lxqt_x11_command()
    if command is None and DRY_RUN:
        command = "startlxqt"
        say("(dry run: no LXQt X11 session file found; assuming startlxqt)")
    if command is None:
        skipped("xrdp desktop: no LXQt session in /usr/share/xsessions")
    else:
        xclients = f"#!/bin/sh\n# Managed by {SCRIPT}: desktop started for xrdp logins.\nexec {command}\n"
        ok_home = write_user_file(HOME / ".Xclients", xclients, "desktop for xrdp logins", 0o755)
        write_root_file(SKEL_XCLIENTS, xclients, mode="755")
        FACTS["xrdp_desktop"] = command if ok_home else None
        if not DRY_RUN and not os.path.exists(XSESSION):
            warn(f"{XSESSION} is missing, so xrdp may not run ~/.Xclients")

    # enable --now: start xrdp now and at every boot, so RDP works after a
    # restart without anyone logging in at the server. xrdp-sesman (the
    # session manager) is a separate unit on current Fedora.
    units = ["xrdp.service"] + [u for u in ("xrdp-sesman.service",) if unit_exists(u)]
    enable_now(units)
    FACTS["xrdp_units"] = units

    # --- Screen lock off. On X11, LXQt locks via xscreensaver; mode off
    # means no blanking and so no lock.
    if shutil.which("xscreensaver"):
        FACTS["screenlock_off"] = all([
            set_line(HOME / ".xscreensaver", "mode:", "mode:\t\toff"),
            set_line(HOME / ".xscreensaver", "lock:", "lock:\t\tFalse"),
        ])
    else:
        say("xscreensaver isn't installed, so nothing locks the screen")
        FACTS["screenlock_off"] = True

    # --- Power: with the idleness watchers off, LXQt takes no action (dim,
    # screen off, suspend, lock) when the session sits idle.
    if verified("lxqt-powermanagement", ["enableIdlenessWatcher", "enableIdlenessBacklightWatcher"],
                "LXQt idle power actions"):
        conf = HOME / ".config/lxqt/lxqt-powermanagement.conf"
        FACTS["power_off"] = all([
            set_ini(conf, "General", "enableIdlenessWatcher", "false"),
            set_ini(conf, "General", "enableIdlenessBacklightWatcher", "false"),
        ])

    NOTES.append("Log out of the server's own screen before connecting over RDP as the same user; "
                 "two desktops for one user at once can conflict.")


# ------------------------------------------------------ drive report (read-only)

def human_size(num_bytes):
    """Decimal GB/TB, the same units drive vendors print on the label."""
    if num_bytes >= 1e12:
        return f"{num_bytes / 1e12:.2f} TB"
    return f"{num_bytes / 1e9:.1f} GB"


def parent_disk(dev):
    """Walk up from a partition / LVM / LUKS / md device to its whole disk,
    e.g. /dev/sda3 -> 'sda'. Returns None if it can't be worked out."""
    for _ in range(10):  # depth limit guards against loops
        dev_type = output_of(["lsblk", "-ndo", "TYPE", dev])
        if dev_type == "disk":
            return os.path.basename(os.path.realpath(dev))
        parent = output_of(["lsblk", "-ndo", "PKNAME", dev]).splitlines()
        if not parent or not parent[0].strip():
            return None
        dev = "/dev/" + parent[0].strip()
    return None


def mount_disk(mountpoint):
    """Disk behind a mount point, or None if it isn't mounted separately."""
    source = output_of(["findmnt", "-no", "SOURCE", mountpoint])
    if not source:
        return None
    # btrfs shows the subvolume too, e.g. /dev/sda3[/root]. Drop the [..] part.
    source = re.sub(r"\[.*\]$", "", source.splitlines()[0])
    return parent_disk(source)


def smart_report():
    """Physical drives behind the PERC, via smartctl's megaraid pass-through."""
    lines = []
    if not (shutil.which("smartctl") or os.path.exists("/usr/sbin/smartctl")):
        return ["  - smartctl not installed (smartmontools); skipped"]
    scan = run(["sudo", "smartctl", "--scan"], changes_system=False)
    # Lines look like: /dev/bus/0 -d megaraid,0 # /dev/bus/0 [megaraid_disk_00], SCSI device
    entries = []
    for line in scan.stdout.splitlines():
        parts = line.split("#")[0].split()
        if len(parts) >= 3 and parts[1] == "-d" and parts[2].startswith("megaraid"):
            entries.append((parts[0], parts[2]))
    if not entries:
        return ["  - smartctl --scan listed no megaraid drives (health check not available)"]

    # SATA and SAS drives label the same facts differently.
    labels = {
        "model": ("Device Model:", "Model Number:", "Product:"),
        "vendor": ("Vendor:",),
        "serial": ("Serial Number:", "Serial number:"),
        "capacity": ("User Capacity:",),
        "health": ("SMART overall-health self-assessment test result:", "SMART Health Status:"),
    }
    for dev, dtype in entries:
        r = run(["sudo", "smartctl", "-i", "-H", "-d", dtype, dev], changes_system=False)
        info = {}
        for line in r.stdout.splitlines():
            for field, prefixes in labels.items():
                for prefix in prefixes:
                    if line.startswith(prefix) and field not in info:
                        info[field] = line[len(prefix):].strip()
        if "model" not in info and "health" not in info:
            lines.append(f"  - {dtype}: couldn't read drive info (exit {r.returncode}); see log")
            FAILURES.append(f"smartctl {dtype} {dev}")
            continue
        model = " ".join(x for x in (info.get("vendor"), info.get("model")) if x)
        capacity = info.get("capacity", "?")
        m = re.search(r"\[(.+)\]", capacity)  # "300,000,000,000 bytes [300 GB]"
        lines.append(f"  - {dtype}: {model or '?'}, serial {info.get('serial', '?')}, "
                     f"{m.group(1) if m else capacity}, health {info.get('health', '?')}")
    return lines


def step8_drive_report():
    step("Step 8: drive report (read-only)")
    rep = [f"Drive report - {datetime.datetime.now():%Y-%m-%d %H:%M} - {FACTS.get('release', '')}", ""]

    # --- where is the OS installed?
    root_disk = mount_disk("/")
    if not root_disk:
        fatal("couldn't work out which disk holds / (findmnt/lsblk gave no answer).")
    os_disks = {root_disk: ["/"]}
    for mp in ("/boot", "/boot/efi"):
        d = mount_disk(mp)
        if d:
            os_disks.setdefault(d, []).append(mp)

    # --- LVM physical volumes and mdadm members, mapped back to whole disks
    pv_disks = set()
    pvs = run(["sudo", "pvs", "--noheadings", "-o", "pv_name"], changes_system=False)
    for pv in pvs.stdout.split():
        d = parent_disk(pv)
        if d:
            pv_disks.add(d)
    md_disks = {}
    mdstat = run(["cat", "/proc/mdstat"], changes_system=False).stdout
    for line in mdstat.splitlines():
        m = re.match(r"^(md\S+)\s*:\s*(.*)", line)
        if not m:
            continue
        for member in re.findall(r"(\S+?)\[\d+\]", m.group(2)):  # e.g. sdb1[0]
            d = parent_disk("/dev/" + member)
            if d:
                md_disks.setdefault(d, []).append(m.group(1))

    # --- every disk
    r = run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS,PKNAME"],
            changes_system=False)
    try:
        devices = json.loads(r.stdout)["blockdevices"]
    except (ValueError, KeyError):
        fatal("couldn't read the disk list from lsblk.")

    groups = {"os": [], "empty": [], "data": []}
    for dev in devices:
        name, size = dev.get("name", ""), int(dev.get("size") or 0)
        # zram0 is Fedora's compressed-RAM swap; it reports as a 'disk' but isn't one.
        if dev.get("type") != "disk" or name.startswith("zram") or size == 0:
            continue
        children = dev.get("children") or []
        parts = [c["name"] for c in children if c.get("type") == "part"]
        wipe = run(["sudo", "wipefs", "--no-act", f"/dev/{name}"], changes_system=False)
        sigs = [l.split()[2] for l in wipe.stdout.splitlines()[1:] if len(l.split()) >= 3]
        if dev.get("fstype") and dev["fstype"] not in sigs:
            sigs.append(dev["fstype"])

        is_os = name in os_disks
        details = [
            f"size {human_size(size)}",
            f"model {(dev.get('model') or '?').strip()}",
            f"serial {(dev.get('serial') or '?').strip()}",
            f"OS disk: {'yes (' + ', '.join(os_disks[name]) + ')' if is_os else 'no'}",
            f"partitions: {', '.join(parts) if parts else 'none'}",
            f"signatures: {', '.join(sigs) if sigs else 'none'}",
            f"LVM PV: {'yes' if name in pv_disks else 'no'}",
            f"mdadm member: {', '.join(md_disks[name]) if name in md_disks else 'no'}",
        ]
        entry = f"  - {name}: " + "; ".join(details)
        if is_os:
            groups["os"].append(entry)
        elif parts or sigs or children or name in pv_disks or name in md_disks:
            groups["data"].append(entry)
        else:
            groups["empty"].append(entry)

    rep.append("OS disk:")
    rep += groups["os"] or ["  - (none matched; root disk is " + root_disk + ")"]
    rep += ["", "Empty, ready for software RAID or LVM:"]
    rep += groups["empty"] or ["  - none"]
    rep += ["", "Has existing data or signatures:"]
    rep += groups["data"] or ["  - none"]

    rep += ["", "Physical drives behind the PERC (SMART):"]
    rep += smart_report()

    rep += ["", "Storage tools:"]
    for label, cmd in (("mdadm", ["mdadm", "--version"]), ("lvm", ["sudo", "lvm", "version"])):
        t = run(cmd, changes_system=False)
        first = (t.stdout + t.stderr).strip().splitlines()
        rep.append(f"  - {label}: {first[0].strip() if t.returncode == 0 and first else 'NOT working'}")
        if t.returncode != 0 and not DRY_RUN:
            FAILURES.append(f"{label} not working")

    FACTS["os_disk"] = ", ".join(f"{d} ({', '.join(m)})" for d, m in os_disks.items())
    FACTS["empty_disks"] = len(groups["empty"])

    text = "\n".join(rep) + "\n"
    REPORT_FILE.write_text(text)
    say("")
    say(text)
    say(f"Saved to {REPORT_FILE}")


# ---------------------------------------------------------------- summary

def primary_ip():
    """The address this machine uses to reach the network. `ip route get`
    only asks the routing table; it sends no packets."""
    m = re.search(r"\bsrc (\S+)", output_of(["ip", "-4", "route", "get", "1.1.1.1"]))
    if m:
        return m.group(1)
    addrs = output_of(["hostname", "-I"]).split()
    return addrs[0] if addrs else "<this machine's IP>"


def network_lines():
    """One line per physical network port: IPv4 address(es), MAC, link state.
    `ip -j` is the JSON form of `ip -4 addr` / `ip link`. Virtual interfaces
    (libvirt/podman bridges) are left out: ports with no /sys/.../device."""
    try:
        links = json.loads(output_of(["ip", "-j", "link"]) or "[]")
        addrs = json.loads(output_of(["ip", "-j", "-4", "addr"]) or "[]")
    except ValueError:
        return ["Network: couldn't read interfaces (see log)"]
    ipv4 = {a.get("ifname"): [f"{i['local']}/{i['prefixlen']}" for i in a.get("addr_info", [])
                               if i.get("family") == "inet"] for a in addrs}
    lines = []
    for link in links:
        name = link.get("ifname", "?")
        if name == "lo" or not os.path.exists(f"/sys/class/net/{name}/device"):
            continue
        lines.append(f"Network {name}: IPv4 {', '.join(ipv4.get(name) or []) or 'none'}, "
                     f"MAC {link.get('address', '?')}, link {link.get('operstate', '?').lower()}")
    return lines or ["Network: no physical interfaces found"]


def step9_summary(third_party_ids):
    step("Step 9: summary")
    s = []
    s.append(f"Fedora: {FACTS.get('release')}, kernel {FACTS.get('kernel')}")

    states = repo_states()
    enabled = [r for r in third_party_ids if states.get(r) == "enabled"]
    s.append(f"Third-party repos enabled: {', '.join(enabled) if enabled else 'none'}")
    remotes = output_of(["flatpak", "remotes", "--columns=name"]).split()
    s.append(f"Flatpak remotes: {', '.join(remotes) if remotes else 'none'}")

    ok = [n for n, res in PACKAGES if not res.startswith(("skipped", "failed"))]
    s.append(f"Packages {'to install' if DRY_RUN else 'installed/present'}: {', '.join(ok) if ok else 'none'}")
    for n, res in PACKAGES:
        if res.startswith(("skipped", "failed")):
            s.append(f"Package {n}: {res}")

    for unit in (FACTS.get("ssh_unit", "sshd.service"), "cockpit.socket", "tuned.service",
                 "fail2ban.service", *FACTS.get("libvirt_units", []), "lvm2-monitor.service"):
        s.append(f"{unit}: {unit_state(unit)}")
    s.append(f"tuned profile: {output_of(['tuned-adm', 'active']).replace('Current active profile: ', '') or 'unknown'}")

    ip = primary_ip()
    s.append(f"Hostname: {socket.gethostname()}")
    s.append(f"Cockpit: https://{ip}:9090")

    s.append(", ".join(f"{u}: {unit_state(u)}" for u in FACTS.get("xrdp_units", ["xrdp.service"])))
    s.append(f"Remote desktop: in Remmina, RDP to {ip}:{RDP_PORT} and log in with your Linux "
             "username and password; each user gets their own desktop")
    s.append(f"xrdp desktop: {FACTS.get('xrdp_desktop') or 'not set'} (~/.Xclients, and {SKEL_XCLIENTS} for new users)")
    s.append(f"Screen lock off: {'yes' if FACTS.get('screenlock_off') else 'no'}")
    s.append(f"Idle power actions off: {'yes' if FACTS.get('power_off') else 'no'}")
    s += network_lines()
    s.append("Set a DHCP reservation on your router for this server's MAC so its IP never changes.")

    tz = output_of(["timedatectl", "show", "-p", "Timezone", "--value"]) or "unknown"
    s.append(f"Time zone: {tz}")
    s.append(f"Time format: system LC_TIME={read_locale_conf().get('LC_TIME', 'not set')}; "
             f"LXQt LC_TIME {'set' if FACTS.get('lxqt_lc_time') else 'not set'}")

    s.append(f"OS disk: {FACTS.get('os_disk', 'unknown')}")
    s.append(f"Empty disks found: {FACTS.get('empty_disks', 0)} (report: {REPORT_FILE})")

    for item in SKIPPED:
        s.append(f"Skipped: {item}")
    if FAILURES:
        for item in FAILURES:
            s.append(f"Failed: {item}")
    else:
        s.append("Failed: nothing")
    for note in NOTES:
        s.append(f"Note: {note}")
    s.append("Reboot to apply the update, kernel and desktop settings. xrdp starts at boot "
             "and waits for connections; no one needs to log in at the server.")
    s.append(f"Full log: {LOG_FILE}")

    say("")
    for line in s:
        say(f"- {line}")


def ask_reboot():
    if DRY_RUN:
        say("\n[dry-run] would ask 'Reboot now? [y/N]' and on yes run: sudo systemctl reboot")
        return
    try:
        answer = input("\nReboot now? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    log(f"Reboot answer: {answer!r}")
    if answer in ("y", "yes"):
        run(["sudo", "systemctl", "reboot"])
    else:
        say("Not rebooting. Reboot later with: sudo systemctl reboot")


# ---------------------------------------------------------------- main

def main():
    global DRY_RUN, LOG

    parser = argparse.ArgumentParser(description="Fedora LXQt post-install setup for a Dell R710.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every command that would change the system, without running it")
    DRY_RUN = parser.parse_args().dry_run

    # Root would put desktop settings and logs in /root instead of your home.
    if os.geteuid() == 0:
        print("Don't run this as root or with sudo. Rerun it as your normal user:\n"
              f"    python3 {SCRIPT}")
        sys.exit(1)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG = open(LOG_FILE, "w")
    say(f"{SCRIPT} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        f"{' (DRY RUN: nothing will be changed)' if DRY_RUN else ''}")
    say(f"Log: {LOG_FILE}")

    check_fedora()
    check_wheel()
    stop_sudo = start_sudo()
    try:
        dnf5 = is_dnf5()
        say(f"Package manager: {'dnf5' if dnf5 else 'dnf4'}")

        step1_update()
        step2_third_party_repos(dnf5)
        step3_helpers()
        step4_server_packages()
        step5_services()
        step6_time()
        step6_lxqt_time()
        step7_remote_session()
        step8_drive_report()
        step9_summary(third_party_repo_ids())
        ask_reboot()
    except KeyboardInterrupt:
        fatal("interrupted.")
    finally:
        stop_sudo.set()


if __name__ == "__main__":
    main()
