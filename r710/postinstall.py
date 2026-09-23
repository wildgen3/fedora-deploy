#!/usr/bin/env python3
"""postinstall.py - one-time setup for Fedora KDE Plasma on a Dell PowerEdge R710.

Run it as your normal (wheel/administrator) user, never as root:

    python3 postinstall.py            # do everything
    python3 postinstall.py --dry-run  # only print what would change

Outline of what it does, in order:
  0. Safety checks: not root, Fedora (read from /etc/os-release), user is in
     wheel, one `sudo -v` password prompt, then a background thread keeps
     sudo alive until the end.
  1. System update: `dnf upgrade --refresh`. If this fails, the script stops.
  2. Third-party repos: `fedora-third-party enable`, then enable any of its
     DNF repos that are still disabled (KDE quirk), then `dnf makecache`.
  3. Helper tools (curl, wget, unzip, ...).
  4. Server packages (Cockpit, SSH, containers/VMs, monitoring, storage tools).
     Every package is checked with `dnf info` first; missing ones are skipped.
  5. Services: sshd + Cockpit, libvirt, tuned, fail2ban (sshd jail), LVM
     monitor, and masking sleep/suspend/hibernate.
  6. Time: America/Chicago, NTP on, 24-hour time for the command line and KDE.
  7. KDE: no animations, no blur/contrast, no Baloo file indexing.
  8. Remote desktop and unattended session: KRDP (KDE's RDP server) starts
     with your session, SDDM logs you in automatically, no screen lock, and
     no screen dimming/turn-off/suspend on AC power.
  9. Read-only drive report (nothing is created, wiped or formatted).
 10. Summary (including network interfaces), then "Reboot now? [y/N]".

Everything is logged to ~/postinstall-logs/. Running it twice is harmless.
Only the Python standard library is used.
"""

import argparse
import datetime
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

# ----------------------------------------------------------------- settings

TIMEZONE = "America/Chicago"

# Locale used only for time/date formats (LC_TIME), both system-wide and in KDE.
#   en_GB.UTF-8 -> 24-hour time, but dates switch to day-first order (23/09/2026).
#   en_DK.UTF-8 -> 24-hour time with ISO dates (2026-09-23).
TIME_LOCALE = "en_GB.UTF-8"

TUNED_PROFILE = "throughput-performance"

# Packages, grouped so a problem in one group doesn't block the others.
HELPER_PACKAGES = ["curl", "wget", "unzip", "ethtool", "pciutils", "util-linux"]
SERVER_PACKAGES = {
    # 'cockpit*' is a wildcard that dnf itself expands to every Cockpit module.
    # We pass arguments as a Python list (no shell), so the shell can't expand it.
    "remote management": ["cockpit*", "openssh-server", "tmux"],
    "containers and VMs": ["podman", "distrobox", "qemu-kvm", "libvirt", "virt-manager"],
    "monitoring and maintenance": ["btop", "htop", "smartmontools", "tuned", "rsync"],
    "file sharing (install only)": ["nfs-utils", "samba"],
    "security": ["fail2ban"],
    "basics": ["git", "vim", "nano"],
    "storage tools": ["mdadm", "lvm2", "xfsprogs"],
}

# fail2ban reads jail.conf first, then overrides from jail.d/*.local.
# Putting our settings in their own file means jail.conf stays untouched and
# package updates never conflict with it. Rewriting the whole file each run
# means reruns can't create duplicate lines.
FAIL2BAN_JAIL = "/etc/fail2ban/jail.d/sshd.local"
FAIL2BAN_CONTENT = """\
# Written by postinstall.py. Turns on the sshd jail; jail.conf is not modified.
[sshd]
enabled  = true
# Ban an address for 1 hour after 5 failed logins within 10 minutes.
maxretry = 5
findtime = 10m
bantime  = 1h
"""

# Plasma config keys this script knows are correct, by Plasma major version.
# If the installed Plasma major version isn't listed (e.g. a future Plasma 7),
# the matching KDE step is skipped with a warning instead of writing a key
# that might not mean anything any more.
KNOWN_PLASMA_KEYS = {
    6: {
        "plasma-localerc/Formats/LC_TIME",
        "appletsrc/digitalclock/Appearance/use24hFormat",
        "kdeglobals/KDE/AnimationDurationFactor",
        "kwinrc/Plugins/blurEnabled",
        "kwinrc/Plugins/contrastEnabled",
        "kscreenlockerrc/Daemon/Autolock",
        "kscreenlockerrc/Daemon/LockOnResume",
        "powerdevilrc/AC/Display/DimDisplayWhenIdle",
        "powerdevilrc/AC/Display/TurnOffDisplayWhenIdle",
        "powerdevilrc/AC/SuspendAndShutdown/AutoSuspendAction",
    },
}

# SDDM (the login screen) reads every file in this folder, so autologin gets
# its own small file instead of editing a shared config.
SDDM_AUTOLOGIN = "/etc/sddm.conf.d/autologin.conf"

RDP_PORT = 3389  # KRDP's default port (already allowed by the firewall zone)

# Logs and reports always go to the home directory, wherever the script lives.
HOME = Path.home()
LOG_DIR = HOME / "postinstall-logs"
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOG_DIR / f"postinstall-{STAMP}.log"
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
            # No input needed? Give the command an empty stdin so nothing can
            # sit waiting for a keypress. (sudo still uses the terminal directly.)
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
    if info.get("VARIANT_ID") != "kde":
        warn(f"expected the KDE edition, found variant '{info.get('VARIANT_ID', 'none')}'. Continuing.")


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
    dnf_path = shutil.which("dnf") or ""
    if "dnf5" in os.path.realpath(dnf_path):
        return True
    r = run(["dnf", "--version"], changes_system=False)
    return "dnf5" in (r.stdout + r.stderr).lower()


def package_available(name):
    """True if dnf can find this package (installed or in an enabled repo).

    `dnf info` matches package names. Some names you'd type are really
    'provides' of another package (e.g. `vim` is provided by vim-enhanced),
    so if info finds nothing, ask repoquery who provides it.
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
    files shipped by fedora-workstation-repositories. Detected, not hard-coded."""
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

    # KDE quirk: repos can end up added but still disabled. Enable any that are.
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


def write_root_file(path, content):
    """Write a root-owned file via `sudo tee`, only if its content differs."""
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


def step5_services():
    step("Step 5: services")

    # SSH + Cockpit first: they're how you'll manage the box remotely.
    # Debian-style systems call SSH 'ssh.service'; Fedora uses 'sshd.service'.
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
        NOTES.append("tuned-ppd (KDE's power-profile bridge) is installed. If KDE's power profile is "
                     "changed it can switch tuned away from throughput-performance; check `tuned-adm active`.")

    # fail2ban: bans IPs that keep failing SSH logins.
    write_root_file(FAIL2BAN_JAIL, FAIL2BAN_CONTENT)
    enable_now(["fail2ban.service"])

    # LVM monitoring (snapshots/mirrors); ready for volumes you'll create later.
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


# ---------------------------------------------------------------- KDE (as me)

def plasma_major():
    """Installed Plasma major version, from the plasma-workspace package."""
    version = output_of(["rpm", "-q", "--qf", "%{VERSION}", "plasma-workspace"])
    return int(version.split(".")[0]) if version[:1].isdigit() else None


def key_known(major, key):
    if key in KNOWN_PLASMA_KEYS.get(major, set()):
        return True
    skipped(f"KDE setting {key}: not verified for Plasma {major}")
    return False


def kwrite(filename, groups, key, value):
    """kwriteconfig6 as the current user (no sudo) -> lands in ~/.config.
    Repeating --group walks into nested groups like [A][B][C]."""
    cmd = ["kwriteconfig6", "--file", filename]
    for g in groups:
        cmd += ["--group", g]
    cmd += ["--key", key, str(value)]
    if run(cmd).returncode != 0:
        failed(f"kwriteconfig6 {filename} {key}")
        return False
    return True


def find_clock_applets():
    """Find Digital Clock widgets in the panel config file.
    Returns group paths like ['Containments', '2', 'Applets', '19']."""
    path = HOME / ".config/plasma-org.kde.plasma.desktop-appletsrc"
    try:
        text = path.read_text()
    except OSError:
        return []
    found, group = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].split("][")
        elif line == "plugin=org.kde.plasma.digitalclock" and group and len(group) == 4:
            found.append(group)
    return found


def kde_setup():
    """Checks shared by steps 6 (KDE part) and 7. Returns Plasma major or None."""
    if not shutil.which("kwriteconfig6"):
        skipped("KDE settings: kwriteconfig6 not found")
        return None
    major = plasma_major()
    if major is None:
        skipped("KDE settings: couldn't read the Plasma version")
        return None
    say(f"Plasma {major} detected")
    return major


def step6_kde_time(major):
    step("Step 6b: KDE 24-hour time (as you)")
    if major is None or not FACTS.get("locale_ok"):
        return

    # Plasma's Region & Language settings: time formats come from LC_TIME.
    if key_known(major, "plasma-localerc/Formats/LC_TIME"):
        FACTS["kde_lc_time"] = kwrite("plasma-localerc", ["Formats"], "LC_TIME", TIME_LOCALE)

    # Panel clock: use24hFormat 0 = 12-hour, 1 = follow region, 2 = 24-hour.
    if key_known(major, "appletsrc/digitalclock/Appearance/use24hFormat"):
        clocks = find_clock_applets()
        if not clocks:
            skipped("24-hour panel clock: no Digital Clock widget found in the panel config")
        for group in clocks:
            if kwrite("plasma-org.kde.plasma.desktop-appletsrc", group + ["Configuration", "Appearance"],
                      "use24hFormat", 2):
                FACTS["kde_clock"] = True


def step7_kde_effects(major):
    step("Step 7: KDE desktop effects (as you)")
    if major is None:
        return
    # 0 = animations finish instantly.
    if key_known(major, "kdeglobals/KDE/AnimationDurationFactor"):
        kwrite("kdeglobals", ["KDE"], "AnimationDurationFactor", 0)
    # Blur and background contrast are GPU-heavy KWin effects.
    if key_known(major, "kwinrc/Plugins/blurEnabled"):
        kwrite("kwinrc", ["Plugins"], "blurEnabled", "false")
    if key_known(major, "kwinrc/Plugins/contrastEnabled"):
        kwrite("kwinrc", ["Plugins"], "contrastEnabled", "false")

    # Baloo indexes file contents in the background; not wanted on a server.
    baloo = shutil.which("balooctl6")
    if not baloo:
        skipped("Baloo: balooctl6 not found")
    elif output_of(["kreadconfig6", "--file", "baloofilerc", "--group", "Basic Settings",
                    "--key", "Indexing-Enabled"]) == "false":
        say("Baloo file indexing already disabled")
    elif run([baloo, "disable"]).returncode != 0:
        failed("balooctl6 disable")

    NOTES.append("KDE changes (effects, clock, time format) apply after you log out or reboot.")


# ------------------------------------------- remote desktop / unattended session

def package_has_keys(package, names):
    """Check an installed package really uses these config names by looking
    for them inside its files. KDE compiles its config definitions into its
    libraries, where Qt stores strings as UTF-16, so look for both encodings.
    Returns the names that were NOT found (all of them if not installed)."""
    missing = set(names)
    r = run(["rpm", "-ql", package], changes_system=False)
    if r.returncode != 0:
        return missing
    for path in r.stdout.split():
        if not missing:
            break
        # Only libraries, programs and config definitions can contain them.
        if not (".so" in path or "/bin/" in path or "/libexec/" in path or path.endswith((".kcfg", ".xml"))):
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


def keys_verified(major, table_keys, package, names):
    """Both checks: known for this Plasma version, and present in the package."""
    if not all(key_known(major, k) for k in table_keys):
        return False
    missing = package_has_keys(package, names)
    if missing:
        skipped(f"{package}: config names {', '.join(sorted(missing))} not found in the installed package")
        return False
    return True


def find_krdp_unit():
    """KRDP's systemd *user* unit, found by listing unit files rather than
    guessing (currently app-org.kde.krdpserver.service)."""
    r = run(["systemctl", "--user", "list-unit-files", "--no-legend"], changes_system=False)
    units = [line.split()[0] for line in r.stdout.splitlines()
             if "krdp" in line.lower() and line.split()[0].endswith(".service")]
    # If there's more than one, the server unit is the one we want.
    units.sort(key=lambda u: "krdpserver" not in u.lower())
    return units[0] if units else None


def sddm_overrides():
    """Other SDDM config files that also set [Autologin] User/Session and are
    read after ours (files are read in name order, /etc/sddm.conf last),
    which would quietly override it."""
    later = sorted(str(p) for p in Path("/etc/sddm.conf.d").glob("*.conf") if p.name > "autologin.conf")
    found = []
    for path in later + ["/etc/sddm.conf"]:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        section = ""
        for line in lines:
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Autologin]" and line.startswith(("User=", "Session=")):
                found.append(path)
                break
    return found


def step8_remote_session(major):
    step("Step 8: remote desktop and unattended session")
    user = pwd.getpwuid(os.getuid()).pw_name  # your login name

    # --- KRDP: KDE's built-in RDP server. It shares your logged-in Plasma
    # session, which is why autologin (below) matters: no session, no desktop.
    if run(["rpm", "-q", "krdp"], changes_system=False).returncode == 0:
        say("krdp already installed")
    else:
        install_packages(["krdp"], "KRDP remote desktop")
    FACTS["krdp_installed"] = DRY_RUN or run(["rpm", "-q", "krdp"], changes_system=False).returncode == 0

    unit = find_krdp_unit()
    FACTS["krdp_unit"] = unit
    if unit:
        # --user = your own systemd instance, so no sudo; it starts with your session.
        if output_of(["systemctl", "--user", "is-enabled", unit]) == "enabled":
            say(f"{unit}: already enabled")
        elif run(["systemctl", "--user", "enable", unit]).returncode != 0:
            failed(f"systemctl --user enable {unit}")
    elif DRY_RUN:
        say("(dry run: KRDP isn't installed yet, so its user unit can't be looked up; "
            "a real run would enable it with systemctl --user enable)")
    else:
        skipped("KRDP autostart: no krdp unit in `systemctl --user list-unit-files`")

    # --- SDDM autologin into the Plasma Wayland session. The session name is
    # the .desktop file name in /usr/share/wayland-sessions/ without '.desktop'.
    sessions = sorted(p.stem for p in Path("/usr/share/wayland-sessions").glob("*.desktop"))
    session = "plasma" if "plasma" in sessions else next((s for s in sessions if "plasma" in s), None)
    if not session:
        skipped(f"SDDM autologin: no Plasma session in /usr/share/wayland-sessions (found: {', '.join(sessions) or 'none'})")
        FACTS["autologin"] = "no (Plasma Wayland session not found)"
    else:
        write_root_file(SDDM_AUTOLOGIN, f"# Written by postinstall.py: log {user} straight into Plasma at boot.\n"
                                        f"[Autologin]\nUser={user}\nSession={session}\n")
        FACTS["autologin"] = f"yes ({user}, session {session})"
        overrides = sddm_overrides()
        if overrides:
            warn(f"{', '.join(overrides)} also set [Autologin] and are read after {SDDM_AUTOLOGIN}")
            FACTS["autologin"] += f", but overridden by {', '.join(overrides)}"
        NOTES.append("Autologin means KWallet isn't unlocked by a password at login. If KRDP asks for the "
                     "wallet or RDP logins fail after a reboot, give the wallet an empty password in KWalletManager.")

    if major is None:
        return  # kwriteconfig6 or Plasma version missing; already reported

    # --- Screen lock off: never lock after idle, and don't lock on wake.
    if keys_verified(major, ["kscreenlockerrc/Daemon/Autolock", "kscreenlockerrc/Daemon/LockOnResume"],
                     "kscreenlocker", ["Autolock", "LockOnResume"]):
        FACTS["screenlock_off"] = all([
            kwrite("kscreenlockerrc", ["Daemon"], "Autolock", "false"),
            kwrite("kscreenlockerrc", ["Daemon"], "LockOnResume", "false"),
        ])

    # --- Power management on AC (Plasma 6 layout: [AC][Display] and
    # [AC][SuspendAndShutdown] in powerdevilrc). AutoSuspendAction 0 = do nothing.
    if keys_verified(major, ["powerdevilrc/AC/Display/DimDisplayWhenIdle",
                             "powerdevilrc/AC/Display/TurnOffDisplayWhenIdle",
                             "powerdevilrc/AC/SuspendAndShutdown/AutoSuspendAction"],
                     "powerdevil", ["DimDisplayWhenIdle", "TurnOffDisplayWhenIdle",
                                    "SuspendAndShutdown", "AutoSuspendAction"]):
        FACTS["power_off"] = all([
            kwrite("powerdevilrc", ["AC", "Display"], "DimDisplayWhenIdle", "false"),
            kwrite("powerdevilrc", ["AC", "Display"], "TurnOffDisplayWhenIdle", "false"),
            kwrite("powerdevilrc", ["AC", "SuspendAndShutdown"], "AutoSuspendAction", 0),
        ])

    NOTES.append("Session changes (autologin, screen lock, power, KRDP autostart) apply after a reboot or logout.")


# ------------------------------------------------------ drive report (read-only)

def human_size(num_bytes):
    """Decimal GB/TB, the same units drive vendors print on the label."""
    if num_bytes >= 1e12:
        return f"{num_bytes / 1e12:.2f} TB"
    return f"{num_bytes / 1e9:.1f} GB"


def parent_disk(dev):
    """Walk up from a partition / LVM / LUKS / md device to its whole disk,
    e.g. /dev/sda3 -> 'sda'. Returns None if it can't be worked out."""
    for _ in range(10):  # a few levels is plenty; stops runaway loops
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
        FACTS["smart_note"] = "no megaraid drives listed by smartctl --scan"
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


def step9_drive_report():
    step("Step 9: drive report (read-only)")
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


def step10_summary(third_party_ids):
    step("Step 10: summary")
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
    s.append(f"Hostname: {socket.gethostname()} (set it yourself in KDE's System Settings)")
    s.append(f"Cockpit: https://{ip}:9090")

    unit = FACTS.get("krdp_unit")
    autostart = bool(unit) and output_of(["systemctl", "--user", "is-enabled", unit]) == "enabled"
    s.append(f"KRDP installed: {'yes' if FACTS.get('krdp_installed') else 'no'}; "
             f"autostart enabled: {'yes' if autostart else 'no'}{f' ({unit})' if unit else ''}")
    s.append(f"Remote desktop: in Remmina, RDP to {ip}:{RDP_PORT}")
    s.append("RDP login: add the RDP username/password once in System Settings > Remote Desktop "
             "(this script doesn't set it)")
    s.append(f"Autologin: {FACTS.get('autologin', 'no')}")
    s.append(f"Screen lock off: {'yes' if FACTS.get('screenlock_off') else 'no'}")
    s.append(f"Power settings (no dim, no screen off, no suspend on AC): {'yes' if FACTS.get('power_off') else 'no'}")
    s += network_lines()
    s.append("Set a DHCP reservation on your router for this server's MAC so its IP never changes.")

    tz = output_of(["timedatectl", "show", "-p", "Timezone", "--value"]) or "unknown"
    s.append(f"Time zone: {tz}")
    s.append(f"Time format: system LC_TIME={read_locale_conf().get('LC_TIME', 'not set')}; "
             f"KDE LC_TIME {'set' if FACTS.get('kde_lc_time') else 'not set'}; "
             f"panel clock {'24-hour' if FACTS.get('kde_clock') else 'unchanged'}")

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
    s.append("Reboot to apply the update, the kernel, and the KDE/locale changes.")
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

    parser = argparse.ArgumentParser(description="Fedora KDE post-install setup for a Dell R710.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every command that would change the system, without running it")
    DRY_RUN = parser.parse_args().dry_run

    # Root would put KDE settings and logs in /root instead of your home.
    if os.geteuid() == 0:
        print("Don't run this as root or with sudo. Rerun it as your normal user:\n"
              "    python3 postinstall.py")
        sys.exit(1)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG = open(LOG_FILE, "w")
    say(f"postinstall.py started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
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
        major = kde_setup()
        step6_kde_time(major)
        step7_kde_effects(major)
        step8_remote_session(major)
        step9_drive_report()
        step10_summary(third_party_repo_ids())
        ask_reboot()
    except KeyboardInterrupt:
        fatal("interrupted.")
    finally:
        stop_sudo.set()


if __name__ == "__main__":
    main()
