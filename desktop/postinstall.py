#!/usr/bin/env python3
"""postinstall.py - personal software, tooling and performance tweaks for a
Fedora KDE Plasma desktop or laptop.

Start it with the bootstrap (installs git, fetches this folder, runs this
script as your normal user, never as root):

    curl -fsSL https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/desktop/bootstrap.sh | bash
    curl -fsSL .../desktop/bootstrap.sh | bash -s -- --dry-run   # only print what would change

Or from a clone: python3 desktop/postinstall.py. The package lists are the
.txt files in desktop/packages/ (format: see the README there).

Every run starts by detecting the hardware (CPU and GPU maker, laptop or
desktop, Framework board) and only applies what matches. A machine can match
several blocks: AMD CPU, Intel CPU, AMD GPU, Intel GPU, laptop, Framework.

It runs in stages. After each reboot a Konsole window opens on its own once you
log in and carries on where it left off (or just run the same command again):

  Stage 1  Clean base: 24-hour time (system and KDE), full system update,
           firmware updates (listed first, installed after you confirm;
           laptops must be on the charger), Flatpak updates. Reboot.
  Stage 2  Install ("Phase A"): repos, packages, Flatpaks, AI CLIs and SDKs,
           VS Code extensions, Antigravity, Google Drive mount, performance
           tweaks (sysctl, I/O schedulers, noatime, ananicy-cpp, GameMode),
           kernel arguments, GPU tooling, laptop power settings and, on a
           Framework laptop, hibernation. Then a validation pass: every item
           is checked, anything missing is retried once. Reboot.
  Stage 3  Configure and sign in ("Phase B"): checks that kernel arguments and
           services are live, GameMode's AMD GPU settings, variable refresh
           rate, Phoronix Test Suite and MangoHud logging, lid-close hibernate
           (Framework) with a test, then every sign-in, then each app.
  Stage 4  Baseline benchmarks ("Phase C"): runs the list in
           ~/.config/desktop-postinstall/benchmarks.txt once at stock settings
           and saves the results under ~/benchmarks/<machine>/.

Options:
    --with NAME[,NAME]  turn on opt-in extras (remembered); --list-options shows them
    --without NAME      turn one off again
    --stage N           run only stage N (1-4)
    --auth              sign-ins only (skips ones already done)
    --benchmark         stage 4 only
    --check             only the validation pass, including post-reboot checks (read-only)
    --verbose           print every command and its full output live

Everything is logged to ~/postinstall-logs/. Running it twice is harmless:
finished steps are detected and skipped. Every tweak is a separate file that
can be deleted to revert. Only the Python standard library is used.
"""

import argparse
import datetime
import getpass
import grp
import json
import math
import os
import pwd
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urljoin

# ================================================================ settings

SCRIPT = "postinstall.py"
HOME = Path.home()
LOCAL_BIN = HOME / ".local" / "bin"

# ---- opt-in extras: off unless turned on with --with NAME (remembered).
# name: (what it does, hardware block it needs or None)
OPTIONS = {
    "heroic": ("Heroic Games Launcher: Epic, GOG and Amazon games", None),
    "lutris": ("Lutris: launcher for other stores and emulators", None),
    "scx": ("sched_ext CPU schedulers (scx_lavd, scx_bpfland, ...) + scx_loader/scxctl + GUI", None),
    "rt-tests": ("cyclictest: scheduling latency, to compare schedulers", None),
    "fio": ("disk I/O benchmark, to compare I/O schedulers", None),
    "tuned-switcher": ("TuneD Switcher: pick any tuned profile, beyond KDE's three", None),
    "thermald": ("Intel's thermal daemon; test per machine, some laptops run better without it", "intel-cpu"),
    "ryzenadj": ("power-limit tuning for AMD laptop/APU chips", "amd-cpu"),
    "zenpower": ("zenpower3 + zenmonitor3: Ryzen power/voltage readings (third-party COPR, "
                 "unsigned kernel module: needs Secure Boot off; replaces k10temp)", "amd-cpu"),
    "igpu-overclock": ("amdgpu.ppfeaturemask on a laptop's AMD iGPU (re-test sleep after)", "laptop"),
    "framework-tool": ("Framework's framework_tool: battery charge limit, keyboard light, EC status",
                       "framework"),
    "audio-no-powersave": ("stop audio pops/buzz from sound-chip power saving (small battery cost)",
                           "laptop"),
}

# ---- repositories (the software's own publisher, or Fedora / RPM Fusion)

# Written to /etc/yum.repos.d/<name>.repo, exactly as the vendor documents them.
REPO_FILES = {
    "google-chrome": """\
[google-chrome]
name=google-chrome
baseurl=https://dl.google.com/linux/chrome/rpm/stable/x86_64
enabled=1
gpgcheck=1
gpgkey=https://dl.google.com/linux/linux_signing_key.pub
""",
    "vscode": """\
[code]
name=Visual Studio Code
baseurl=https://packages.microsoft.com/yumrepos/vscode
enabled=1
autorefresh=1
type=rpm-md
gpgcheck=1
gpgkey=https://packages.microsoft.com/keys/microsoft.asc
""",
    "google-cloud-cli": """\
[google-cloud-cli]
name=Google Cloud CLI
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el10-x86_64
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key-v10.gpg
""",
}

# The ChatGPT desktop app for Linux includes Codex (and ChatGPT Work). OpenAI
# publishes no key URL: its first RPM installs the signing key and OpenAI's
# signed repo, and later updates come through dnf from that repo.
CHATGPT_RPM = "https://persistent.oaistatic.com/codex-app-prod/linux/rpm/latest/chatgpt.x86_64.rpm"

# .repo files the publisher hosts; downloaded as-is into /etc/yum.repos.d/.
REPO_URLS = {
    "tailscale": "https://pkgs.tailscale.com/stable/fedora/tailscale.repo",
    # Red Hat's signed Windows guest drivers (disk, network, display) for VMs.
    "virtio-win": "https://fedorapeople.org/groups/virt/virtio-win/virtio-win.repo",
}

# COPRs (Fedora's community package build service, like Arch's AUR).
# (name, what, hardware block/option tag or None, only these packages or None)
COPRS = [
    ("lizardbyte/stable", "Sunshine, from its developers (LizardByte)", None, None),
    # CachyOS project's Fedora ports. Limited to these packages so nothing else
    # from it (like cachyos-settings) can replace a Fedora package.
    ("bieszczaders/kernel-cachyos-addons", "ananicy-cpp, CachyOS rules and sched_ext tools", None,
     ["ananicy-cpp", "cachyos-ananicy-rules", "scx-scheds", "scx-tools", "scx-manager"]),
    ("ilyaz/LACT", "LACT, from its developer", "amd-gpu", None),
    ("shdwchn10/zenpower3", "zenpower3 (third-party packager)", "opt:zenpower", None),
]

RPMFUSION = [
    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-{rel}.noarch.rpm",
    "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-{rel}.noarch.rpm",
]

# ---- package lists: read from desktop/packages/*.txt (see the README there).
# Each file loads on its own; a bad line is reported and skipped.
LIST_DIR = Path(__file__).resolve().parent / "packages"
RPM_CORE_FILE = "rpm-core.txt"       # installed first, before any other repo
# Filled in by load_lists():
RPM_GROUPS = []         # [(group, tag, [(package, what)], file)]
OPTIONAL_RPMS = set()   # names marked with "?": fine if missing
SWAPS = []              # [(fedora_package, replacement, tag, what)]
FLATPAKS = []           # [(app_id, what, tag)]
VSCODE_EXTENSIONS = {}  # id -> what
NPM_GLOBALS = {}        # package -> command
UV_TOOLS = {}           # package -> command
AGENT_SDKS = []         # pip packages
AGENT_IMPORTS = []      # import names checked by the validation pass
LIST_ERRORS = []        # problems found while reading the lists

# ---- Antigravity (Google)

# Google publishes Antigravity 2.x (the agent app) and the Antigravity IDE for
# Linux only as tarballs on its download page. Its older RPM repo carried the
# 1.x IDE. Each run reads the download page for the current versions, checks
# the RPM repo too, and installs the IDE from whichever is newer (the app only
# exists as a tarball). Tarballs go under ~/.local/opt (no sudo). They can't
# update themselves: when Antigravity says an update is out, run stage 2 again.
ANTIGRAVITY_PAGE = "https://antigravity.google/download"
ANTIGRAVITY_REPO_URL = "https://us-central1-yum.pkg.dev/projects/antigravity-auto-updater-dev/antigravity-rpm"
ANTIGRAVITY_REPO_FILE = """\
[antigravity-rpm]
name=Antigravity RPM Repository
baseurl=https://us-central1-yum.pkg.dev/projects/antigravity-auto-updater-dev/antigravity-rpm
enabled=1
# Google publishes this repo unsigned (same as their own instructions).
gpgcheck=0
"""
ANTIGRAVITY_DIR = HOME / ".local" / "opt"
ANTIGRAVITY = {
    # file: the tarball's name in Google's URLs. fallback: the newest known URL
    # (2026-09-29), used only if the download page can't be read.
    "antigravity": {
        "label": "Antigravity", "file": r"Antigravity\.tar\.gz", "rpm": None,
        "fallback": "https://storage.googleapis.com/antigravity-public/antigravity-hub/"
                    "2.18.1-4945794252537856/linux-x64/Antigravity.tar.gz"},
    "antigravity-ide": {
        "label": "Antigravity IDE", "file": r"Antigravity(?:%20|\+| )IDE\.tar\.gz", "rpm": "antigravity",
        "fallback": "https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/"
                    "2.5.5-4923483625488384/linux-x64/Antigravity%20IDE.tar.gz"},
}

# Steam used to be a Flatpak here; a leftover one is removed in stage 2.
OLD_STEAM_FLATPAK = "com.valvesoftware.Steam"
OLD_STEAM_LAYERS = ["org.freedesktop.Platform.VulkanLayer.MangoHud",
                    "org.freedesktop.Platform.VulkanLayer.gamescope",
                    "org.freedesktop.Platform.VulkanLayer.vkBasalt",
                    "org.freedesktop.Platform.VulkanLayer.OBSVkCapture"]

FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"

# ---- command-line tools and SDKs (installed in your home, no sudo)

# npm installs "global" packages into ~/.local (so ~/.local/bin), not /usr.
NPM_PREFIX = HOME / ".local"

CLAUDE_INSTALLER = "https://claude.ai/install.sh"


# Shared scratch environment for agent SDK experiments; real projects pin
# their own copies. `agents` in a terminal activates it.
AGENTS_VENV = HOME / ".venvs" / "agents"
AGENTS_PYTHON = "3.13"
# API keys for the SDKs. Kept in a private file and loaded only by `agents`,
# never globally: a global ANTHROPIC_API_KEY (or GEMINI_/OPENAI_) makes the
# Claude/Gemini/Codex CLIs bill that key instead of your subscription.
API_KEYS_FILE = HOME / ".config" / "api-keys.env"
API_KEYS = {
    "ANTHROPIC_API_KEY": "https://console.anthropic.com/settings/keys",
    "GEMINI_API_KEY": "https://aistudio.google.com/apikey",
    "OPENAI_API_KEY": "https://platform.openai.com/api-keys",
}

SHELL_SNIPPET = HOME / ".bashrc.d" / "desktop-postinstall.sh"
SHELL_SNIPPET_CONTENT = f"""\
# Managed by {SCRIPT}.
# User-installed tools (Claude Code, Gemini, Codex, hf, uv tools) live here.
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac

# `agents`: activate the shared agent SDK environment and load your API keys
# into this terminal only. `deactivate` leaves the environment.
agents() {{
    . "$HOME/.venvs/agents/bin/activate" || return
    if [ -f "$HOME/.config/api-keys.env" ]; then
        set -a; . "$HOME/.config/api-keys.env"; set +a
    fi
}}
"""

# ---- 24-hour time (stage 1, first step)

# Locale used only for time/date formats (LC_TIME), system-wide and in KDE;
# the language and everything else stay as installed.
#   en_GB.UTF-8 -> 24-hour time, dates day-first (29/09/2026)
#   en_DK.UTF-8 -> 24-hour time, ISO dates (2026-09-29)
TIME_LOCALE = "en_GB.UTF-8"
CLOCK_APPLETS_FILE = HOME / ".config" / "plasma-org.kde.plasma.desktop-appletsrc"

# ---- performance tweaks (drop-in files: delete one to revert it)

SYSCTL_FILE = "/etc/sysctl.d/99-performance.conf"
# Fedora already sets vm.max_map_count this high; only written if it isn't.
MAX_MAP_COUNT = 1048576
BBR_MODULE_FILE = "/etc/modules-load.d/tcp_bbr.conf"

IOSCHED_RULES_FILE = "/etc/udev/rules.d/60-ioschedulers.rules"
IOSCHED_RULES = f"""\
# Managed by {SCRIPT}. I/O scheduler per disk type (the order disk requests
# are served in). Delete this file to go back to the kernel's defaults.
# NVMe: none (the drive schedules itself).
ACTION=="add|change", KERNEL=="nvme[0-9]*n[0-9]*", ATTR{{queue/scheduler}}="none"
# SATA/eMMC SSD: mq-deadline.
ACTION=="add|change", KERNEL=="sd[a-z]*|mmcblk[0-9]*", ATTR{{queue/rotational}}=="0", ATTR{{queue/scheduler}}="mq-deadline"
# Spinning disk: bfq.
ACTION=="add|change", KERNEL=="sd[a-z]*", ATTR{{queue/rotational}}=="1", ATTR{{queue/scheduler}}="bfq"
"""

FSTAB = "/etc/fstab"
FSTAB_BACKUP = "/etc/fstab.before-postinstall"

ANANICY_CUSTOM_DIR = "/etc/ananicy.d/99-custom"
ANANICY_CUSTOM_README = f"""\
Your own ananicy-cpp rules (created by {SCRIPT}).

This folder loads last, so anything here overrides the CachyOS rules in
00-default/ without editing them (those are updated by the COPR package).

  *.rules    process name -> type, e.g.  {{ "name": "blender", "type": "Heavy_CPU" }}
  *.types    your own named priority profiles (nice, I/O class, ...)
  *.cgroups  CPU budget groups

After changing something: sudo systemctl restart ananicy-cpp
"""

GAMEMODE_INI = "/etc/gamemode.ini"

KWINRC = ("kwinrc", ["Compositing"], "AllowTearing")

# ---- AMD GPU

# RX 6600/6600 XT/6650 XT (Navi 23, gfx1032) and RX 6400/6500 (Navi 24,
# gfx1034) aren't supported by ROCm. They share an instruction set with
# gfx1030 (RX 6800/6900), so ROCm runs them when told to treat them as that.
NAVI_23_24 = re.compile(r"\[1002:(73[ef][0-9a-f]|74[23][0-9a-f])\]", re.I)
OLLAMA_OVERRIDE = "/etc/systemd/system/ollama.service.d/10-gfx-override.conf"
OLLAMA_OVERRIDE_CONTENT = f"""\
# Managed by {SCRIPT}. RX 6600-class GPUs (gfx1032/gfx1034) have no ROCm
# support; run them as gfx1030 (same instruction set). Unofficial, widely used.
[Service]
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
"""
RAMALAMA_CONF = "/etc/ramalama/ramalama.conf"
RAMALAMA_CONF_CONTENT = f"""\
# Managed by {SCRIPT}. This GPU has no ROCm support, so use the Vulkan
# llama.cpp image instead of the ROCm one. Delete this file for the defaults.
[ramalama]
image = "quay.io/ramalama/ramalama:latest"

[ramalama.images]
HIP_VISIBLE_DEVICES = "quay.io/ramalama/ramalama:latest"
"""
# Opt-in override for other ROCm apps (Blender, DaVinci Resolve).
ROCM_PROFILE = "/etc/profile.d/rocm-gfx-override.sh"
ROCM_PROFILE_CONTENT = f"""\
# Managed by {SCRIPT}. Uncomment to run ROCm apps on this RX 6600-class GPU
# as gfx1030 (unofficial). ollama already has this set for its service.
# export HSA_OVERRIDE_GFX_VERSION=10.3.0
"""

# ---- kernel arguments (set with grubby for every installed kernel)
# (argument, tag, why). amd_pstate and the hibernate resume= arguments are
# worked out at run time.
KERNEL_ARGS = [
    ("amdgpu.ppfeaturemask=0xffffffff", "amd-gpu-tuning",
     "unlocks the AMD driver's overclock/undervolt controls, so LACT can tune (not just monitor)"),
    ('acpi_osi="!Windows 2020"', "framework-intel",
     "Framework Intel s2idle fix: keyboard light and power button turn off in suspend, ~1%/hour drain"),
]

# ---- laptop / Framework

# KDE Power Management's power profile per state; KDE switches automatically
# when you unplug or reach low battery. Values are the menu's profiles:
# power-saver, balanced, performance. (Framework only, for now.)
POWERDEVIL_PROFILES = {"AC": "balanced", "Battery": "balanced", "LowBattery": "power-saver"}

AUDIO_MODPROBE = "/etc/modprobe.d/99-audio-no-powersave.conf"
AUDIO_MODPROBE_CONTENT = f"""\
# Managed by {SCRIPT} (--with audio-no-powersave). Stops the pop/buzz when the
# sound chip powers down; costs a little battery. Delete this file to revert.
options snd_hda_intel power_save=0
"""
ZENPOWER_MODPROBE = "/etc/modprobe.d/99-zenpower.conf"
ZENPOWER_MODPROBE_CONTENT = f"""\
# Managed by {SCRIPT} (--with zenpower). zenpower3 replaces k10temp (both read
# the same sensors). Delete this file to go back to k10temp.
blacklist k10temp
"""
FRAMEWORK_TOOL_URL = "https://github.com/FrameworkComputer/framework-system/releases/latest/download/framework_tool"
FRAMEWORK_TOOL = "/usr/local/bin/framework_tool"

# ---- hibernation (Framework laptops; lid close = lowest-drain state)

# What closing the lid does: "hibernate" (RAM saved to disk, full power-off,
# ~0% drain) or "suspend-then-hibernate" (sleep first for fast wake, then
# hibernate after HIBERNATE_DELAY).
LID_ACTION = "hibernate"
HIBERNATE_DELAY = "30min"
SWAP_SUBVOL = Path("/swap")               # own Btrfs subvolume: kept out of snapshots
SWAP_FILE = SWAP_SUBVOL / "swapfile"
SWAP_FSTAB_LINE = f"{SWAP_FILE} none swap defaults,pri=0 0 0"  # after zram (priority 100)
LOGIND_LID = "/etc/systemd/logind.conf.d/10-lid.conf"
SLEEP_CONF = "/etc/systemd/sleep.conf.d/10-hibernate.conf"
SLEEP_CONF_CONTENT = f"""\
# Managed by {SCRIPT}. Used by suspend-then-hibernate: sleep this long first.
[Sleep]
HibernateDelaySec={HIBERNATE_DELAY}
"""
DRACUT_RESUME = "/etc/dracut.conf.d/resume.conf"
DRACUT_RESUME_CONTENT = f"""\
# Managed by {SCRIPT}: lets the boot image find and restore a hibernation image.
add_dracutmodules+=" resume "
"""
# KDE's lid action numbers (powerdevil: Sleep=1, Hibernate=2; SleepMode
# SuspendThenHibernate=3).
KDE_LID = {"hibernate": ("2", None), "suspend-then-hibernate": ("1", "3")}

# ---- services, groups, firewall

# (unit, tag)
SYSTEM_SERVICES = [
    ("tailscaled.service", None),
    ("ollama.service", None),
    ("ananicy-cpp.service", None),
    ("tuned.service", None),
    ("tuned-ppd.service", None),
    ("lactd.service", "amd-gpu"),
    ("thermald.service", "opt:thermald"),
]
USER_SERVICES = ["app-dev.lizardbyte.app.Sunshine.service"]
# libvirt: manage VMs without a password. render/video: GPU compute (ROCm).
# gamemode: lets GameMode change the CPU governor and GPU clocks.
GROUPS = ["libvirt", "render", "video", "gamemode"]
# Opened in the default firewall zone (Fedora's desktop zone already allows
# them; this covers other zones). Sunshine's web UI (47990) stays local-only.
STREAMING_PORTS = {
    "Sunshine": ["47984/tcp", "47989/tcp", "48010/tcp", "47998-48000/udp"],
    "Steam Remote Play": ["27036-27037/tcp", "27031-27036/udp"],
}
SUNSHINE_WEB_UI = "https://localhost:47990"

# ---- Google Drive and Google Docs

# Google Drive as a real folder that every app can use: rclone mounts it at
# ~/GoogleDrive, started with your session by a systemd user service. Files are
# cached locally as you use them (--vfs-cache-mode full), so apps can edit them
# normally. Google Docs/Sheets/Slides show up as .docx/.xlsx/.pptx exports. The
# service is enabled once you sign in (stage 3, or --auth later).
GDRIVE_REMOTE = "gdrive"
GDRIVE_DIR = HOME / "GoogleDrive"
GDRIVE_UNIT = "rclone-gdrive.service"
GDRIVE_UNIT_FILE = HOME / ".config/systemd/user" / GDRIVE_UNIT
GDRIVE_UNIT_CONTENT = f"""\
# Managed by {SCRIPT}. Google Drive at ~/GoogleDrive (rclone remote "{GDRIVE_REMOTE}").
[Unit]
Description=Google Drive at ~/GoogleDrive (rclone)

[Service]
# rclone tells systemd when the mount is ready.
Type=notify
ExecStartPre=/usr/bin/mkdir -p %h/GoogleDrive
ExecStart=/usr/bin/rclone mount {GDRIVE_REMOTE}: %h/GoogleDrive \\
    --vfs-cache-mode full --vfs-cache-max-size 20G --vfs-cache-max-age 720h \\
    --dir-cache-time 1h --poll-interval 1m
ExecStop=/usr/bin/fusermount3 -uz %h/GoogleDrive
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""

# Google's "Google Docs Offline" extension, pre-installed through a Chrome
# policy file. normal_installed: installed for you, but you can still remove
# it. (Any policy file makes Chrome show "Managed by your organization".)
DOCS_OFFLINE_EXTENSION = "ghbmnnjooekpmoecnnnilnnbdlolhkhi"
CHROME_POLICY = "/etc/opt/chrome/policies/managed/desktop-postinstall.json"
CHROME_POLICY_CONTENT = json.dumps({"ExtensionSettings": {DOCS_OFFLINE_EXTENSION: {
    "installation_mode": "normal_installed",
    "update_url": "https://clients2.google.com/service/update2/crx"}}}, indent=2) + "\n"

# Menu entries that open Google apps in their own Chrome window (--app).
GOOGLE_APPS = {
    "google-docs": ("Google Docs", "https://docs.google.com/document/", "x-office-document"),
    "google-sheets": ("Google Sheets", "https://docs.google.com/spreadsheets/", "x-office-spreadsheet"),
    "google-slides": ("Google Slides", "https://docs.google.com/presentation/", "x-office-presentation"),
    "google-gmail": ("Gmail", "https://mail.google.com/", "internet-mail"),
    "google-drive": ("Google Drive (web)", "https://drive.google.com/", "folder-cloud"),
}

# ---- benchmarks (stage 4) and logging

BENCH_ROOT = HOME / "benchmarks"          # one folder per machine
BENCH_LIST = HOME / ".config" / "desktop-postinstall" / "benchmarks.txt"
BENCH_LIST_DEFAULT = """\
# Baseline benchmark list (edit freely). One entry per line:
#   pts/<profile>   a Phoronix Test Suite test, run unattended; results stay local
#   run: <command>  any command; its output is saved as a text file.
#                   {out} is replaced with this run's results folder.
# Lines starting with # are ignored.
pts/compress-7zip
pts/c-ray
run: sysbench cpu --threads=$(nproc) --time=30 run
run: sysbench memory --threads=$(nproc) --time=30 run
run: vkmark
run: glmark2 --off-screen
run: sudo turbostat --quiet --interval 5 --num_iterations 6
run: sensors
"""
PTS_SETTINGS = ["SaveResults=TRUE", "OpenBrowser=FALSE", "UploadResults=FALSE",
                "PromptForTestIdentifier=FALSE", "PromptForTestDescription=FALSE",
                "PromptSaveName=FALSE", "RunAllTestCombinations=TRUE", "Configured=TRUE",
                "AnonymousUsageReporting=FALSE", "AlwaysUploadSystemLogs=FALSE",
                "AllowResultUploadsToOpenBenchmarking=FALSE"]
PTS_CONFIG = HOME / ".phoronix-test-suite" / "user-config.xml"
MANGOHUD_CONF = HOME / ".config" / "MangoHud" / "MangoHud.conf"

# ---- stage 3: apps that need a sign-in, opened one at a time

# (label, .desktop ids to try, what to do there)
SIGNIN_APPS = [
    ("Google Chrome", ["google-chrome"],
     "Sign in to Google and turn on sync. Then, for offline Docs/Sheets/Slides: "
     "drive.google.com > Settings (gear) > Offline > turn it on. For Gmail: Gmail > "
     "Settings > See all settings > Offline."),
    ("VS Code", ["code"],
     "Accounts (bottom left) > Backup and Sync Settings. Then sign in to Claude Code, "
     "Gemini Code Assist, Codex and Cline from their sidebar icons."),
    ("Antigravity", ["google-antigravity"], "Sign in with your Google account."),
    ("Antigravity IDE", ["google-antigravity-ide", "antigravity"], "Sign in with your Google account."),
    ("ChatGPT", ["chatgpt", "ChatGPT"],
     "Sign in to your OpenAI account. Codex is in the app's sidebar (Linux preview)."),
    ("Discord", ["com.discordapp.Discord"], "Sign in."),
    ("Spotify", ["com.spotify.Client"], "Sign in."),
    ("Steam", ["steam"],
     "Sign in. Settings > Compatibility: turn on Steam Play for all titles. "
     "Settings > Remote Play: turn it on."),
    ("Obsidian", ["md.obsidian.Obsidian"], "Open or create a vault; sign in if you use Sync."),
    ("Lychee Slicer", ["io.mango3d.LycheeSlicer"], "Sign in to your Mango3D account."),
    ("OrcaSlicer", ["com.orcaslicer.OrcaSlicer"], "Pick your printers; sign in if you use cloud printing."),
    ("OBS Studio", ["com.obsproject.Studio"], "Run the auto-configuration wizard."),
]

APP_DIRS = [HOME / ".local/share/flatpak/exports/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            HOME / ".local/share/applications",
            Path("/usr/share/applications")]

# Always on the to-do list: things no script on this machine can do.
MANUAL_TODO = [
    "Steam Deck: in Desktop Mode, install Moonlight from Discover, then add it to Steam "
    "(Add a Non-Steam Game) so it opens from Game Mode on the dock.",
    f"Pair Moonlight with Sunshine: open Moonlight on the Deck, pick this PC, and type the "
    f"PIN it shows into Sunshine's web page ({SUNSHINE_WEB_UI} > PIN).",
    "ProtonPlus: install the latest GE-Proton (or Proton-CachyOS) for Steam.",
    "Games: add `gamemoderun %command%` to a game's Steam launch options for GameMode; "
    "`mangohud %command%` for the overlay (Shift_L+F2 starts/stops a CSV log).",
    "Windows VM: download the Windows 11 ISO from microsoft.com. In virt-manager, choose "
    "'Microsoft Windows 11' as the OS (adds UEFI + TPM), and attach "
    "/usr/share/virtio-win/virtio-win.iso as a second CD for the drivers.",
    "Local AI test: `ollama run llama3.2` and `ramalama run llama3.2`; keep whichever is faster.",
    "Antigravity IDE: add extensions from its own store (it doesn't share VS Code's).",
    f"Antigravity updates: when it says a new version is out, run "
    f"`python3 {HOME / '.local/share/desktop-postinstall/postinstall.py'} --stage 2` "
    f"(tarball installs can't update themselves).",
]

# ---- where things go

STATE_DIR = HOME / ".local" / "state" / "desktop-postinstall"
STAGE_FILE = STATE_DIR / "stage"
OPTIONS_FILE = STATE_DIR / "options"
# A copy of this script, so the after-reboot autostart has a fixed path.
INSTALLED_COPY = HOME / ".local" / "share" / "desktop-postinstall" / SCRIPT
AUTOSTART = HOME / ".config" / "autostart" / "desktop-postinstall.desktop"
TODO_FILE = HOME / "Desktop" / "desktop-postinstall-TODO.txt"

LOG_DIR = HOME / "postinstall-logs"
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOG_DIR / f"desktop-postinstall-{STAMP}.log"

DONE = 5   # saved stage after stage 4

# ================================================================ state

DRY_RUN = False
VERBOSE = False
LOG = None            # open log file handle
FAILURES = []         # things that went wrong but didn't stop the script
SKIPPED = []          # things skipped on purpose (with the reason)
NOTES = []            # reminders for the summary
TODO = []             # skipped sign-ins and follow-ups, for the Desktop to-do file
FACTS = {}            # values collected along the way
BLOCKS = set()        # hardware blocks this machine matches
ENABLED = set()       # opt-in extras turned on with --with

# ================================================================ output


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


def failed(what, detail=None):
    """Record a non-fatal failure; the script keeps going. All of them are
    listed at the end with the reason: `detail`, or by default the error text
    of the command that just failed (dnf's, npm's, ...)."""
    if detail is None:
        detail = FACTS.pop("last_error", "")
    FAILURES.append((what, detail.strip()))
    say(f"   FAILED: {what}")
    for line in detail.strip().splitlines()[:6]:
        say(f"      {line}")


def error_text(result, lines=8):
    """The most useful lines of a failed command's output."""
    out = (result.stdout + "\n" + result.stderr).strip().splitlines()
    keep = [l for l in out if re.search(r"error|fail|no match|not found|conflict|problem|nothing provides", l, re.I)]
    return "\n".join((keep or out)[-lines:])


def skipped(what):
    SKIPPED.append(what)
    say(f"   skipped: {what}")


def fatal(msg):
    """Stop the whole script. Only for problems that make continuing pointless."""
    say(f"\nFATAL: {msg}")
    if LOG:
        say(f"Log: {LOG_FILE}")
    sys.exit(1)


def banner(title):
    say("\n" + "=" * 64)
    say(title)
    say("=" * 64)


def section(title):
    say(f"-- {title}")


def run_tasks(title, tasks):
    """Run a stage's steps in order with a progress bar."""
    banner(title)
    total = len(tasks)
    for i, (label, fn) in enumerate(tasks, 1):
        filled = round(24 * i / total)
        say(f"\n[{'█' * filled}{'░' * (24 - filled)}] {i}/{total}  {label}")
        fn()


HEARTBEAT_SECS = 60   # "still running" notice after this long without output


def heartbeat(shown, started, last_output, stop):
    """Background thread: while a command runs, say so every HEARTBEAT_SECS
    it stays quiet, with how long it's been. Shows where a run is stuck."""
    while not stop.wait(15):
        quiet = datetime.datetime.now() - last_output[0]
        if quiet.total_seconds() >= HEARTBEAT_SECS:
            mins = int((datetime.datetime.now() - started).total_seconds() // 60)
            say(f"   ... still running ({mins} min, no output for {int(quiet.total_seconds())} s): {shown[:90]}")
            last_output[0] = datetime.datetime.now()


def run(cmd, changes_system=True, input_text=None, env=None, live=False):
    """Run and log one command. Returns a CompletedProcess.

    changes_system=True: the command modifies something. With --dry-run it is
    only printed. changes_system=False: a read-only lookup (rpm -q, dnf info,
    flatpak info, ...). Those run even in a dry run so the preview can make the
    same decisions a real run would.

    Output always goes to the log, line by line as it arrives (so
    `tail -f` on the log shows a running command). The terminal shows it live
    with --verbose, or for live=True commands; otherwise only the tail if the
    command fails. A command that goes quiet gets a "still running" notice.
    """
    shown = shlex.join(cmd)
    if DRY_RUN and changes_system:
        say(f"   [dry-run] {shown}")
        if input_text:
            say("   [dry-run]   with this content:")
            for line in input_text.splitlines():
                say(f"   [dry-run]     {line}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    live = live or (VERBOSE and changes_system)
    started = datetime.datetime.now()
    if live or (VERBOSE and changes_system):
        say(f"   $ {shown}")
    else:
        log(f"$ {shown}  [{started:%H:%M:%S}]")
    last_output = [started]
    stop = threading.Event()
    if changes_system:
        threading.Thread(target=heartbeat, args=(shown, started, last_output, stop), daemon=True).start()
    try:
        if input_text is not None:
            result = subprocess.run(cmd, input=input_text, capture_output=True, text=True,
                                    errors="replace", env=env)
            for line in (result.stdout + result.stderr).splitlines():
                log(f"  | {line}")
        else:
            # Empty stdin: nothing can sit waiting for an answer (a question
            # gets end-of-input instead). Output is read as it comes; \r
            # progress bars arrive as separate lines.
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace", env=env)
            lines = []
            for line in proc.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                last_output[0] = datetime.datetime.now()
                if live:
                    say(f"      | {line}")
                else:
                    log(f"  | {line}")
            result = subprocess.CompletedProcess(cmd, proc.wait(), "\n".join(lines), "")
    except FileNotFoundError as err:
        result = subprocess.CompletedProcess(cmd, 127, "", str(err))
    finally:
        stop.set()
    secs = int((datetime.datetime.now() - started).total_seconds())
    log(f"(exit code {result.returncode}, {secs} s)")
    if changes_system:
        FACTS["last_error"] = error_text(result) if result.returncode != 0 else ""
    if result.returncode != 0 and changes_system:
        say(f"   command failed: {shown}")
        tail = (result.stdout + result.stderr).strip().splitlines()[-12:]
        for line in tail:
            say(f"      {line}")
    return result


def output_of(cmd):
    """Run a read-only command and return its stripped stdout ('' on failure)."""
    r = run(cmd, changes_system=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def succeeds(cmd):
    """True if a read-only command exits 0."""
    return run(cmd, changes_system=False).returncode == 0


def interactive(cmd, env=None):
    """Run a command attached to the terminal (logins, prompts). Returns True on exit 0."""
    shown = shlex.join(cmd)
    if DRY_RUN:
        say(f"   [dry-run] {shown}")
        return True
    log(f"$ {shown}")
    try:
        rc = subprocess.run(cmd, env=env).returncode
    except FileNotFoundError:
        rc = 127
    log(f"(exit code {rc})")
    return rc == 0


def ask(prompt, default=""):
    """input() that treats a closed terminal (EOF) as the default answer."""
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""
    log(f"{prompt}{answer!r}")
    return answer or default


def yes(prompt, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{prompt} {hint} ").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def read_sys(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


# ================================================================ package lists

NAME_OK = re.compile(r"^[@A-Za-z0-9._+:/-]+$")
KNOWN_BLOCKS = {"amd-cpu", "intel-cpu", "amd-gpu", "intel-gpu", "laptop", "desktop", "framework",
                "framework-intel", "framework-amd", "hibernate", "amd-gpu-tuning"}


def read_list(filename, fields):
    """Parse one list file. Returns [(group, tag, values, what, optional)].
    A bad line or header is recorded in LIST_ERRORS (file:line) and skipped;
    the rest of the file still loads."""
    path = LIST_DIR / filename
    try:
        lines = path.read_text().splitlines()
    except OSError as err:
        LIST_ERRORS.append(f"{filename}: can't read it ({err.strerror})")
        return []
    entries, group, tag, bad_group = [], filename, None, False
    for n, raw in enumerate(lines, 1):
        body, _, what = raw.partition("#")
        body, what = body.strip(), what.strip()
        if not body:
            continue
        where = f"{filename}:{n}"
        if body.startswith("["):
            m = re.fullmatch(r"\[\s*([^\]|]+?)\s*(?:\|\s*([^\]]*?)\s*)?\]", body)
            tag = (m.group(2) or None) if m else None
            if not m:
                LIST_ERRORS.append(f"{where}: can't read this group header: {raw.strip()}")
                bad_group = True
            elif tag and tag not in KNOWN_BLOCKS and not (tag.startswith("opt:") and tag[4:] in OPTIONS):
                LIST_ERRORS.append(f"{where}: unknown block or option '{tag}' (group skipped)")
                bad_group = True
            else:
                group, bad_group = m.group(1), False
            continue
        if bad_group:
            continue
        values = body.split()
        optional = any(v.endswith("?") for v in values)
        values = [v.rstrip("?") for v in values]
        if len(values) != fields:
            LIST_ERRORS.append(f"{where}: expected {fields} value(s), found {len(values)}: {raw.strip()}")
            continue
        if not all(NAME_OK.match(v) for v in values):
            LIST_ERRORS.append(f"{where}: unexpected characters: {raw.strip()}")
            continue
        entries.append((group, tag, values, what, optional))
    return entries


def load_lists():
    """Read every list in desktop/packages/ into the globals above."""
    if not LIST_DIR.is_dir():
        fatal(f"the package lists aren't next to this script ({LIST_DIR}).\n"
              "Get the whole folder with the bootstrap:\n"
              "  curl -fsSL https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/desktop/bootstrap.sh | bash")
    rpm_files = [RPM_CORE_FILE] + sorted(f.name for f in LIST_DIR.glob("rpm-*.txt") if f.name != RPM_CORE_FILE)
    for filename in rpm_files:
        groups = {}
        for group, tag, (name,), what, optional in read_list(filename, 1):
            groups.setdefault((group, tag), []).append((name, what))
            if optional:
                OPTIONAL_RPMS.add(name)
        RPM_GROUPS.extend((g, t, pkgs, filename) for (g, t), pkgs in groups.items())
    for _, tag, (old, new), what, optional in read_list("swaps.txt", 2):
        SWAPS.append((old, new, tag, what))
        if optional:
            OPTIONAL_RPMS.update((old, new))
    for _, tag, (app,), what, _ in read_list("flatpak.txt", 1):
        FLATPAKS.append((app, what or app, tag))
    for _, _, (ext,), what, _ in read_list("vscode.txt", 1):
        VSCODE_EXTENSIONS[ext] = what or ext
    for _, _, (pkg, cmd), _, _ in read_list("npm.txt", 2):
        NPM_GLOBALS[pkg] = cmd
    for _, _, (pkg, cmd), _, _ in read_list("uv-tools.txt", 2):
        UV_TOOLS[pkg] = cmd
    for _, _, (pkg, mod), _, _ in read_list("agent-sdks.txt", 2):
        AGENT_SDKS.append(pkg)
        AGENT_IMPORTS.append(mod)
    count = sum(len(p) for _, _, p, _ in RPM_GROUPS) + len(SWAPS) + len(FLATPAKS) + \
        len(VSCODE_EXTENSIONS) + len(NPM_GLOBALS) + len(UV_TOOLS) + len(AGENT_SDKS)
    say(f"Package lists: {count} entries from {LIST_DIR}")
    for err in LIST_ERRORS:
        say(f"   LIST PROBLEM {err}")


# ================================================================ startup checks

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
    FACTS["version"] = info.get("VERSION_ID", "")
    say(f"System: {FACTS['release']} (kernel {os.uname().release})")
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
    say("\nThis script needs your sudo password once for system-level steps.")
    # Read-only lookups (dnf info, grubby --info) use sudo too, so this also
    # happens in a dry run.
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        fatal("sudo -v failed (wrong password, or no sudo rights).")
    stop = threading.Event()
    threading.Thread(target=keep_sudo_alive, args=(stop,), daemon=True).start()
    return stop


def stay_awake():
    """Block sleep while the script runs: a sleeping machine freezes every
    install and benchmark (and spoils benchmark timings). systemd-inhibit
    makes logind refuse sleep; kde-inhibit also stops KDE's idle sleep from
    starting. The screen can still lock and turn off. Returns the helper
    processes; ending them lifts the block (done at exit)."""
    helpers = []
    if DRY_RUN:
        return helpers
    for cmd in (["systemd-inhibit", "--what=sleep:idle", "--who=desktop post-install",
                 "--why=Installing and benchmarking; sleep would interrupt it", "--mode=block",
                 "sleep", "infinity"],
                ["kde-inhibit", "--power", "sleep", "infinity"]):
        if shutil.which(cmd[0]):
            try:
                helpers.append(subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, start_new_session=True))
            except OSError:
                pass
    if helpers:
        say("Sleep is blocked while this runs (the screen can still lock and turn off).")
    return helpers


def let_sleep(helpers):
    for proc in helpers:
        proc.terminate()


def username():
    return pwd.getpwuid(os.getuid()).pw_name


# ================================================================ hardware detection

def battery_dirs():
    """Laptop batteries (BAT0, BAT1, ...). Mice and headsets report batteries
    too, under other names, so they don't count."""
    return sorted(Path("/sys/class/power_supply").glob("BAT*"))


def drm_cards():
    """[(index, driver, pci_address, vram_bytes)] for each GPU (/sys/class/drm/cardN)."""
    cards = []
    for card in sorted(Path("/sys/class/drm").glob("card*")):
        m = re.fullmatch(r"card(\d+)", card.name)
        if not m:
            continue  # connectors like card0-DP-1
        try:
            driver = Path(os.readlink(card / "device" / "driver")).name
        except OSError:
            continue
        pci = Path(os.path.realpath(card / "device")).name
        vram = read_sys(card / "device" / "mem_info_vram_total")
        cards.append((int(m.group(1)), driver, pci, int(vram) if vram.isdigit() else 0))
    return cards


def detect_hardware():
    """Work out which hardware blocks apply. Read-only; runs on every start."""
    banner("Hardware")
    dmi = Path("/sys/class/dmi/id")
    cpuinfo = read_sys("/proc/cpuinfo")
    m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    flags = re.search(r"^flags\s*:\s*(.+)$", cpuinfo, re.M)
    cpu_vendor = ("amd" if "AuthenticAMD" in cpuinfo else
                  "intel" if "GenuineIntel" in cpuinfo else "other")
    cards = drm_cards()
    drivers = {c[1] for c in cards}
    batteries = battery_dirs()
    vendor = read_sys(dmi / "sys_vendor")
    product = read_sys(dmi / "product_name")
    virt = run(["systemd-detect-virt"], changes_system=False).stdout.strip() or "none"
    gpu_names = []
    for idx, driver, pci, _ in cards:
        name = output_of(["lspci", "-s", pci]) or pci
        gpu_names.append(f"card{idx}: {name.split(': ', 1)[-1]} [{driver}]")

    BLOCKS.clear()
    BLOCKS.add(f"{cpu_vendor}-cpu")
    if "amdgpu" in drivers:
        BLOCKS.add("amd-gpu")
    if drivers & {"i915", "xe"}:
        BLOCKS.add("intel-gpu")
    BLOCKS.add("laptop" if batteries else "desktop")
    if vendor.strip().lower().startswith("framework"):
        BLOCKS.add("framework")
        BLOCKS.add(f"framework-{cpu_vendor}")
    if "laptop" in BLOCKS and "framework" in BLOCKS and virt == "none":
        # Secure Boot stays on; with it on, kernel lockdown blocks hibernation.
        if secure_boot_on():
            FACTS["secure_boot"] = True
        else:
            BLOCKS.add("hibernate")
    if "amd-gpu" in BLOCKS and ("desktop" in BLOCKS or "igpu-overclock" in ENABLED):
        BLOCKS.add("amd-gpu-tuning")

    FACTS["hw"] = {
        "vendor": vendor, "model": product, "cpu": m.group(1).strip() if m else "unknown",
        "cpu_vendor": cpu_vendor, "cpu_flags": flags.group(1).split() if flags else [],
        "hybrid": Path("/sys/devices/cpu_core").exists(),
        "cards": cards, "gpus": gpu_names, "batteries": batteries, "virt": virt,
    }
    say(f"Machine: {vendor or '?'} {product}".rstrip()
        + (f" (virtual: {virt})" if virt != "none" else ""))
    say(f"CPU:     {FACTS['hw']['cpu']}"
        + (" (hybrid P/E cores)" if FACTS["hw"]["hybrid"] else ""))
    for line in gpu_names or ["none found"]:
        say(f"GPU:     {line}")
    if "nvidia" in drivers or "nouveau" in drivers:
        say("         (NVIDIA GPU found: this script never installs NVIDIA packages.)")
    if FACTS.get("secure_boot"):
        say("Secure Boot: on (hibernation not available; lid close = sleep)")
    if batteries:
        say(f"Power:   laptop, {'charger connected' if on_ac_power() else 'on battery'}"
            + (f", battery {battery_percent()}%" if battery_percent() is not None else ""))
    if "framework" in BLOCKS:
        gen = re.search(r"(\d+)(?:st|nd|rd|th) Gen", product)
        FACTS["framework_board"] = (f"{gen.group(1)}th gen Intel" if gen and cpu_vendor == "intel"
                                    else "AMD" if cpu_vendor == "amd" else product)
        say(f"Framework board: {FACTS['framework_board']}")
    say(f"Blocks:  {', '.join(sorted(BLOCKS))}")
    if ENABLED:
        say(f"Extras:  {', '.join(sorted(ENABLED))}")


def wanted(tag):
    """Does this item apply here? None = everywhere; a block name; or opt:NAME."""
    if not tag:
        return True
    if tag.startswith("opt:"):
        name = tag[4:]
        need = OPTIONS[name][1]
        return name in ENABLED and (not need or need in BLOCKS)
    return tag in BLOCKS


def on_ac_power():
    """True on a desktop, or when a laptop's charger is connected."""
    if not FACTS.get("hw", {}).get("batteries", battery_dirs()):
        return True
    for d in Path("/sys/class/power_supply").glob("*"):
        if read_sys(d / "type") in ("Mains", "USB") and read_sys(d / "online") == "1":
            return True
    return False


def battery_percent():
    bats = FACTS.get("hw", {}).get("batteries") or battery_dirs()
    levels = [int(v) for v in (read_sys(b / "capacity") for b in bats) if v.isdigit()]
    return min(levels) if levels else None


def ensure_ac_power(what):
    """Laptops: don't start long updates, flashing or benchmarks on battery."""
    if on_ac_power():
        return
    if DRY_RUN:
        say(f"   [dry-run] on battery: a real run asks you to plug in before {what}")
        return
    warn(f"this laptop is on battery. Plug in the charger before {what}.")
    while not on_ac_power():
        if ask("   Press Enter once it's plugged in (s = continue on battery): ").lower() == "s":
            warn(f"continuing {what} on battery")
            return
    say("   Charger connected.")


# ================================================================ stage bookkeeping

def saved_stage():
    """1-4, or DONE. A missing file means a fresh start."""
    try:
        text = STAGE_FILE.read_text().strip()
    except OSError:
        return 1
    return int(text) if text.isdigit() and 1 <= int(text) <= DONE else 1


def save_stage(n):
    if DRY_RUN:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STAGE_FILE.write_text(f"{n}\n")


def load_options():
    try:
        return {o for o in OPTIONS_FILE.read_text().split() if o in OPTIONS}
    except OSError:
        return set()


def save_options(opts):
    if DRY_RUN:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OPTIONS_FILE.write_text("".join(f"{o}\n" for o in sorted(opts)))


def install_copy():
    """Copy this script and its package lists to a fixed place for the
    after-reboot autostart."""
    me = Path(__file__).resolve()
    if me == INSTALLED_COPY.resolve() or DRY_RUN:
        return
    INSTALLED_COPY.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(me, INSTALLED_COPY)
    lists = INSTALLED_COPY.parent / "packages"
    shutil.rmtree(lists, ignore_errors=True)  # so lists removed upstream go too
    shutil.copytree(LIST_DIR, lists)


def set_autostart(on):
    """KDE starts everything in ~/.config/autostart at login. The entry opens
    Konsole running this script, which continues from the saved stage."""
    if DRY_RUN:
        return
    if not on:
        AUTOSTART.unlink(missing_ok=True)
        return
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(f"""\
[Desktop Entry]
Type=Application
Name=Desktop post-install (continues after reboot)
Exec=konsole --hold -e python3 {INSTALLED_COPY}
X-KDE-autostart-phase=2
""")


def ask_reboot(what_next):
    """Offer the reboot; the autostart continues with `what_next` after login."""
    set_autostart(True)
    say(f"\nAfter the reboot, log in and {what_next} starts by itself in a Konsole window.")
    say(f"(Or run it yourself any time: python3 {INSTALLED_COPY})")
    if DRY_RUN:
        say("   [dry-run] would ask 'Reboot now? [Y/n]' and on yes run: sudo systemctl reboot")
        return
    if yes("Reboot now?"):
        run(["sudo", "systemctl", "reboot"])
    else:
        say("Not rebooting yet. Reboot with: sudo systemctl reboot")


# ================================================================ package helpers

ARCH = re.compile(r"\.(i686|x86_64|noarch)$")


def package_available(name):
    """True if dnf can find this package (installed or in an enabled repo).

    `dnf info` only matches package names. Some names are provided by another
    package (`npm` comes from nodejs-npm), so fall back to --whatprovides.
    Names starting with @ are package groups.
    """
    if name.startswith("@"):
        return succeeds(["sudo", "dnf", "-q", "group", "info", name[1:]])
    if succeeds(["sudo", "dnf", "-q", "info", name]):
        return True
    r = run(["sudo", "dnf", "-q", "repoquery", "--whatprovides", name], changes_system=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def rpm_installed(name):
    if name.startswith("@"):
        # Group installs are recorded by dnf; check the group's key packages.
        return name == "@virtualization" and succeeds(["rpm", "-q", "virt-manager", "qemu-kvm"])
    if ARCH.search(name):
        return succeeds(["rpm", "-q", name])  # e.g. mesa-va-drivers.i686
    return succeeds(["rpm", "-q", "--whatprovides", name])


def install_packages(names, label):
    """Install a group of packages. Missing ones are skipped (quietly if
    optional). If the transaction fails, retry one by one to isolate the bad one."""
    missing = [name for name in names if not rpm_installed(name)]
    if not missing:
        say(f"   {label}: already installed")
        return
    wanted_names = []
    for name in missing:
        # A dry run doesn't add the repos, so vendor packages can't be found yet.
        if DRY_RUN or package_available(name):
            wanted_names.append(name)
        elif name in OPTIONAL_RPMS:
            skipped(f"{name}: not in this Fedora release's repositories")
        else:
            failed(f"{name} ({label}): not found in any enabled repository",
                   "Check the spelling in the package list, or that its repo was added.")
    if not wanted_names:
        return
    say(f"   {label}: installing {', '.join(wanted_names)}")
    if run(["sudo", "dnf", "install", "-y", *wanted_names]).returncode == 0:
        return
    warn(f"installing '{label}' as a group failed; retrying one package at a time")
    for name in wanted_names:
        r = run(["sudo", "dnf", "install", "-y", name])
        if r.returncode != 0:
            failed(f"install {name} ({label})", error_text(r))


def swap_package(old, new, what):
    """Replace a Fedora package with its RPM Fusion build (or just install it)."""
    if rpm_installed(new):
        say(f"   {new}: already installed")
        return
    if not DRY_RUN and not package_available(new):
        if new in OPTIONAL_RPMS:
            skipped(f"{new}: not in this Fedora release's repositories")
        else:
            failed(f"{new}: not found in any enabled repository",
                   "Check swaps.txt, or that RPM Fusion is enabled.")
        return
    say(f"   {new}: {what}")
    if rpm_installed(old):
        cmd = ["sudo", "dnf", "swap", "-y", old, new, "--allowerasing"]
    else:
        cmd = ["sudo", "dnf", "install", "-y", new]
    r = run(cmd)
    if r.returncode != 0:
        failed(f"swap {old} -> {new}", error_text(r))


def write_root_file(path, content, mode="644"):
    """Write a root-owned file via `sudo tee`, only if its content differs.
    Returns True if the file now has this content."""
    try:
        if Path(path).read_text() == content:
            say(f"   {path}: already up to date")
            return True
    except OSError:
        pass  # doesn't exist yet (or unreadable): write it
    if not DRY_RUN:
        say(f"   writing {path}")
    run(["sudo", "mkdir", "-p", os.path.dirname(path)])
    # tee copies stdin into the file; its own stdout copy is just discarded.
    if run(["sudo", "tee", path], input_text=content).returncode != 0:
        failed(f"write {path}")
        return False
    if run(["sudo", "chmod", mode, path]).returncode != 0:
        failed(f"chmod {mode} {path}")
        return False
    return True


def write_user_file(path, content, mode=0o644):
    path = Path(path)
    try:
        if path.read_text() == content and (path.stat().st_mode & 0o777) == mode:
            return
    except OSError:
        pass
    if DRY_RUN:
        say(f"   [dry-run] would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with the final mode so a private file is never briefly readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, mode)
    say(f"   wrote {path}")


def unit_exists(unit, user=False):
    cmd = ["systemctl"] + (["--user"] if user else []) + ["list-unit-files", "--no-legend", unit]
    r = run(cmd, changes_system=False)
    return r.returncode == 0 and unit in r.stdout


def unit_enabled(unit, user=False):
    cmd = ["systemctl"] + (["--user"] if user else []) + ["is-enabled", unit]
    return output_of(cmd) in ("enabled", "alias")


def unit_active(unit):
    return output_of(["systemctl", "is-active", unit]) == "active"


def kread(file, groups, key):
    cmd = ["kreadconfig6", "--file", file]
    for g in groups:
        cmd += ["--group", g]
    return output_of(cmd + ["--key", key])


def kwrite(file, groups, key, value):
    """Set a KDE config value with kwriteconfig6 (skipped if already set)."""
    if kread(file, groups, key) == value:
        return False
    cmd = ["kwriteconfig6", "--file", file]
    for g in groups:
        cmd += ["--group", g]
    if run(cmd + ["--key", key, value]).returncode != 0:
        failed(f"set {file} [{']['.join(groups)}] {key}")
        return False
    say(f"   {file} [{']['.join(groups)}] {key}={value}")
    return True


# ================================================================ kernel arguments

def norm_args(text):
    """Kernel arguments with quotes/backslashes removed, for comparisons."""
    return " " + re.sub(r'[\\"]', "", text) + " "


def default_kernel_args():
    # -n: never prompt (the read-only --check runs without the sudo prompt).
    out = output_of(["sudo", "-n", "grubby", "--info=DEFAULT"])
    m = re.search(r'^args="?(.*?)"?$', out, re.M)
    return m.group(1) if m else ""


def ensure_kernel_arg(arg, why):
    """Add `arg` to every installed kernel (grubby). An existing argument with
    the same name but another value is replaced. Takes effect after a reboot."""
    key = arg.split("=", 1)[0]
    current = norm_args(default_kernel_args())
    if norm_args(arg).strip() and norm_args(arg) in current:
        say(f"   {arg}: already set")
        return
    say(f"   {arg}: {why}")
    if re.search(rf"\s{re.escape(key)}=", current):
        run(["sudo", "grubby", "--update-kernel=ALL", f"--remove-args={key}"])
    if run(["sudo", "grubby", "--update-kernel=ALL", f"--args={arg}"]).returncode != 0:
        failed(f"kernel argument {arg}")
        return
    FACTS["kernel_args_changed"] = True


def expected_kernel_args():
    """Kernel arguments this machine should have after stage 2."""
    args = [a for a, tag, _ in KERNEL_ARGS if wanted(tag)]
    if FACTS.get("amd_pstate_arg"):
        args.append("amd_pstate=active")
    return args


# ================================================================ stage 1

def stage1_clean_base():
    run_tasks("Stage 1 of 4: clean base", [
        ("24-hour time", set_24h_time),
        ("System update (dnf)", system_update),
        ("Firmware (fwupd)", firmware_updates),
        ("Flatpak updates", flatpak_updates),
    ])


def norm_locale(name):
    """'en_GB.UTF-8' and 'en_GB.utf8' are the same locale; compare them loosely."""
    return name.lower().replace("-", "")


def read_locale_conf():
    """Current system locale settings from /etc/locale.conf (KEY=value lines)."""
    values = {}
    for line in read_sys("/etc/locale.conf").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            values[key] = value.strip('"')
    return values


def find_clock_applets():
    """Digital Clock widgets in the panel config, as group paths like
    ['Containments', '2', 'Applets', '19']."""
    found, group = [], None
    for line in read_sys(CLOCK_APPLETS_FILE).splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].split("][")
        elif line == "plugin=org.kde.plasma.digitalclock" and group and len(group) == 4:
            found.append(group)
    return found


def time_is_24h():
    return (norm_locale(read_locale_conf().get("LC_TIME", "")) == norm_locale(TIME_LOCALE)
            and norm_locale(kread("plasma-localerc", ["Formats"], "LC_TIME")) == norm_locale(TIME_LOCALE))


def set_24h_time():
    """24-hour time for the command line (system LC_TIME) and KDE (Region &
    Language > Time format, and the panel clock). Only the time/date format
    changes; the language stays."""
    # The locale must exist first; English ones come from glibc-langpack-en.
    if not any(norm_locale(l) == norm_locale(TIME_LOCALE) for l in output_of(["locale", "-a"]).split()):
        install_packages([f"glibc-langpack-{TIME_LOCALE.split('_')[0]}"], "locale data")
        if not DRY_RUN and not any(norm_locale(l) == norm_locale(TIME_LOCALE)
                                   for l in output_of(["locale", "-a"]).split()):
            failed(f"locale {TIME_LOCALE} isn't available, so 24-hour time wasn't set")
            return

    section(f"system time format: LC_TIME={TIME_LOCALE}")
    current = read_locale_conf()
    if norm_locale(current.get("LC_TIME", "")) == norm_locale(TIME_LOCALE):
        say("   already set")
    else:
        # `localectl set-locale` replaces the whole setting, so pass the
        # current values (LANG etc.) back in along with the new LC_TIME.
        current["LC_TIME"] = TIME_LOCALE
        run(["sudo", "localectl", "set-locale", *[f"{k}={v}" for k, v in current.items()]])

    section("KDE: Region & Language > Time format")
    if not kwrite("plasma-localerc", ["Formats"], "LC_TIME", TIME_LOCALE):
        say("   already set")
    # Panel clock: use24hFormat 0 = 12-hour, 1 = follow the region (default),
    # 2 = 24-hour. Only a clock forced to 12-hour needs changing.
    for group in find_clock_applets():
        if kread(CLOCK_APPLETS_FILE.name, group + ["Configuration", "Appearance"], "use24hFormat") == "0":
            kwrite(CLOCK_APPLETS_FILE.name, group + ["Configuration", "Appearance"], "use24hFormat", "2")
    say("   The panel clock switches after the reboot at the end of this stage.")


def system_update():
    say("   A full update on a fresh install can take a while.")
    ensure_ac_power("the system update")
    if run(["sudo", "dnf", "upgrade", "--refresh", "-y"]).returncode != 0:
        fatal("`dnf upgrade` failed. Fix networking/repos and rerun.")
    if not DRY_RUN:
        say("   System is up to date.")


def pending_firmware():
    """[(device, current, new)] from `fwupdmgr get-updates --json`."""
    r = run(["fwupdmgr", "get-updates", "--json"], changes_system=False)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return []  # exit code 2 prints a plain "no updates" message instead
    found = []
    for dev in data.get("Devices", []):
        releases = dev.get("Releases") or [{}]
        found.append((dev.get("Name", "?"), dev.get("Version", "?"), releases[0].get("Version", "?")))
    return found


def firmware_updates():
    """fwupd updates firmware from the LVFS, where vendors (Framework, Dell,
    Lenovo, SSD and dock makers, ...) publish it. The pending list is shown and
    nothing is flashed until you confirm."""
    hw = FACTS["hw"]
    if hw["virt"] != "none":
        skipped(f"firmware: this is a virtual machine ({hw['virt']})")
        return
    # Download the current catalogue first (the one on a fresh install is old).
    run(["sudo", "fwupdmgr", "refresh", "--force"])
    devices = []
    try:
        devices = json.loads(output_of(["fwupdmgr", "get-devices", "--json"]) or "{}").get("Devices", [])
    except ValueError:
        pass
    updatable = [d for d in devices if "updatable" in d.get("Flags", [])]
    say(f"   Devices fwupd can update on this {hw['vendor'] or 'machine'}: {len(updatable)}")
    if "framework" in BLOCKS:
        # Many Framework sleep/battery fixes ship as BIOS and EC firmware.
        for d in devices:
            if re.search(r"System Firmware|Embedded Controller|\bEC\b", d.get("Name", "")):
                say(f"   {d.get('Name')}: {d.get('Version', '?')}")
    pending = pending_firmware()
    if not pending:
        say("   No firmware updates pending." + (" (dry run: the catalogue isn't refreshed, so "
                                                  "a real run may find some)" if DRY_RUN else ""))
        return
    say("   Pending firmware updates:")
    for name, cur, new in pending:
        say(f"     - {name}: {cur} -> {new}")
    if not DRY_RUN and not yes("   Install these firmware updates now?"):
        TODO.append("Firmware updates: fwupdmgr refresh && fwupdmgr update")
        skipped("firmware updates (you chose not to install them now)")
        return
    # fwupd refuses system-firmware updates on battery; check before starting.
    ensure_ac_power("firmware updates")
    # -y: no questions. --no-reboot-check: this script reboots at the end.
    # Some updates (BIOS/UEFI) are only staged now and flashed during the reboot.
    r = run(["sudo", "fwupdmgr", "update", "-y", "--no-reboot-check"])
    if r.returncode not in (0, 2):
        warn("firmware update reported a problem (see log); continuing")
    else:
        NOTES.append("Firmware updates may finish during the reboot: leave the machine "
                     "plugged in and don't power it off while it shows a progress screen.")


def flatpak_updates():
    if run(["sudo", "flatpak", "update", "-y", "--noninteractive"]).returncode != 0:
        warn("flatpak update reported a problem (see log); continuing")
    elif not DRY_RUN:
        say("   Flatpaks are up to date.")


# ================================================================ stage 2

def stage2_install():
    run_tasks("Stage 2 of 4: install (Phase A)", [
        ("Core tools: git, CLI and code tools", install_core),
        ("Repositories", setup_repos),
        ("Codecs for this machine (RPM Fusion)", install_swaps),
        ("Packages", install_rpms),
        ("Old Flatpak Steam cleanup", cleanup_flatpak_steam),
        ("Flatpak apps", install_flatpaks),
        ("AI command-line tools", install_cli_tools),
        ("Agent SDK environment", install_agent_sdks),
        ("Shell setup", install_shell_config),
        ("VS Code extensions", install_vscode_extensions),
        ("Antigravity", install_antigravity),
        ("Google Drive mount and Docs offline", setup_google_drive),
        ("Performance tweaks", performance_tweaks),
        ("CPU and GPU", cpu_gpu_settings),
        ("Laptop and Framework", laptop_settings),
        ("Hibernation (swap file and resume)", hibernate_setup),
        ("Services, groups and firewall", setup_services),
        ("Checking everything", lambda: validate(retry=True, post_reboot=False)),
    ])


def setup_repos():
    rel = FACTS.get("version") or output_of(["rpm", "-E", "%fedora"])
    section("RPM Fusion (free + nonfree)")
    if succeeds(["rpm", "-q", "rpmfusion-free-release", "rpmfusion-nonfree-release"]):
        say("   already enabled")
    elif run(["sudo", "dnf", "install", "-y", *[u.format(rel=rel) for u in RPMFUSION]]).returncode != 0:
        failed("enable RPM Fusion")

    section("vendor repos (Google, Microsoft, Tailscale, virtio-win)")
    for name, content in REPO_FILES.items():
        write_root_file(f"/etc/yum.repos.d/{name}.repo", content)
    for name, url in REPO_URLS.items():
        path = f"/etc/yum.repos.d/{name}.repo"
        if Path(path).exists():
            say(f"   {path}: already present")
            continue
        r = run(["curl", "-fsSL", url], changes_system=False)
        if r.returncode == 0 and "[" in r.stdout:
            write_root_file(path, r.stdout)
        else:
            failed(f"download {url}")

    section("ChatGPT (its RPM adds OpenAI's signed repo)")
    if rpm_installed("chatgpt"):
        say("   already installed")
    elif run(["sudo", "dnf", "install", "-y", CHATGPT_RPM]).returncode != 0:
        failed("install ChatGPT (adds OpenAI's repo)")

    section("COPRs")
    enabled_repos = output_of(["dnf", "repolist", "--enabled"])
    for copr, what, tag, only in COPRS:
        if not wanted(tag):
            continue
        repo_id = f"copr:copr.fedorainfracloud.org:{copr.replace('/', ':')}"
        if repo_id in enabled_repos:
            say(f"   {copr}: already enabled")
        else:
            say(f"   {copr}: {what}")
            if run(["sudo", "dnf", "copr", "enable", "-y", copr]).returncode != 0:
                failed(f"enable COPR {copr}")
                continue
        if only:
            # A dnf drop-in (repos.override.d), not an edit of the repo file.
            run(["sudo", "dnf", "config-manager", "setopt", f"{repo_id}.includepkgs={','.join(only)}"])

    section("refreshing package lists")
    if run(["sudo", "dnf", "makecache", "--refresh", "-y"]).returncode != 0:
        failed("dnf makecache")

    section("Flathub (for your user)")
    if run(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_URL]).returncode != 0:
        failed("add Flathub")


def install_swaps():
    todo = [(o, n, w) for o, n, tag, w in SWAPS if wanted(tag)]
    if not todo:
        say("   Nothing for this hardware.")
    for old, new, what in todo:
        swap_package(old, new, what)


def rpm_groups(core):
    """(label, [names]) for each package group that applies to this machine:
    the core file's groups, or all the others."""
    return [(label, [name for name, _ in pkgs]) for label, tag, pkgs, file in RPM_GROUPS
            if wanted(tag) and (file == RPM_CORE_FILE) == core]


def install_core():
    """git, the CLI and code tools, from Fedora: the rest of the setup uses them."""
    for label, names in rpm_groups(core=True):
        install_packages(names, label)


def install_rpms():
    for label, names in rpm_groups(core=False):
        install_packages(names, label)


def flatpak_refs(app_ids):
    """Installed user refs (app/runtime/branch) for these IDs."""
    refs = output_of(["flatpak", "list", "--user", "--all", "--columns=ref"]).split()
    return [r for r in refs if r.split("/")[1 if r.count("/") >= 3 else 0] in app_ids]


def cleanup_flatpak_steam():
    """Steam is now RPM Fusion's package. If the Flatpak Steam from an earlier
    run is here, remove it, its Vulkan layers and its sandbox permission, then
    the runtimes nothing uses any more (Flatpak's own 32-bit GL and i386
    compatibility libraries). Your Flatpak Steam data (~/.var/app/...) stays."""
    refs = flatpak_refs([OLD_STEAM_FLATPAK, *OLD_STEAM_LAYERS])
    if not refs:
        say("   No Flatpak Steam here; nothing to clean up.")
        return
    say(f"   Removing {len(refs)} Flatpak Steam item(s): {', '.join(r.split('/')[1] for r in refs)}")
    r = run(["flatpak", "uninstall", "--user", "-y", "--noninteractive", *refs])
    if r.returncode != 0:
        failed("remove the Flatpak Steam", error_text(r))
        return
    run(["flatpak", "override", "--user", "--reset", OLD_STEAM_FLATPAK])
    say("   Removing Flatpak runtimes nothing uses any more (incl. its 32-bit libraries)")
    run(["flatpak", "uninstall", "--user", "-y", "--noninteractive", "--unused"])
    old_data = HOME / ".var/app" / OLD_STEAM_FLATPAK
    if old_data.exists():
        NOTES.append(f"The Flatpak Steam's games and settings are still in {old_data}. In Steam: "
                     "Settings > Storage > Add Drive to reuse its steamapps folder, or delete it.")


def flatpak_installed(app_id, branch=None):
    ref = f"{app_id}//{branch}" if branch else app_id
    return succeeds(["flatpak", "info", "--user", ref])


def install_flatpaks():
    apps = [(a, what) for a, what, tag in FLATPAKS if wanted(tag)]
    missing = [(a, what) for a, what in apps if not flatpak_installed(a)]
    if not missing:
        say("   All installed.")
        return
    say(f"   Installing: {', '.join(what.split(':')[0] for _, what in missing)}")
    cmd = ["flatpak", "install", "--user", "-y", "--noninteractive", "flathub"]
    if run(cmd + [a for a, _ in missing]).returncode == 0:
        return
    warn("the batch install failed; retrying one app at a time")
    for app, _ in missing:
        r = run(cmd + [app])
        if r.returncode != 0:
            failed(f"flatpak {app}", error_text(r))


def tool_env():
    """Environment for user-level installs: ~/.local/bin first on PATH."""
    env = dict(os.environ)
    env["PATH"] = f"{LOCAL_BIN}:{env.get('PATH', '')}"
    return env


def have_tool(cmd):
    return shutil.which(cmd, path=tool_env()["PATH"]) is not None


def install_cli_tools():
    env = tool_env()
    section("Claude Code (Anthropic's installer, updates itself)")
    if have_tool("claude"):
        say("   already installed")
    elif run(["bash", "-c", f"set -o pipefail; curl -fsSL {CLAUDE_INSTALLER} | bash"], env=env).returncode != 0:
        failed("install Claude Code")

    section(f"Gemini CLI and Codex CLI (npm, into {NPM_PREFIX})")
    run(["npm", "config", "set", "prefix", str(NPM_PREFIX)], env=env)
    missing = [p for p, cmd in NPM_GLOBALS.items() if not have_tool(cmd)]
    for pkg in missing:
        say(f"   installing {pkg}")
        if run(["npm", "install", "-g", pkg], env=env).returncode != 0:
            failed(f"npm {pkg}")
    if not missing:
        say("   already installed")

    section("Hugging Face CLI (hf)")
    for pkg, cmd in UV_TOOLS.items():
        if have_tool(cmd):
            say(f"   {cmd}: already installed")
        elif run(["uv", "tool", "install", pkg], env=env).returncode != 0:
            failed(f"uv tool {pkg}")


def install_agent_sdks():
    env = tool_env()
    python = AGENTS_VENV / "bin" / "python"
    say(f"   {AGENTS_VENV}: {', '.join(AGENT_SDKS)}")
    if not python.exists():
        if run(["uv", "venv", "--python", AGENTS_PYTHON, str(AGENTS_VENV)], env=env).returncode != 0:
            failed("create the agent SDK environment")
            return
    if run(["uv", "pip", "install", "--python", str(python), "--upgrade", *AGENT_SDKS],
           env=env).returncode != 0:
        failed("install agent SDKs")
        return
    # Lets VS Code / Jupyter pick "Python (agents)" as a notebook kernel.
    run([str(python), "-m", "ipykernel", "install", "--user", "--name", "agents",
         "--display-name", "Python (agents)"], env=env)


def install_shell_config():
    write_user_file(SHELL_SNIPPET, SHELL_SNIPPET_CONTENT)
    # Fedora's default ~/.bashrc already loads ~/.bashrc.d/*; add it if missing.
    bashrc = HOME / ".bashrc"
    try:
        text = bashrc.read_text()
    except OSError:
        text = ""
    if "bashrc.d" not in text:
        if DRY_RUN:
            say(f"   [dry-run] would add a ~/.bashrc.d loader to {bashrc}")
        else:
            with open(bashrc, "a") as f:
                f.write('\nfor rc in ~/.bashrc.d/*; do [ -f "$rc" ] && . "$rc"; done; unset rc\n')
    say("   `agents` activates the SDK environment in a terminal.")


def installed_vscode_extensions():
    return set(output_of(["code", "--list-extensions"]).lower().split())


def install_vscode_extensions():
    if not shutil.which("code") and not DRY_RUN:
        failed("VS Code extensions: VS Code isn't installed")
        return
    have = installed_vscode_extensions()
    for ext, label in VSCODE_EXTENSIONS.items():
        if ext.lower() in have:
            continue
        say(f"   {label}")
        if run(["code", "--install-extension", ext]).returncode != 0:
            failed(f"VS Code extension {ext}")
    if all(e.lower() in have for e in VSCODE_EXTENSIONS):
        say("   All installed.")


# ---------------------------------------------------------------- Antigravity

def version_key(v):
    """'2.18.1' -> (2, 18, 1) for comparing versions."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def url_version(url):
    """Google's download URLs carry the version: .../2.18.1-4945794252537856/linux-x64/..."""
    m = re.search(r"/(\d+\.\d+\.\d+)-\d+/", unquote(url))
    return m.group(1) if m else ""


def antigravity_published():
    """{product: (version, url)} for the current Linux x64 tarballs, read from
    the JavaScript behind Google's download page (the page builds its download
    buttons from it). Falls back to the newest known URLs if that fails."""
    if "antigravity" in FACTS:
        return FACTS["antigravity"]
    found = {}
    html = output_of(["curl", "-fsSL", "--compressed", "--max-time", "30", ANTIGRAVITY_PAGE])
    bundles = re.findall(r"""(?:src|href)=["']([^"']+\.js)["']""", html)
    bundles.sort(key=lambda b: "main" not in b)  # the app bundle holds the download list
    for bundle in bundles[:8]:
        js = output_of(["curl", "-fsSL", "--compressed", "--max-time", "30",
                        urljoin(ANTIGRAVITY_PAGE, bundle)]).replace("\\/", "/")
        for key, p in ANTIGRAVITY.items():
            for url in re.findall(r"https?://[^\"'\s<>)]+/linux-x64/" + p["file"], js):
                ver = url_version(url)
                if ver and (key not in found or version_key(ver) > version_key(found[key][0])):
                    found[key] = (ver, url)
    for key, p in ANTIGRAVITY.items():
        if key not in found:
            warn(f"couldn't read {p['label']}'s current version from {ANTIGRAVITY_PAGE}; "
                 f"using the newest known ({url_version(p['fallback'])})")
            found[key] = (url_version(p["fallback"]), p["fallback"])
    FACTS["antigravity"] = found
    return found


def antigravity_rpm_version(name):
    """Newest version of `name` in Google's RPM repo, queried without adding
    the repo to the system ('' if it isn't there)."""
    r = run(["dnf", "-q", "repoquery", f"--repofrompath=antigravity-check,{ANTIGRAVITY_REPO_URL}",
             "--repo=antigravity-check", "--latest-limit=1", "--qf", "%{version}\n", name],
            changes_system=False)
    lines = [l for l in r.stdout.split() if re.match(r"\d+\.\d+", l)]
    return lines[-1] if r.returncode == 0 and lines else ""


def antigravity_installed_version(key):
    """Installed version: from the tarball install's .version file, else the RPM."""
    ver = read_sys(ANTIGRAVITY_DIR / key / ".version")
    if ver:
        return ver
    rpm = ANTIGRAVITY[key]["rpm"]
    return output_of(["rpm", "-q", "--qf", "%{VERSION}", rpm]) if rpm else ""


def install_antigravity():
    published = antigravity_published()
    for key, p in ANTIGRAVITY.items():
        ver, url = published[key]
        section(f"{p['label']}: Google's download page has {ver}")
        if p["rpm"]:
            rpm_ver = antigravity_rpm_version(p["rpm"])
            say(f"   Google's RPM repo has {rpm_ver or 'no build'}")
            if rpm_ver and version_key(rpm_ver) >= version_key(ver):
                say("   Using the RPM repo (updates then come with dnf).")
                write_root_file(f"/etc/yum.repos.d/{p['rpm']}.repo", ANTIGRAVITY_REPO_FILE)
                install_packages([p["rpm"]], p["label"])
                continue
        have = antigravity_installed_version(key)
        if have and version_key(have) >= version_key(ver):
            say(f"   {have} is installed: up to date")
            continue
        say(f"   Installing {ver} from Google's tarball" + (f" (replacing {have})" if have else ""))
        install_antigravity_tarball(key, ver, url)


def asar_extract(asar, name, out):
    """Copy one top-level file out of an Electron .asar archive. Layout: a
    4-byte size, the header's size, then the header (JSON listing each file's
    offset and size), then the file data."""
    try:
        with open(asar, "rb") as f:
            _, header_size, _, json_size = struct.unpack("<4I", f.read(16))
            entry = json.loads(f.read(json_size))["files"][name]
            f.seek(8 + header_size + int(entry["offset"]))
            Path(out).write_bytes(f.read(entry["size"]))
        return True
    except (OSError, ValueError, KeyError, struct.error):
        return False


def install_antigravity_tarball(key, ver, url):
    """Unpack Google's tarball to ~/.local/opt/<key>, then add a command in
    ~/.local/bin and a menu entry. The old copy is only removed once the new
    one is in place."""
    p = ANTIGRAVITY[key]
    dest = ANTIGRAVITY_DIR / key
    if DRY_RUN:
        say(f"   [dry-run] download {url}")
        say(f"   [dry-run] unpack it to {dest}, link {LOCAL_BIN / key}, add a menu entry")
        return
    # Unpack next to the destination so the final move is a quick rename.
    ANTIGRAVITY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=ANTIGRAVITY_DIR))
    try:
        archive = tmp / "download.tar.gz"
        if run(["curl", "-fL", "--retry", "3", "-o", str(archive), url]).returncode != 0:
            failed(f"download {p['label']}")
            return
        unpacked = tmp / "unpacked"
        unpacked.mkdir()
        if run(["tar", "-xzf", str(archive), "-C", str(unpacked)]).returncode != 0:
            failed(f"unpack {p['label']}")
            return
        # The launcher is `antigravity` or `antigravity-ide`, one folder down.
        exe = next((f for f in sorted(unpacked.glob(f"*/{key}")) if os.access(f, os.X_OK)), None)
        if not exe:
            failed(f"{p['label']}: no `{key}` launcher inside the tarball")
            return
        app_dir = exe.parent
        (app_dir / ".version").write_text(f"{ver}\n")
        (app_dir / ".source-url").write_text(f"{url}\n")
        if dest.exists():
            dest.rename(tmp / "previous")  # deleted with tmp below
        app_dir.rename(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    link = LOCAL_BIN / key
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(dest / key)
    # The IDE ships its icon as a file; the app keeps it inside resources/app.asar.
    icon = dest / "resources/app/resources/linux/code.png"
    if not icon.exists():
        icon = dest / "icon.png"
        if not icon.exists() and not asar_extract(dest / "resources/app.asar", "icon.png", icon):
            icon = "applications-development"  # generic theme icon
    write_user_file(HOME / ".local/share/applications" / f"google-{key}.desktop", f"""\
[Desktop Entry]
Type=Application
Name={p['label']}
Comment=Google {p['label']} {ver} (installed by {SCRIPT})
Exec={dest / key} %U
Icon={icon}
Terminal=false
Categories=Development;IDE;
""")
    say(f"   Installed {p['label']} {ver} in {dest}")


# ---------------------------------------------------------------- Google Drive

def setup_google_drive():
    write_user_file(GDRIVE_UNIT_FILE, GDRIVE_UNIT_CONTENT)
    run(["systemctl", "--user", "daemon-reload"])
    if gdrive_signed_in():
        # Already signed in (a re-run): make sure the mount starts with the session.
        if not unit_enabled(GDRIVE_UNIT, user=True):
            run(["systemctl", "--user", "enable", "--now", GDRIVE_UNIT])
    else:
        say("   The mount starts once you sign in to Google Drive (stage 3, or --auth).")
    write_root_file(CHROME_POLICY, CHROME_POLICY_CONTENT)
    for key, (name, url, icon) in GOOGLE_APPS.items():
        write_user_file(HOME / ".local/share/applications" / f"{key}.desktop", f"""\
[Desktop Entry]
Type=Application
Name={name}
Exec=google-chrome-stable --app={url}
Icon={icon}
Terminal=false
Categories=Office;Network;
""")


# ---------------------------------------------------------------- performance tweaks

def sysctl_value(name):
    return output_of(["sysctl", "-n", name])


def sysctl_content():
    lines = [f"# Managed by {SCRIPT}: CachyOS-style tweaks. Delete this file to revert.",
             "# zram (compressed swap in RAM) is cheap to swap to, so use it more.",
             "vm.swappiness = 100",
             "# Keep file system metadata cached longer.",
             "vm.vfs_cache_pressure = 50",
             "# Remove the slowdown penalty some games trigger with misaligned memory",
             "# operations. (\"-\": ignored on CPUs without split-lock detection.)",
             "-kernel.split_lock_mitigate = 0",
             "# Google's BBR congestion control: steadier throughput and latency online.",
             "net.ipv4.tcp_congestion_control = bbr"]
    current = sysctl_value("vm.max_map_count")
    if current.isdigit() and int(current) < MAX_MAP_COUNT:
        lines += [f"# Was {current}; modern games need more memory mappings.",
                  f"vm.max_map_count = {MAX_MAP_COUNT}"]
    return "\n".join(lines) + "\n"


def fstab_noatime():
    """Add noatime to every Btrfs mount in /etc/fstab (only the options field
    changes). A copy of the original is kept once as /etc/fstab.before-postinstall."""
    try:
        lines = Path(FSTAB).read_text().splitlines(keepends=True)
    except OSError:
        failed(f"read {FSTAB}")
        return
    out, changed = [], False
    for line in lines:
        fields = line.split()
        if not line.lstrip().startswith("#") and len(fields) >= 4 and fields[2] == "btrfs":
            opts = [o for o in fields[3].split(",") if o not in ("relatime", "atime", "strictatime")]
            if "noatime" not in opts:
                opts.append("noatime")
            if opts != fields[3].split(","):
                line = re.sub(r"^(\s*\S+\s+\S+\s+\S+\s+)(\S+)", lambda m: m.group(1) + ",".join(opts), line)
                changed = True
        out.append(line)
    if not changed:
        say("   /etc/fstab: Btrfs mounts already use noatime")
        return
    if not Path(FSTAB_BACKUP).exists():
        run(["sudo", "cp", "-a", FSTAB, FSTAB_BACKUP])
    say("   /etc/fstab: adding noatime to Btrfs mounts (applies after the reboot)")
    if write_root_file(FSTAB, "".join(out)):
        run(["sudo", "systemctl", "daemon-reload"])


def gamemode_ini(gpu_index=None):
    text = f"""\
; Managed by {SCRIPT}. Delete this file to go back to GameMode's defaults.
[general]
; ananicy-cpp sets game priority, so GameMode doesn't renice.
renice=0
; CPU governor while a game runs (the previous one comes back afterwards).
desiredgov=performance
"""
    if gpu_index is not None:
        text += f"""
[gpu]
; GameMode's required opt-in phrase for changing GPU settings.
apply_gpu_optimisations=accept-responsibility
; The AMD GPU: /sys/class/drm/card{gpu_index}
gpu_device={gpu_index}
; High GPU clocks only while a game runs.
amd_performance_level=high
"""
    return text


def performance_tweaks():
    section("sysctl (swappiness, cache pressure, split-lock, BBR)")
    run(["sudo", "modprobe", "tcp_bbr"])
    write_root_file(BBR_MODULE_FILE, f"# Managed by {SCRIPT}: BBR congestion control.\ntcp_bbr\n")
    if write_root_file(SYSCTL_FILE, sysctl_content()):
        run(["sudo", "systemctl", "restart", "systemd-sysctl"])

    section("I/O schedulers (NVMe: none, SSD: mq-deadline, HDD: bfq)")
    if write_root_file(IOSCHED_RULES_FILE, IOSCHED_RULES):
        run(["sudo", "udevadm", "control", "--reload"])
        run(["sudo", "udevadm", "trigger", "--subsystem-match=block", "--action=change"])

    section("Btrfs noatime")
    fstab_noatime()

    section("ananicy-cpp: your own rules folder (loads after CachyOS's)")
    write_root_file(f"{ANANICY_CUSTOM_DIR}/README", ANANICY_CUSTOM_README)

    section("GameMode base settings")
    # Keep a GPU section written by stage 3 when re-running stage 2.
    current = read_sys(GAMEMODE_INI)
    if "[gpu]" not in current:
        write_root_file(GAMEMODE_INI, gamemode_ini())
    else:
        say(f"   {GAMEMODE_INI}: already set up (with GPU settings)")

    section("KDE: allow tearing in fullscreen games (lower input latency)")
    if kwrite(KWINRC[0], KWINRC[1], KWINRC[2], "true"):
        run(["dbus-send", "--session", "--type=method_call", "--dest=org.kde.KWin",
             "/KWin", "org.kde.KWin.reconfigure"])
    else:
        say("   already on")


def cpu_gpu_settings():
    hw = FACTS["hw"]
    if "amd-cpu" in BLOCKS:
        section("AMD CPU: frequency driver")
        status = read_sys("/sys/devices/system/cpu/amd_pstate/status")
        if status == "active":
            say("   amd_pstate is active (the default on Zen 2 and newer)")
        elif "cppc" in hw["cpu_flags"]:
            say(f"   amd_pstate is {status or 'not in use'}; switching it to active")
            FACTS["amd_pstate_arg"] = True
            ensure_kernel_arg("amd_pstate=active", "AMD's own CPU frequency driver, in active mode")
        else:
            say("   this CPU doesn't support amd_pstate; the kernel's default driver stays")
        if "zenpower" in ENABLED:
            if rpm_installed("zenpower3"):
                write_root_file(ZENPOWER_MODPROBE, ZENPOWER_MODPROBE_CONTENT)
            else:
                say("   zenpower3 isn't installed, so k10temp stays")
    if "intel-cpu" in BLOCKS:
        section("Intel CPU: frequency driver")
        status = read_sys("/sys/devices/system/cpu/intel_pstate/status")
        if status == "active":
            say("   intel_pstate is active (Intel's own driver; works with tuned and GameMode)")
        else:
            warn(f"intel_pstate is {status or 'not in use'}; expected 'active' on a modern Intel CPU")
        if hw["hybrid"]:
            say("   Hybrid P/E cores: the kernel places work using Intel Thread Director.")
            say("   (With --with scx, prefer schedulers that know core types, e.g. scx_lavd.)")

    if "amd-gpu" in BLOCKS:
        section("AMD GPU")
        if "amd-gpu-tuning" in BLOCKS:
            for arg, tag, why in KERNEL_ARGS:
                if tag == "amd-gpu-tuning":
                    ensure_kernel_arg(arg, why)
        else:
            say("   Laptop iGPU: overclock controls stay locked (turn on with --with igpu-overclock)")
        gpus = "\n".join(output_of(["lspci", "-nn", "-s", c[2]]) for c in hw["cards"] if c[1] == "amdgpu")
        if NAVI_23_24.search(gpus):
            say("   RX 6600-class GPU (gfx1032/gfx1034): ROCm doesn't support it, so ollama runs")
            say("   it as gfx1030 (same instruction set) and ramalama uses its Vulkan image.")
            write_root_file(OLLAMA_OVERRIDE, OLLAMA_OVERRIDE_CONTENT)
            write_root_file(RAMALAMA_CONF, RAMALAMA_CONF_CONTENT)
            write_root_file(ROCM_PROFILE, ROCM_PROFILE_CONTENT)
            run(["sudo", "systemctl", "daemon-reload"])
        else:
            say("   ROCm supports this GPU as-is; no override needed.")
    if "intel-gpu" in BLOCKS:
        section("Intel GPU")
        say("   intel-media-driver (video) and intel_gpu_top (monitoring); nothing else to set.")


def laptop_settings():
    if "laptop" not in BLOCKS:
        say("   Desktop: tuned-ppd stays at Fedora's defaults; GameMode handles games.")
        return
    section("Power profiles (tuned + tuned-ppd, Fedora's standard mapping)")
    for pkg in ("power-profiles-daemon", "tlp"):
        if rpm_installed(pkg):
            warn(f"{pkg} is installed and fights tuned-ppd; remove it: sudo dnf remove {pkg}")
    say("   KDE's battery applet drives it: Power Saver -> powersave, Balanced -> balanced")
    say("   (balanced-battery on battery), Performance -> throughput-performance.")
    if "framework" not in BLOCKS:
        return

    section("Framework: KDE power profile per state")
    for state, profile in POWERDEVIL_PROFILES.items():
        kwrite("powerdevilrc", [state, "Performance"], "PowerProfile", profile)
    refresh_powerdevil()

    if "framework-intel" in BLOCKS:
        section("Framework Intel: s2idle fix")
        for arg, tag, why in KERNEL_ARGS:
            if tag == "framework-intel":
                ensure_kernel_arg(arg, why)
        say("   Revert if wake problems appear: sudo grubby --update-kernel=ALL --remove-args=acpi_osi")

    if "framework-tool" in ENABLED:
        section("framework_tool (Framework's CLI)")
        if Path(FRAMEWORK_TOOL).exists():
            say(f"   {FRAMEWORK_TOOL}: already installed")
        else:
            tmp = Path(tempfile.mkdtemp())
            if run(["curl", "-fsSL", "-o", str(tmp / "framework_tool"), FRAMEWORK_TOOL_URL]).returncode == 0:
                run(["sudo", "install", "-m", "755", str(tmp / "framework_tool"), FRAMEWORK_TOOL])
            else:
                failed("download framework_tool")
            shutil.rmtree(tmp, ignore_errors=True)
        say("   e.g. battery charge limit 80%: sudo framework_tool --charge-limit 80")
    if "audio-no-powersave" in ENABLED:
        section("Audio power saving off")
        write_root_file(AUDIO_MODPROBE, AUDIO_MODPROBE_CONTENT)


def refresh_powerdevil():
    run(["dbus-send", "--session", "--type=method_call", "--dest=org.kde.Solid.PowerManagement",
         "/org/kde/Solid/PowerManagement", "org.kde.Solid.PowerManagement.refreshStatus"])


# ---------------------------------------------------------------- hibernation

def secure_boot_on():
    out = output_of(["mokutil", "--sb-state"]).lower()
    if out:
        return "enabled" in out
    # Without mokutil: the EFI variable's last byte is 1 when Secure Boot is on.
    for var in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"):
        try:
            return var.read_bytes()[-1:] == b"\x01"
        except OSError:
            pass
    return False


def swap_in_fstab():
    return any(l.split()[:1] == [str(SWAP_FILE)] for l in read_sys(FSTAB).splitlines())


def logind_lid_content():
    return f"""\
# Managed by {SCRIPT}. Closing the lid = lowest-drain state. KDE's own lid
# setting (powerdevilrc) says the same, for when you're logged in.
[Login]
HandleLidSwitch={LID_ACTION}
HandleLidSwitchExternalPower={LID_ACTION}
HandleLidSwitchDocked=ignore
"""


def kde_lid_done():
    lid, mode = KDE_LID[LID_ACTION]
    return all(kread("powerdevilrc", [s, "SuspendAndShutdown"], "LidAction") == lid
               and (mode is None or kread("powerdevilrc", [s, "SuspendAndShutdown"], "SleepMode") == mode)
               for s in ("AC", "Battery", "LowBattery"))


def hibernate_state():
    """n/a, secure-boot, needs-setup, needs-reboot, needs-lid or done."""
    if "hibernate" not in BLOCKS:
        return "n/a"
    if secure_boot_on():
        return "secure-boot"
    args = norm_args(default_kernel_args())
    if not (SWAP_FILE.exists() and swap_in_fstab() and " resume_offset=" in args
            and Path(DRACUT_RESUME).exists()):
        return "needs-setup"
    if "resume_offset=" not in read_sys("/proc/cmdline"):
        return "needs-reboot"
    if read_sys(LOGIND_LID) != logind_lid_content() or not kde_lid_done():
        return "needs-lid"
    return "done"


def secure_boot_message():
    say("   Secure Boot is on, and kernel lockdown (part of Secure Boot) blocks hibernation.")
    say("   Lid close stays at sleep (s2idle), the lowest-drain state available with it on.")


def no_hibernate_message():
    if FACTS.get("secure_boot"):
        secure_boot_message()
    else:
        say("   Not a Framework laptop: hibernation isn't set up.")


def hibernate_setup():
    """Phase A: swap file (own Btrfs subvolume, SELinux label, fstab), resume
    kernel arguments and boot image. Takes effect after the reboot."""
    state = hibernate_state()
    if state == "n/a":
        no_hibernate_message()
        return
    if state == "secure-boot":
        secure_boot_message()
        return
    if state != "needs-setup":
        say("   Swap file and resume settings are already in place.")
        return
    if output_of(["findmnt", "-no", "FSTYPE", "/"]) != "btrfs":
        skipped("hibernation: / isn't Btrfs")
        return
    mem_kib = int(re.search(r"MemTotal:\s+(\d+)", read_sys("/proc/meminfo")).group(1))
    size_gib = math.ceil(mem_kib / 1024 / 1024)
    free_gib = shutil.disk_usage("/").free / 1024 ** 3
    if not SWAP_FILE.exists() and free_gib < size_gib + 10:
        skipped(f"hibernation: needs {size_gib} GiB for the swap file (+10 GiB spare), "
                f"only {free_gib:.0f} GiB free")
        return
    say(f"   Swap file of {size_gib} GiB (= RAM) for hibernation; zram still handles everyday swap.")
    if not SWAP_SUBVOL.exists():
        run(["sudo", "btrfs", "subvolume", "create", str(SWAP_SUBVOL)])
    if not SWAP_FILE.exists():
        # mkswapfile creates it with copy-on-write off, which Btrfs swap files need.
        if run(["sudo", "btrfs", "filesystem", "mkswapfile", "--size", f"{size_gib}g",
                str(SWAP_FILE)]).returncode != 0:
            failed("create the hibernation swap file")
            return
    section("SELinux label for the swap file")
    if "/swap(/.*)?" not in output_of(["sudo", "semanage", "fcontext", "-l", "-C"]):
        run(["sudo", "semanage", "fcontext", "-a", "-t", "swapfile_t", "/swap(/.*)?"])
    run(["sudo", "restorecon", "-RF", str(SWAP_SUBVOL)])
    section("fstab entry (low priority, after zram)")
    if not swap_in_fstab():
        if not Path(FSTAB_BACKUP).exists():
            run(["sudo", "cp", "-a", FSTAB, FSTAB_BACKUP])
        text = read_sys(FSTAB) + "\n" + f"# Hibernation swap file (managed by {SCRIPT})\n" + SWAP_FSTAB_LINE + "\n"
        write_root_file(FSTAB, text)
        run(["sudo", "systemctl", "daemon-reload"])
    if str(SWAP_FILE) not in read_sys("/proc/swaps"):
        run(["sudo", "swapon", "--priority", "0", str(SWAP_FILE)])
    section("resume kernel arguments")
    uuid = output_of(["findmnt", "-no", "UUID", "-T", str(SWAP_FILE)])
    offset = output_of(["sudo", "btrfs", "inspect-internal", "map-swapfile", "-r", str(SWAP_FILE)])
    if DRY_RUN and not (uuid and offset):
        uuid, offset = uuid or "<filesystem UUID>", offset or "<offset>"
    if not (uuid and offset):
        failed("hibernation: couldn't read the swap file's location")
        return
    ensure_kernel_arg(f"resume=UUID={uuid}", "where the hibernation image lives")
    ensure_kernel_arg(f"resume_offset={offset}", "its position inside the Btrfs file system")
    section("boot image with the resume module (dracut; takes a few minutes)")
    write_root_file(DRACUT_RESUME, DRACUT_RESUME_CONTENT)
    if run(["sudo", "dracut", "-f", "--regenerate-all"]).returncode != 0:
        failed("rebuild the boot image (dracut)")
    write_root_file(SLEEP_CONF, SLEEP_CONF_CONTENT)
    say("   Hibernation is ready after the reboot. Stage 3 sets the lid action and offers a test.")
    NOTES.append("The hibernation image is a copy of RAM on disk; it's only encrypted if the "
                 "disk is (LUKS).")


def can_hibernate():
    out = output_of(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                     "org.freedesktop.login1.Manager", "CanHibernate"])
    return '"yes"' in out


def hibernate_finish():
    """Phase B: lid action (systemd + KDE), then an optional test."""
    state = hibernate_state()
    if state == "n/a":
        no_hibernate_message()
        return
    if state == "secure-boot":
        secure_boot_message()
        return
    if DRY_RUN and state in ("needs-setup", "needs-reboot"):
        say("   (dry run: stage 2 and the reboot would be done by now; previewing the lid step)")
        state = "needs-lid"
    if state == "needs-setup":
        hibernate_setup()
        FACTS["reboot_for_hibernate"] = True
        return
    if state == "needs-reboot":
        say("   The resume settings take effect after a reboot; this step runs then.")
        FACTS["reboot_for_hibernate"] = True
        return
    section(f"Lid close: {LID_ACTION}")
    write_root_file(LOGIND_LID, logind_lid_content())
    run(["sudo", "systemctl", "kill", "-s", "HUP", "systemd-logind"])
    lid, mode = KDE_LID[LID_ACTION]
    for s in ("AC", "Battery", "LowBattery"):
        kwrite("powerdevilrc", [s, "SuspendAndShutdown"], "LidAction", lid)
        if mode:
            kwrite("powerdevilrc", [s, "SuspendAndShutdown"], "SleepMode", mode)
    refresh_powerdevil()
    if not DRY_RUN and not can_hibernate():
        warn("the system doesn't report hibernation as possible yet (logind CanHibernate != yes)")
        TODO.append("Hibernate: check `swapon --show` lists /swap/swapfile and /proc/cmdline has "
                    "resume_offset=, then run this script again.")
        return
    section("Test hibernate")
    if DRY_RUN:
        say("   [dry-run] would offer: systemctl hibernate, then check the journal")
        return
    say("   Save your work first. The laptop powers off; press the power button to resume,")
    say("   and this window continues where it was.")
    if not yes("   Hibernate now to test?", default=False):
        TODO.append("Hibernate test: save work, `systemctl hibernate`, power on, then "
                    "`journalctl -b -g 'hibernat|resum'`.")
        return
    started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    interactive(["systemctl", "hibernate"])
    ask("   Press Enter once the laptop has resumed: ")
    lines = output_of(["journalctl", "-b", "--since", started, "-g", "hibernat|resum|PM: Image"])
    for line in lines.splitlines()[-8:]:
        say(f"     {line}")
    denials = output_of(["journalctl", "-b", "--since", started, "-g", "avc:.*swapfile"])
    if denials:
        warn("SELinux blocked something around the swap file:")
        for line in denials.splitlines()[-4:]:
            say(f"     {line}")
    else:
        say("   No SELinux denials for the swap file.")


# ---------------------------------------------------------------- services

def user_in_group(group):
    try:
        return username() in grp.getgrnam(group).gr_mem
    except KeyError:
        return False


def setup_services():
    for unit, tag in SYSTEM_SERVICES:
        if not wanted(tag):
            continue
        if unit_enabled(unit) and (DRY_RUN or unit_active(unit)):
            say(f"   {unit}: running")
        elif unit_exists(unit) or DRY_RUN:
            if run(["sudo", "systemctl", "enable", "--now", unit]).returncode != 0:
                failed(f"enable {unit}")
            elif not DRY_RUN:
                say(f"   {unit}: enabled")
        else:
            failed(f"enable {unit}: not installed")
    for unit in USER_SERVICES:
        if unit_enabled(unit, user=True):
            say(f"   {unit} (user): enabled")
        elif unit_exists(unit, user=True) or DRY_RUN:
            # Starts with your desktop session from the next login on.
            if run(["systemctl", "--user", "enable", unit]).returncode != 0:
                failed(f"enable user service {unit}")
        else:
            failed(f"enable user service {unit}: not installed")

    me = username()
    for group in GROUPS:
        try:
            grp.getgrnam(group)
        except KeyError:
            continue
        if user_in_group(group):
            continue
        say(f"   adding {me} to {group} (takes effect after the reboot)")
        if run(["sudo", "usermod", "-aG", group, me]).returncode != 0:
            failed(f"add {me} to {group}")

    if not succeeds(["systemctl", "is-active", "--quiet", "firewalld"]):
        say("   firewalld isn't running; no ports to open")
        return
    zone = output_of(["firewall-cmd", "--get-default-zone"])
    if not zone:
        failed("firewall: couldn't read the default zone")
        return
    have = output_of(["sudo", "firewall-cmd", "--permanent", f"--zone={zone}", "--list-ports"]).split()
    for what, ports in STREAMING_PORTS.items():
        new = [p for p in ports if p not in have]
        if new:
            say(f"   firewall ({zone}): {what} {' '.join(new)}")
            for port in new:
                run(["sudo", "firewall-cmd", "--permanent", f"--zone={zone}", f"--add-port={port}"])
    run(["sudo", "firewall-cmd", "--reload"])


# ================================================================ validation

def collect_checks(post_reboot):
    """Every item stage 2 installs, as (area, item, ok). Read-only.
    post_reboot adds checks that only pass once the reboot made them live."""
    rows = []
    for label, tag, pkgs, _ in RPM_GROUPS:
        if not wanted(tag):
            continue
        for name, _ in pkgs:
            if name in OPTIONAL_RPMS and not rpm_installed(name):
                continue
            rows.append(("rpm", f"{name} ({label})", rpm_installed(name)))
    for old, new, tag, _ in SWAPS:
        if wanted(tag) and not (new in OPTIONAL_RPMS and not rpm_installed(new)):
            rows.append(("codecs", new, rpm_installed(new)))

    for app, what, tag in FLATPAKS:
        if wanted(tag):
            rows.append(("flatpak", what.split(":")[0], flatpak_installed(app)))
    rows.append(("cleanup", "no leftover Flatpak Steam", not flatpak_refs([OLD_STEAM_FLATPAK])))

    env = tool_env()
    for cmd in ["claude", *NPM_GLOBALS.values(), *UV_TOOLS.values(), "gcloud", "code", "uv", "node"]:
        # hf has no --version; its help screen proves it runs.
        probe = "--help" if cmd == "hf" else "--version"
        ok = have_tool(cmd) and run([cmd, probe], changes_system=False, env=env).returncode == 0
        rows.append(("cli", cmd, ok))

    python = AGENTS_VENV / "bin" / "python"
    for mod in AGENT_IMPORTS:
        ok = python.exists() and succeeds([str(python), "-c", f"import {mod}"])
        rows.append(("sdk", mod, ok))

    have = installed_vscode_extensions() if shutil.which("code") else set()
    for ext, label in VSCODE_EXTENSIONS.items():
        rows.append(("vscode", label, ext.lower() in have))

    published = antigravity_published()
    for key, p in ANTIGRAVITY.items():
        have_ver = antigravity_installed_version(key)
        want = published[key][0]  # current version on Google's download page
        rows.append(("antigravity", f"{p['label']} {want}",
                     bool(have_ver) and version_key(have_ver) >= version_key(want)))

    rows.append(("google", "Drive mount service file", GDRIVE_UNIT_FILE.exists()))
    rows.append(("google", "Docs Offline extension policy", Path(CHROME_POLICY).exists()))
    for key, (name, _, _) in GOOGLE_APPS.items():
        rows.append(("google", f"{name} menu entry",
                     (HOME / ".local/share/applications" / f"{key}.desktop").exists()))

    rows.append(("tweaks", SYSCTL_FILE, Path(SYSCTL_FILE).exists()))
    rows.append(("tweaks", IOSCHED_RULES_FILE, Path(IOSCHED_RULES_FILE).exists()))
    rows.append(("tweaks", "noatime on Btrfs mounts", all(
        "noatime" in l.split()[3] for l in read_sys(FSTAB).splitlines()
        if not l.lstrip().startswith("#") and len(l.split()) >= 4 and l.split()[2] == "btrfs")))
    rows.append(("tweaks", f"{ANANICY_CUSTOM_DIR}/", Path(ANANICY_CUSTOM_DIR).is_dir()))
    rows.append(("tweaks", GAMEMODE_INI, Path(GAMEMODE_INI).exists()))
    rows.append(("tweaks", "KDE: allow tearing", kread(*KWINRC) in ("true", "")))
    for arg in expected_kernel_args():
        rows.append(("kernel", f"{arg} (configured)", norm_args(arg) in norm_args(default_kernel_args())))

    for unit, tag in SYSTEM_SERVICES:
        if wanted(tag):
            rows.append(("service", f"{unit} enabled", unit_enabled(unit)))
    for unit in USER_SERVICES:
        rows.append(("service", f"{unit} (user) enabled", unit_enabled(unit, user=True)))
    for group in GROUPS:
        try:
            grp.getgrnam(group)
        except KeyError:
            continue
        rows.append(("group", group, user_in_group(group)))
    rows.append(("shell", "~/.bashrc.d snippet", SHELL_SNIPPET.exists()))
    rows.append(("time", f"24-hour time ({TIME_LOCALE})", time_is_24h()))

    if post_reboot:
        rows += verify_checks()
    return rows


def verify_checks():
    """Is it live? (kernel arguments, daemons, sysctl, drivers, sleep). Read-only."""
    rows = []
    cmdline = norm_args(read_sys("/proc/cmdline"))
    for arg in expected_kernel_args():
        rows.append(("live", f"{arg} active", norm_args(arg) in cmdline))
    if "amd-cpu" in BLOCKS and read_sys("/sys/devices/system/cpu/amd_pstate/status"):
        rows.append(("live", "amd_pstate active", read_sys("/sys/devices/system/cpu/amd_pstate/status") == "active"))
    if "intel-cpu" in BLOCKS:
        rows.append(("live", "intel_pstate active", read_sys("/sys/devices/system/cpu/intel_pstate/status") == "active"))
    for unit, tag in SYSTEM_SERVICES:
        if wanted(tag) and unit in ("ananicy-cpp.service", "tuned.service", "tuned-ppd.service", "lactd.service"):
            rows.append(("live", f"{unit} running", unit_active(unit)))
    rows.append(("live", "swappiness 100", sysctl_value("vm.swappiness") == "100"))
    rows.append(("live", "TCP congestion control bbr", sysctl_value("net.ipv4.tcp_congestion_control") == "bbr"))
    for dev in sorted(Path("/sys/block").glob("nvme*n*")):
        rows.append(("live", f"{dev.name} I/O scheduler none", "[none]" in read_sys(dev / "queue/scheduler")))
    if BLOCKS & {"amd-gpu", "intel-gpu"}:
        rows.append(("live", "hardware video (vainfo)", succeeds(["vainfo", "--display", "drm"])))
    if "amd-gpu" in BLOCKS:
        rows.append(("live", "GameMode AMD GPU settings", "[gpu]" in read_sys(GAMEMODE_INI)))
    rows.append(("live", "Phoronix Test Suite configured (no upload)", pts_configured()))
    if "laptop" in BLOCKS:
        rows.append(("live", "sleep mode s2idle", "[s2idle]" in read_sys("/sys/power/mem_sleep")))
    if "hibernate" in BLOCKS:
        rows.append(("live", "resume= / resume_offset= active", "resume_offset=" in read_sys("/proc/cmdline")))
        rows.append(("live", "hibernation swap file active", str(SWAP_FILE) in read_sys("/proc/swaps")))
        rows.append(("live", "hibernate possible (logind)", can_hibernate()))
        rows.append(("live", f"lid close = {LID_ACTION}", hibernate_state() == "done"))
    return rows


# What to re-run when an area has failures.
RETRY = {
    "rpm": lambda: (install_core(), install_rpms()),
    "codecs": install_swaps,
    "flatpak": install_flatpaks,
    "cleanup": cleanup_flatpak_steam,
    "cli": install_cli_tools,
    "sdk": install_agent_sdks,
    "vscode": install_vscode_extensions,
    "antigravity": install_antigravity,
    "google": setup_google_drive,
    "tweaks": performance_tweaks,
    "kernel": cpu_gpu_settings,
    "service": setup_services,
    "group": setup_services,
    "shell": install_shell_config,
    "time": set_24h_time,
}


def print_table(rows, retried=()):
    width = max(len(item) for _, item, _ in rows)
    for area, item, ok in rows:
        if ok:
            mark = "FIXED" if (area, item) in retried else "ok"
        else:
            mark = "FAILED"
        say(f"   {area:<12} {item:<{width}}  {mark}")


def validate(retry=True, post_reboot=False):
    rows = collect_checks(post_reboot)
    bad = [(a, i) for a, i, ok in rows if not ok]
    if bad and retry and not DRY_RUN:
        areas = sorted({a for a, _ in bad if a in RETRY})
        if areas:
            say(f"   {len(bad)} item(s) missing; retrying once: {', '.join(areas)}")
            done = set()
            for area in areas:
                fix = RETRY[area]
                if fix not in done:
                    fix()
                    done.add(fix)
            rows = collect_checks(post_reboot)
    elif bad and DRY_RUN:
        say("   (dry run: nothing is installed yet, so most checks below fail)")
    section("Results")
    print_table(rows, retried=set(bad))
    still_bad = [f"{a}: {i}" for a, i, ok in rows if not ok]
    say(f"\n   {len(rows) - len(still_bad)} of {len(rows)} OK")
    if still_bad and not DRY_RUN:
        for item in still_bad:
            failed(f"check {item}")
    return not still_bad


# ================================================================ stage 3

def stage3_configure():
    run_tasks("Stage 3 of 4: configure (Phase B) and sign in", [
        ("Post-reboot checks", post_reboot_checks),
        ("GameMode GPU settings", gamemode_gpu),
        ("Displays: variable refresh rate", displays_vrr),
        ("Benchmark tools (Phoronix Test Suite, MangoHud logging)", benchmark_tools_setup),
        ("Hibernation: lid action and test", hibernate_finish),
        ("Sign-ins", signins),
        ("API keys for the agent SDKs", api_keys),
        ("Apps that need a sign-in", open_apps),
        ("To-do list", write_todo),
    ])


def post_reboot_checks():
    rows = verify_checks()
    print_table(rows)
    bad = [i for _, i, ok in rows if not ok]
    if bad and not DRY_RUN:
        NOTES.append("Not live yet: " + "; ".join(bad) + ". See the log, or run --check.")


def amd_gpu_index():
    """The AMD GPU GameMode should drive: the amdgpu card with the most VRAM
    (the graphics card, not a CPU's iGPU)."""
    cards = [c for c in FACTS["hw"]["cards"] if c[1] == "amdgpu"]
    return max(cards, key=lambda c: c[3])[0] if cards else None


def gamemode_gpu():
    if "amd-gpu" not in BLOCKS:
        say("   No AMD GPU: GameMode's GPU settings are AMD-only; skipped.")
        return
    index = amd_gpu_index()
    say(f"   GPU for GameMode: card{index} (high clocks only while a game runs)")
    write_root_file(GAMEMODE_INI, gamemode_ini(index))


def displays_vrr():
    """KDE Wayland: adaptive sync (VRR) = Automatic (on for fullscreen games)."""
    try:
        outputs = json.loads(output_of(["kscreen-doctor", "-j"]) or "{}").get("outputs", [])
    except ValueError:
        outputs = []
    if not outputs:
        say("   Couldn't read the displays (needs a running Plasma session); skipped.")
        return
    for o in outputs:
        if not (o.get("connected") and o.get("enabled")):
            continue
        name = o.get("name", "?")
        if str(o.get("vrrPolicy", "")).lower() in ("2", "automatic"):
            say(f"   {name}: adaptive sync already automatic")
            continue
        say(f"   {name}: adaptive sync -> automatic")
        run(["kscreen-doctor", f"output.{name}.vrrpolicy.automatic"])
    say(f"   Allow tearing in fullscreen: {kread(*KWINRC) or 'true (default)'}")


def machine_id():
    mid = read_sys("/etc/machine-id")[:8] or "unknown"
    return f"{socket.gethostname().split('.')[0]}-{mid}"


def pts_configured():
    text = read_sys(PTS_CONFIG)
    return "<UploadResults>FALSE</UploadResults>" in text and "<Configured>TRUE</Configured>" in text


def benchmark_tools_setup():
    section("Phoronix Test Suite: unattended, results kept local, no upload")
    if pts_configured():
        say("   already configured")
    else:
        # enterprise-setup: accepts the terms once, anonymous reporting off.
        run(["phoronix-test-suite", "enterprise-setup"])
        run(["phoronix-test-suite", "user-config-set", *PTS_SETTINGS])
    section("MangoHud logging")
    logs = BENCH_ROOT / machine_id() / "mangohud"
    if MANGOHUD_CONF.exists():
        say(f"   {MANGOHUD_CONF} already exists; left as is")
    else:
        write_user_file(MANGOHUD_CONF, f"""\
# Written by {SCRIPT} (only because there was none). Edit freely.
# Shift_L+F2 starts/stops a CSV log of frame times, FPS, loads, clocks and temps.
# For a timed run: MANGOHUD_CONFIG=autostart_log=1,log_duration=60 mangohud %command%
output_folder={logs}
toggle_logging=Shift_L+F2
""")
    if not DRY_RUN:
        logs.mkdir(parents=True, exist_ok=True)
    say(f"   Logs go to {logs}")


# ---------------------------------------------------------------- sign-ins

def signin(title, is_done, action, todo_text):
    """One sign-in: skip if already done, else offer to run it now."""
    section(title)
    if DRY_RUN:
        say(f"   [dry-run] if not already done, would offer to run: {todo_text}")
        return
    if is_done():
        say("   already done")
        return
    if yes("   Do this now?"):
        action()
        if is_done():
            say("   done")
            return
        warn(f"{title} doesn't look finished")
    TODO.append(todo_text)


def open_url(url):
    if DRY_RUN:
        say(f"   [dry-run] xdg-open {url}")
        return
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def git_identity():
    name = ask("   Your name for git commits: ")
    email = ask("   Your email for git commits: ")
    if name:
        run(["git", "config", "--global", "user.name", name])
    if email:
        run(["git", "config", "--global", "user.email", email])
    run(["git", "config", "--global", "init.defaultBranch", "main"])


SSH_KEY = HOME / ".ssh" / "id_ed25519"


def ssh_key():
    email = output_of(["git", "config", "--global", "user.email"]) or username()
    say("   Pick a passphrase (KDE's wallet can remember it at login), or leave it empty.")
    interactive(["ssh-keygen", "-t", "ed25519", "-C", email, "-f", str(SSH_KEY)])


def gh_login():
    say("   Choose 'Login with a web browser'. When asked, upload your SSH key.")
    interactive(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "ssh", "--web"])


def gcloud_account():
    return output_of(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])


def gcloud_login():
    interactive(["gcloud", "auth", "login"])


ADC_FILE = HOME / ".config" / "gcloud" / "application_default_credentials.json"


def gcloud_adc():
    say("   Application Default Credentials: what the Python SDKs and Google's ADK use.")
    interactive(["gcloud", "auth", "application-default", "login"])
    project = ask("   Default Google Cloud project ID (Enter to skip): ")
    if project:
        run(["gcloud", "config", "set", "project", project])
        run(["gcloud", "auth", "application-default", "set-quota-project", project])


def claude_cmd():
    return str(LOCAL_BIN / "claude") if (LOCAL_BIN / "claude").exists() else "claude"


def claude_login():
    interactive([claude_cmd(), "auth", "login"])


GEMINI_CREDS = HOME / ".gemini" / "oauth_creds.json"


def gemini_login():
    say("   Gemini CLI opens. Choose 'Login with Google', finish in the browser, then type /quit.")
    interactive(["gemini"])


def codex_login():
    interactive(["codex", "login"])


def hf_login():
    say("   Create a token (type: Read, or Write if you'll upload), copy it, and paste it here.")
    open_url("https://huggingface.co/settings/tokens")
    interactive(["hf", "auth", "login"])


def tailscale_login():
    say("   Open the link it prints to add this PC to your tailnet.")
    say(f"   --operator={username()} lets you run tailscale without sudo from now on.")
    interactive(["sudo", "tailscale", "up", f"--operator={username()}"])


def gdrive_signed_in():
    """True once the rclone remote for Google Drive exists."""
    return f"{GDRIVE_REMOTE}:" in output_of(["rclone", "listremotes"]).split()


def gdrive_login():
    if not gdrive_signed_in():
        say("   Your browser opens to let rclone use your Google Drive.")
        say("   Optional: your own Google OAuth client ID avoids the rate limits of rclone's")
        say("   shared one (guide: rclone.org/drive/#making-your-own-client-id).")
        client_id = ask("   Client ID (Enter = rclone's shared one): ")
        extra = []
        if client_id:
            try:
                secret = getpass.getpass("   Client secret (hidden): ").strip()
            except EOFError:
                secret = ""
            extra = [f"client_id={client_id}", f"client_secret={secret}"]
        interactive(["rclone", "config", "create", GDRIVE_REMOTE, "drive", "scope=drive", *extra])
    if gdrive_signed_in():
        run(["systemctl", "--user", "enable", "--now", GDRIVE_UNIT])
        say(f"   Google Drive is at {GDRIVE_DIR} (drag it to Dolphin's Places panel for quick access).")


def signins():
    say("   Each item is checked first; finished ones are skipped. Answer n to skip one.")
    signin("Git name and email",
           lambda: bool(output_of(["git", "config", "--global", "user.email"])),
           git_identity,
           'git config --global user.name "..." ; git config --global user.email "..."')
    signin("SSH key", SSH_KEY.exists, ssh_key,
           "SSH key: ssh-keygen -t ed25519")
    signin("GitHub (gh)", lambda: succeeds(["gh", "auth", "status"]), gh_login,
           "gh auth login --git-protocol ssh --web")
    signin("Google Cloud (gcloud)", lambda: bool(gcloud_account()), gcloud_login,
           "gcloud auth login")
    signin("Google Cloud credentials for SDKs (ADC)", ADC_FILE.exists, gcloud_adc,
           "gcloud auth application-default login")
    signin("Claude Code", lambda: succeeds([claude_cmd(), "auth", "status"]), claude_login,
           "claude auth login")
    signin("Gemini CLI", GEMINI_CREDS.exists, gemini_login, "Gemini CLI: run `gemini` and log in with Google")
    signin("Codex CLI", lambda: succeeds(["codex", "login", "status"]), codex_login, "codex login")
    signin("Hugging Face", lambda: succeeds(["hf", "auth", "whoami"]), hf_login, "hf auth login")
    signin("Google Drive mount (~/GoogleDrive)",
           lambda: gdrive_signed_in() and unit_enabled(GDRIVE_UNIT, user=True), gdrive_login,
           f"Google Drive mount: python3 {INSTALLED_COPY} --auth (or rclone config create "
           f"{GDRIVE_REMOTE} drive, then systemctl --user enable --now {GDRIVE_UNIT})")
    signin("Tailscale", lambda: succeeds(["tailscale", "status"]), tailscale_login,
           f"sudo tailscale up --operator={username()}")


def read_api_keys():
    keys = {}
    try:
        for line in API_KEYS_FILE.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and v.strip():
                keys[k.strip()] = v.strip()
    except OSError:
        pass
    return keys


def api_keys():
    say(f"   Saved to {API_KEYS_FILE} (only you can read it) and loaded only when you run")
    say("   `agents` in a terminal, so they never override the CLIs' subscription logins.")
    keys = read_api_keys()
    changed = False
    for var, page in API_KEYS.items():
        if var in keys:
            section(f"{var}: already saved")
            continue
        if DRY_RUN:
            say(f"   [dry-run] would offer to open {page} and ask for {var} (hidden input)")
            continue
        if not yes(f"-- Add {var}?", default=False):
            TODO.append(f"(optional) API key: create one at {page}, then add {var}=... to {API_KEYS_FILE}")
            continue
        open_url(page)
        try:
            value = getpass.getpass(f"   Paste {var} (hidden): ").strip()
        except EOFError:
            value = ""
        if value:
            keys[var] = value
            changed = True
    if changed:
        content = f"# Managed by {SCRIPT}; loaded by `agents`. Keep private.\n"
        content += "".join(f"{k}={v}\n" for k, v in keys.items())
        write_user_file(API_KEYS_FILE, content, mode=0o600)


def find_desktop_file(ids, label):
    """The app's .desktop file: by id first, then by its menu name."""
    for app_id in ids:
        for d in APP_DIRS:
            f = d / f"{app_id}.desktop"
            if f.exists():
                return f
    for d in APP_DIRS:
        for f in sorted(d.glob("*.desktop")) if d.is_dir() else []:
            try:
                if f"\nName={label}\n" in f.read_text(errors="replace"):
                    return f
            except OSError:
                continue
    return None


def open_apps():
    say("   Each app opens in turn. Sign in, then come back here and press Enter.")
    say("   Type s then Enter to skip one (it goes on the to-do list).")
    for label, ids, what in SIGNIN_APPS:
        section(f"{label}: {what}")
        if DRY_RUN:
            say(f"   [dry-run] would open {label} and wait for Enter")
            continue
        if ask("   Enter = open it, s = skip: ").lower() == "s":
            TODO.append(f"{label}: {what}")
            continue
        desktop = find_desktop_file(ids, label)
        if not desktop:
            failed(f"open {label}: not installed")
            TODO.append(f"{label}: {what}")
            continue
        subprocess.Popen(["gio", "launch", str(desktop)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        if ask("   Press Enter when done (s = not finished, add to to-do): ").lower() == "s":
            TODO.append(f"{label}: {what}")

    section(f"Sunshine: create its admin username and password at {SUNSHINE_WEB_UI}")
    say("   (Your browser warns about the certificate: it's Sunshine's own, on this PC; continue.)")
    if DRY_RUN or yes("   Open it now?"):
        open_url(SUNSHINE_WEB_UI)
    else:
        TODO.append(f"Sunshine: create the admin login at {SUNSHINE_WEB_UI}")


def write_todo():
    items = TODO + MANUAL_TODO
    text = "Desktop post-install: things left to do\n" + "=" * 40 + "\n"
    text += "".join(f"[ ] {item}\n" for item in items)
    text += f"\nSign-ins again any time: python3 {INSTALLED_COPY} --auth\n"
    text += f"Baseline benchmarks any time: python3 {INSTALLED_COPY} --benchmark\n"
    write_user_file(TODO_FILE, text)
    say(f"   {TODO_FILE}")


# ================================================================ stage 4

def bench_entries():
    if not BENCH_LIST.exists():
        write_user_file(BENCH_LIST, BENCH_LIST_DEFAULT)
    text = BENCH_LIST.read_text() if BENCH_LIST.exists() else BENCH_LIST_DEFAULT
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def stage4_baseline():
    entries = bench_entries()
    tests = [e for e in entries if e.startswith("pts/")]
    commands = [e[4:].strip() for e in entries if e.startswith("run:")]
    out = BENCH_ROOT / machine_id() / f"{STAMP}-baseline"
    name = re.sub(r"[^a-z0-9-]", "-", f"{machine_id()}-baseline-{STAMP}".lower())

    def snapshot():
        say(f"   Results: {out}")
        if DRY_RUN:
            return
        out.mkdir(parents=True, exist_ok=True)
        info = {
            "machine": machine_id(), "when": STAMP, "blocks": sorted(BLOCKS), "extras": sorted(ENABLED),
            "hardware": {k: v for k, v in FACTS["hw"].items() if k not in ("batteries",)},
            "kernel": os.uname().release, "cmdline": read_sys("/proc/cmdline"),
            "tuned": output_of(["tuned-adm", "active"]), "power_profile": output_of(["powerprofilesctl", "get"]),
            "cpu_driver": read_sys("/sys/devices/system/cpu/amd_pstate/status")
            or read_sys("/sys/devices/system/cpu/intel_pstate/status"),
            "sysctl": {k: sysctl_value(k) for k in ("vm.swappiness", "vm.vfs_cache_pressure",
                                                     "vm.max_map_count", "net.ipv4.tcp_congestion_control")},
            "on_ac_power": on_ac_power(),
        }
        (out / "system.json").write_text(json.dumps(info, indent=2, default=str) + "\n")
        (out / "system-info.txt").write_text(output_of(["phoronix-test-suite", "system-info"]) + "\n")

    def pts_run():
        if not tests:
            say("   No pts/ tests in the list.")
            return
        say(f"   Tests: {', '.join(tests)}")
        say("   Its own output is shown below. Downloading and building each test takes a few")
        say("   minutes the first time, and each test runs several times for a stable result.")
        if not pts_configured():
            benchmark_tools_setup()  # batch mode: no questions during the run
        # SKIP_EXTERNAL_DEPENDENCIES: don't try to install packages itself
        # (that asks for a password and would sit waiting); the core build
        # tools are installed already.
        env = dict(os.environ, SKIP_EXTERNAL_DEPENDENCIES="1")
        section("downloading and building the tests")
        r = run(["phoronix-test-suite", "batch-install", *tests], env=env, live=True)
        if r.returncode != 0:
            failed("Phoronix Test Suite: installing the tests")
        env.update(TEST_RESULTS_NAME=name, TEST_RESULTS_IDENTIFIER="baseline",
                   TEST_RESULTS_DESCRIPTION=f"Stock settings on {machine_id()}")
        section("running the tests")
        if run(["phoronix-test-suite", "batch-benchmark", *tests], env=env, live=True).returncode != 0:
            failed("Phoronix Test Suite: running the tests")
        if DRY_RUN:
            return
        results = HOME / ".phoronix-test-suite" / "test-results" / name
        if results.is_dir():
            shutil.copytree(results, out / "pts", dirs_exist_ok=True)
        for fmt in ("csv", "json"):
            run(["phoronix-test-suite", f"result-file-to-{fmt}", name])
            for f in HOME.glob(f"{name}*.{fmt}"):
                shutil.move(str(f), out / f.name)

    def commands_run():
        for i, cmd in enumerate(commands, 1):
            cmd = cmd.replace("{out}", str(out))
            section(cmd)
            r = run(["bash", "-c", cmd], live=True)
            if not DRY_RUN:
                slug = re.sub(r"[^a-z0-9]+", "-", cmd.lower()).strip("-")[:40]
                (out / f"{i:02d}-{slug}.txt").write_text(f"$ {cmd}\n\n{r.stdout}{r.stderr}")

    ensure_ac_power("the benchmarks")
    run_tasks("Stage 4 of 4: baseline benchmarks (Phase C)", [
        ("System snapshot", snapshot),
        ("Phoronix Test Suite", pts_run),
        ("Other benchmark commands", commands_run),
    ])
    say(f"\n   Edit the list any time: {BENCH_LIST}")


# ================================================================ summary

def summary():
    banner("Summary")
    for item in SKIPPED:
        say(f"- Skipped: {item}")
    for note in NOTES:
        say(f"- Note: {note}")
    problems = [(f"package list: {e}", "") for e in LIST_ERRORS] + FAILURES
    if not problems:
        say("- Problems: none")
    else:
        say(f"- Problems ({len(problems)}):")
        report = [f"Problems from {SCRIPT}, {STAMP}", ""]
        for what, detail in problems:
            say(f"  * {what}")
            report.append(f"* {what}")
            for line in detail.splitlines():
                say(f"      {line}")
                report.append(f"    {line}")
        report += ["", f"Full log: {LOG_FILE}"]
        problems_file = LOG_DIR / f"problems-{STAMP}.txt"
        problems_file.write_text("\n".join(report) + "\n")
        say(f"- Problems saved to {problems_file}")
    say(f"- Full log: {LOG_FILE}")


# ================================================================ main

def parse_option_names(values):
    names = set()
    for v in values or []:
        for n in v.split(","):
            n = n.strip()
            if not n:
                continue
            if n not in OPTIONS:
                sys.exit(f"Unknown option '{n}'. Known: {', '.join(OPTIONS)}")
            names.add(n)
    return names


def main():
    global DRY_RUN, VERBOSE, LOG

    parser = argparse.ArgumentParser(description="Fedora KDE post-install: software, tooling and tweaks.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every command that would change the system, without running it")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3, 4), help="run only this stage")
    parser.add_argument("--auth", action="store_true", help="sign-ins only")
    parser.add_argument("--benchmark", action="store_true", help="baseline benchmarks only (stage 4)")
    parser.add_argument("--check", action="store_true", help="validation only, including post-reboot checks")
    parser.add_argument("--with", dest="with_", action="append", metavar="NAME[,NAME]",
                        help="turn on opt-in extras (remembered for later runs)")
    parser.add_argument("--without", action="append", metavar="NAME[,NAME]", help="turn extras off again")
    parser.add_argument("--list-options", action="store_true", help="show the opt-in extras")
    parser.add_argument("--verbose", action="store_true",
                        help="print every command and its full output live (to find where something hangs)")
    args = parser.parse_args()
    DRY_RUN, VERBOSE = args.dry_run, args.verbose

    if args.list_options:
        for name, (what, need) in OPTIONS.items():
            print(f"  {name:<20} {what}" + (f"  [{need} only]" if need else ""))
        return

    # Root would put the Flatpaks, CLIs and sign-ins in /root instead of your home.
    if os.geteuid() == 0:
        print("Don't run this as root or with sudo. Rerun it as your normal user:\n"
              f"    python3 {SCRIPT}")
        sys.exit(1)

    ENABLED.update((load_options() | parse_option_names(args.with_)) - parse_option_names(args.without))
    save_options(ENABLED)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG = open(LOG_FILE, "w")
    say(f"{SCRIPT} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        f"{' (DRY RUN: nothing will be changed)' if DRY_RUN else ''}")
    say(f"Log: {LOG_FILE}")
    say(f"     (watch everything live from another terminal: tail -f {LOG_FILE})")

    # Tools installed into ~/.local/bin (Claude, Gemini, Codex, hf) must be
    # findable even when Konsole was started by the autostart, not a login shell.
    os.environ["PATH"] = f"{LOCAL_BIN}:{os.environ.get('PATH', '')}"

    check_fedora()
    load_lists()
    detect_hardware()
    if args.check:
        banner("Checking everything (read-only)")
        validate(retry=False, post_reboot=True)
        return

    install_copy()
    check_wheel()
    stop_sudo = start_sudo()
    awake = stay_awake()
    try:
        if args.auth:
            banner("Sign-ins")
            signins()
            api_keys()
            open_apps()
            write_todo()
        elif args.benchmark:
            stage4_baseline()
        elif DRY_RUN:
            # Preview from here to the end, without saving progress or rebooting.
            start = args.stage or min(saved_stage(), 4)
            for n in range(start, 5 if not args.stage else start + 1):
                (stage1_clean_base, stage2_install, stage3_configure, stage4_baseline)[n - 1]()
            summary()
        else:
            # The autostart entry is only for the first login after a reboot.
            set_autostart(False)
            stage = args.stage or saved_stage()
            if stage == 1:
                stage1_clean_base()
                summary()
                if not args.stage:
                    save_stage(2)
                    ask_reboot("stage 2 (install)")
            elif stage == 2:
                stage2_install()
                summary()
                if not args.stage:
                    save_stage(3)
                    ask_reboot("stage 3 (configure and sign in)")
            elif stage == 3:
                stage3_configure()
                if not args.stage:
                    save_stage(4)
                if FACTS.get("reboot_for_hibernate"):
                    summary()
                    ask_reboot("the hibernation step")
                    return
                if not args.stage:
                    if yes("\nRun the baseline benchmarks now? They take 20-60 minutes; "
                           "leave the machine alone meanwhile.", default=False):
                        stage4_baseline()
                    else:
                        say(f"Later: python3 {INSTALLED_COPY} --benchmark")
                    save_stage(DONE)
                summary()
            elif stage == 4:
                stage4_baseline()
                if not args.stage:
                    save_stage(DONE)
                summary()
            else:
                banner("All stages are done")
                hib = hibernate_state()
                if hib in ("needs-setup", "needs-reboot", "needs-lid"):
                    say("Picking up the hibernation step:")
                    hibernate_finish()
                    if FACTS.get("reboot_for_hibernate"):
                        summary()
                        ask_reboot("the hibernation step")
                        return
                say("Checking everything is still installed and live.")
                say("(Sign-ins: --auth. Benchmarks: --benchmark. A single stage: --stage N.)")
                validate(retry=True, post_reboot=True)
                summary()
    except KeyboardInterrupt:
        # Keep the autostart so the next login picks this stage up again.
        if not DRY_RUN and saved_stage() in (2, 3, 4):
            set_autostart(True)
        fatal("interrupted. Run the same command again to continue.")
    finally:
        let_sleep(awake)
        stop_sudo.set()


if __name__ == "__main__":
    main()
