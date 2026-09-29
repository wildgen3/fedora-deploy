#!/usr/bin/env python3
"""postinstall.py - personal software and tooling for a Fedora KDE desktop.

Download it, then run it as your normal user, never as root:

    curl -fsSLO https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/desktop/postinstall.py
    python3 postinstall.py              # run (or continue) the setup
    python3 postinstall.py --dry-run    # only print what would change

It runs in three stages with a reboot between them. After each reboot a
Konsole window opens on its own once you log in and carries on where it left
off (you can also just run the same command again):

  Stage 1  Clean base: safety checks, one sudo password prompt, hardware
           detection, full system update (dnf), firmware updates (fwupd, with
           the pending updates listed first; laptops must be on the charger),
           Flatpak updates. Reboot.
  Stage 2  Install: official repos (RPM Fusion, Google, Microsoft, OpenAI,
           Tailscale, Sunshine's COPR, virtio-win, Flathub), RPMs, Flatpaks,
           the Claude / Gemini / Codex / Hugging Face CLIs, the agent SDK
           environment, VS Code extensions, Antigravity (current version read
           from Google's download page), GPU-specific drivers (AMD or Intel,
           detected), services and
           groups. Then a validation pass: every item is checked, anything
           missing is retried once, and a pass/fail table is printed. Reboot.
  Stage 3  Sign-in: git identity, SSH key, GitHub, Google Cloud, Claude,
           Gemini, Codex, Hugging Face, Tailscale and API keys, then each app
           that needs a sign-in is opened one at a time. Anything skipped is
           written to a to-do file on your Desktop.

Other options:
    --stage N   run only stage N (1, 2 or 3)
    --auth      same as --stage 3 (sign-ins only; skips ones already done)
    --check     only the validation pass (read-only)

Everything is logged to ~/postinstall-logs/. Running it twice is harmless:
finished steps are detected and skipped. Only the Python standard library is
used.
"""

import argparse
import datetime
import getpass
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urljoin

# ----------------------------------------------------------------- settings

SCRIPT = "postinstall.py"

# ---- repositories (all from the software's own publisher)

# Written to /etc/yum.repos.d/<name>.repo. Each entry is exactly what the
# vendor's own install instructions add.
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

# OpenAI publishes no key URL: its first RPM installs the signing key and
# OpenAI's signed repo, and later updates come through dnf from that repo.
CHATGPT_RPM = "https://persistent.oaistatic.com/codex-app-prod/linux/rpm/latest/chatgpt.x86_64.rpm"

# .repo files the publisher hosts; downloaded as-is into /etc/yum.repos.d/.
REPO_URLS = {
    "tailscale": "https://pkgs.tailscale.com/stable/fedora/tailscale.repo",
    # Red Hat's signed Windows guest drivers (disk, network, display) for VMs.
    "virtio-win": "https://fedorapeople.org/groups/virt/virtio-win/virtio-win.repo",
}

# COPRs run by the software's own developers.
COPRS = {
    "lizardbyte/stable": "Sunshine (LizardByte, Sunshine's developers)",
}

RPMFUSION = [
    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-{rel}.noarch.rpm",
    "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-{rel}.noarch.rpm",
]

# ---- RPM packages, grouped so a problem in one group doesn't block the others

RPM_GROUPS = {
    "command-line basics": ["git", "gh", "curl", "jq", "ripgrep", "fzf", "btop",
                            "tmux", "unzip", "7zip", "fastfetch"],
    # python3.13: the agent SDK environment uses it (newest Python the SDKs all support).
    "Python and Node": ["python3", "python3-pip", "python3-devel", "python3.13", "uv",
                        "nodejs", "npm", "gcc", "gcc-c++", "make"],
    "containers": ["podman"],
    "local AI": ["ollama", "ramalama", "llama-cpp"],
    # -secret stores saved passwords in KWallet (through the Secret Service).
    "remote desktop": ["remmina", "remmina-plugins-rdp", "remmina-plugins-vnc",
                       "remmina-plugins-secret", "remmina-plugins-kwallet"],
    # @virtualization = QEMU/KVM + libvirt + virt-manager. swtpm (virtual TPM)
    # and edk2-ovmf (UEFI) are what Windows 11 VMs need.
    "virtual machines": ["@virtualization", "swtpm", "swtpm-tools", "edk2-ovmf"],
    # steam-devices: udev rules so controllers (and the Steam Deck) work.
    "gaming (host side)": ["steam-devices", "gamemode"],
}

# Nice to have, but not in every Fedora release: skipped quietly if missing.
OPTIONAL_RPMS = {"remmina-plugins-kwallet"}

# Packages from the vendor repos and COPRs above.
VENDOR_GROUPS = {
    "Google Chrome": ["google-chrome-stable"],
    "VS Code": ["code"],
    "ChatGPT": ["chatgpt"],
    "Google Cloud CLI": ["google-cloud-cli", "google-cloud-cli-gke-gcloud-auth-plugin",
                         "kubectl", "google-cloud-cli-skaffold"],
    "Tailscale": ["tailscale"],
    "Sunshine": ["Sunshine"],
    "virtio-win drivers": ["virtio-win"],
}

# RPM Fusion: full codecs. Fedora's own video drivers can't encode or decode
# H.264/HEVC; RPM Fusion's builds can, which Sunshine (streaming to the Steam
# Deck) and OBS need. (from, to) pairs are swapped in place.
CODEC_SWAPS = [("ffmpeg-free", "ffmpeg"),
               ("mesa-vulkan-drivers", "mesa-vulkan-drivers-freeworld")]
# Per GPU maker, from the hardware detection. AMD video goes through Mesa;
# Intel through Intel's media driver. ROCm lets ollama use an AMD GPU.
GPU_SWAPS = {
    "amd": [("mesa-va-drivers", "mesa-va-drivers-freeworld")],
    "intel": [("libva-intel-media-driver", "intel-media-driver")],
}
GPU_PACKAGES = {
    "amd": ["rocm-hip", "rocm-opencl", "rocminfo"],
}
CODEC_PACKAGES = ["gstreamer1-plugins-bad-freeworld", "gstreamer1-plugins-ugly",
                  "gstreamer1-plugin-libav"]

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
ANTIGRAVITY_DIR = Path.home() / ".local" / "opt"
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

# ---- Flatpaks (Flathub, installed for your user only)

FLATPAKS = {
    "com.discordapp.Discord": "Discord",
    "com.spotify.Client": "Spotify",
    "md.obsidian.Obsidian": "Obsidian",
    "com.orcaslicer.OrcaSlicer": "OrcaSlicer (filament printers)",
    "io.mango3d.LycheeSlicer": "Lychee Slicer (resin printers)",
    "com.valvesoftware.Steam": "Steam",
    "com.vysp3r.ProtonPlus": "ProtonPlus (Proton versions)",
    "com.obsproject.Studio": "OBS Studio",
    "com.obsproject.Studio.Plugin.OBSVkCapture": "OBS game capture plugin",
    "com.moonlight_stream.Moonlight": "Moonlight (streaming client)",
}

# Extensions Steam games can use. Installed at the branch that matches Steam's
# runtime, which is read from Steam once it's installed.
STEAM_EXTENSIONS = {
    "org.freedesktop.Platform.VulkanLayer.MangoHud": "MangoHud (FPS overlay)",
    "org.freedesktop.Platform.VulkanLayer.gamescope": "gamescope",
    "org.freedesktop.Platform.VulkanLayer.vkBasalt": "vkBasalt (post-processing)",
    "org.freedesktop.Platform.VulkanLayer.OBSVkCapture": "OBS game capture layer",
}

FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"

# ---- command-line tools and SDKs (installed in your home, no sudo)

HOME = Path.home()
LOCAL_BIN = HOME / ".local" / "bin"

# npm installs "global" packages into ~/.local (so ~/.local/bin), not /usr.
NPM_PREFIX = HOME / ".local"
NPM_GLOBALS = {"@google/gemini-cli": "gemini", "@openai/codex": "codex"}

CLAUDE_INSTALLER = "https://claude.ai/install.sh"

UV_TOOLS = {"huggingface_hub": "hf"}

# Shared scratch environment for agent SDK experiments; real projects pin
# their own copies. `agents` in a terminal activates it.
AGENTS_VENV = HOME / ".venvs" / "agents"
AGENTS_PYTHON = "3.13"
AGENT_SDKS = ["anthropic", "claude-agent-sdk", "google-genai", "google-adk", "openai",
              "openai-agents", "mcp", "litellm", "python-dotenv", "ipykernel"]
# Import names checked by the validation pass.
AGENT_IMPORTS = ["anthropic", "claude_agent_sdk", "google.genai", "google.adk", "openai",
                 "agents", "mcp", "litellm", "dotenv", "ipykernel"]

VSCODE_EXTENSIONS = {
    "ms-vscode-remote.remote-ssh": "Remote - SSH",
    "anthropic.claude-code": "Claude Code",
    "google.geminicodeassist": "Gemini Code Assist",
    "saoudrizwan.claude-dev": "Cline",
    "ms-python.python": "Python",
    "ms-python.vscode-pylance": "Pylance",
    "charliermarsh.ruff": "Ruff",
    "ms-toolsai.jupyter": "Jupyter",
    "googlecloudtools.cloudcode": "Cloud Code",
    "github.vscode-pull-request-github": "GitHub Pull Requests",
    "redhat.vscode-yaml": "YAML",
    "tamasfe.even-better-toml": "Even Better TOML",
    "usernamehw.errorlens": "Error Lens",
    "ms-azuretools.vscode-containers": "Container Tools",
    "timonwong.shellcheck": "ShellCheck",
}

# API keys for the SDKs. Kept in a private file and loaded only by `agents`,
# never globally: a global ANTHROPIC_API_KEY (or GEMINI_/OPENAI_) makes the
# Claude/Gemini/Codex CLIs bill that key instead of your subscription login.
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

# ---- services, groups, firewall

SYSTEM_SERVICES = ["tailscaled.service", "ollama.service"]
USER_SERVICES = ["app-dev.lizardbyte.app.Sunshine.service"]
# libvirt: manage VMs without a password. render/video: GPU compute (ROCm).
GROUPS = ["libvirt", "render", "video"]
# Opened in the default firewall zone (Fedora's desktop zone already allows
# them; this covers other zones). Sunshine's web UI (47990) stays local-only.
STREAMING_PORTS = {
    "Sunshine": ["47984/tcp", "47989/tcp", "48010/tcp", "47998-48000/udp"],
    "Steam Remote Play": ["27036-27037/tcp", "27031-27036/udp"],
}
SUNSHINE_WEB_UI = "https://localhost:47990"

# ---- stage 3: apps that need a sign-in, opened one at a time

# (label, .desktop ids to try, what to do there)
SIGNIN_APPS = [
    ("Google Chrome", ["google-chrome"], "Sign in to Google and turn on sync."),
    ("VS Code", ["code"],
     "Accounts (bottom left) > Backup and Sync Settings. Then sign in to Claude Code, "
     "Gemini Code Assist and Cline from their sidebar icons."),
    ("Antigravity", ["google-antigravity"], "Sign in with your Google account."),
    ("Antigravity IDE", ["google-antigravity-ide", "antigravity"], "Sign in with your Google account."),
    ("ChatGPT", ["chatgpt", "ChatGPT"], "Sign in to your OpenAI account."),
    ("Discord", ["com.discordapp.Discord"], "Sign in."),
    ("Spotify", ["com.spotify.Client"], "Sign in."),
    ("Steam", ["com.valvesoftware.Steam"],
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
    "ProtonPlus: install the latest GE-Proton for Steam.",
    "Windows VM: download the Windows 11 ISO from microsoft.com. In virt-manager, choose "
    "'Microsoft Windows 11' as the OS (adds UEFI + TPM), and attach "
    "/usr/share/virtio-win/virtio-win.iso as a second CD for the drivers.",
    "Local AI test: `ollama run llama3.2` and `ramalama run llama3.2`; keep whichever is faster.",
    "Antigravity IDE: add extensions from its own store (it doesn't share VS Code's).",
    f"Antigravity updates: when it says a new version is out, run "
    f"`python3 {Path.home() / '.local/share/desktop-postinstall/postinstall.py'} --stage 2` "
    f"(tarball installs can't update themselves).",
]

# ---- where things go

STATE_DIR = HOME / ".local" / "state" / "desktop-postinstall"
STAGE_FILE = STATE_DIR / "stage"
# A copy of this script, so the after-reboot autostart has a fixed path.
INSTALLED_COPY = HOME / ".local" / "share" / "desktop-postinstall" / SCRIPT
AUTOSTART = HOME / ".config" / "autostart" / "desktop-postinstall.desktop"
TODO_FILE = HOME / "Desktop" / "desktop-postinstall-TODO.txt"

LOG_DIR = HOME / "postinstall-logs"
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOG_DIR / f"desktop-postinstall-{STAMP}.log"

# ------------------------------------------------------------------ state

DRY_RUN = False
LOG = None            # open log file handle
FAILURES = []         # things that went wrong but didn't stop the script
SKIPPED = []          # things skipped on purpose (with the reason)
NOTES = []            # reminders for the summary
TODO = []             # sign-ins skipped in stage 3, for the Desktop to-do file
FACTS = {}            # values collected along the way

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


def run(cmd, changes_system=True, input_text=None, env=None):
    """Print, run and log one command. Returns a CompletedProcess.

    changes_system=True: the command modifies something. With --dry-run it is
    only printed. changes_system=False: a read-only lookup (rpm -q, dnf info,
    flatpak info, ...). Those run even in a dry run so the preview can make
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

    if changes_system:
        say(f"$ {shown}")
    else:
        log(f"$ {shown}")
    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            # Empty stdin so nothing waits for input (sudo prompts via the terminal).
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
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


def succeeds(cmd):
    """True if a read-only command exits 0."""
    return run(cmd, changes_system=False).returncode == 0


def interactive(cmd):
    """Run a command attached to the terminal (logins, prompts). Returns True on exit 0."""
    shown = shlex.join(cmd)
    if DRY_RUN:
        say(f"[dry-run] {shown}")
        return True
    say(f"$ {shown}")
    try:
        rc = subprocess.run(cmd).returncode
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
    FACTS["version"] = info.get("VERSION_ID", "")
    say(f"Detected: {FACTS['release']} (kernel {os.uname().release})")
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


def read_sys(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


# SMBIOS chassis types for portable machines (notebook, laptop, convertible, ...).
LAPTOP_CHASSIS = {"8", "9", "10", "14", "30", "31", "32"}
GPU_MAKERS = {"1002": "amd", "8086": "intel", "10de": "nvidia"}


def detect_hardware():
    """Read what this machine is, so later steps can match it (firmware, GPU
    drivers, charger checks). Read-only; runs on every start."""
    step("Hardware")
    dmi = Path("/sys/class/dmi/id")
    cpuinfo = read_sys("/proc/cpuinfo")
    m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    gpus = [line for cls in ("0300", "0302", "0380")
            for line in output_of(["lspci", "-nn", "-d", f"::{cls}"]).splitlines() if line.strip()]
    batteries = [d for d in Path("/sys/class/power_supply").glob("*")
                 if read_sys(d / "type") == "Battery"]
    hw = {
        "vendor": read_sys(dmi / "sys_vendor"),
        "model": read_sys(dmi / "product_name"),
        "cpu": m.group(1).strip() if m else "unknown",
        "gpus": gpus,
        "gpu_makers": sorted({GPU_MAKERS[v] for line in gpus
                              for v in re.findall(r"\[([0-9a-f]{4}):[0-9a-f]{4}\]", line)
                              if v in GPU_MAKERS}),
        "laptop": bool(batteries) or read_sys(dmi / "chassis_type") in LAPTOP_CHASSIS,
        "batteries": batteries,
        # systemd-detect-virt prints "none" (and exits 1) on real hardware.
        "virt": run(["systemd-detect-virt"], changes_system=False).stdout.strip() or "none",
    }
    FACTS["hw"] = hw
    say(f"Machine: {hw['vendor'] or '?'} {hw['model'] or ''}".rstrip()
        + (" (laptop)" if hw["laptop"] else "") + (f" (virtual: {hw['virt']})" if hw["virt"] != "none" else ""))
    say(f"CPU: {hw['cpu']}")
    for line in gpus or ["none found by lspci"]:
        say(f"GPU: {line}")
    if hw["laptop"]:
        say(f"Power: {'charger connected' if on_ac_power() else 'on battery'}"
            + (f", battery {battery_percent()}%" if battery_percent() is not None else ""))


def on_ac_power():
    """True on a desktop, or when a laptop's charger is connected."""
    if not FACTS["hw"]["batteries"]:
        return True
    for d in Path("/sys/class/power_supply").glob("*"):
        if read_sys(d / "type") in ("Mains", "USB") and read_sys(d / "online") == "1":
            return True
    return False


def battery_percent():
    levels = [int(v) for v in (read_sys(b / "capacity") for b in FACTS["hw"]["batteries"]) if v.isdigit()]
    return min(levels) if levels else None


def ensure_ac_power(what):
    """Laptops: don't start long updates or firmware flashing on battery."""
    if on_ac_power():
        return
    if DRY_RUN:
        say(f"[dry-run] on battery: a real run asks you to plug in before {what}")
        return
    warn(f"this laptop is on battery. Plug in the charger before {what}.")
    while not on_ac_power():
        if ask("   Press Enter once it's plugged in (s = continue on battery): ").lower() == "s":
            warn(f"continuing {what} on battery")
            return
    say("   Charger connected.")


def keep_sudo_alive(stop):
    """Background thread: refresh sudo's timestamp every 60 s until told to stop.
    -n means 'never prompt', so this can't hang if the timestamp is gone."""
    while not stop.wait(60):
        subprocess.run(["sudo", "-n", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_sudo():
    say("This script needs your sudo password once for system-level steps.")
    say("$ sudo -v")
    # Read-only lookups (dnf info) use sudo too, so this also happens in a dry run.
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        fatal("sudo -v failed (wrong password, or no sudo rights).")
    stop = threading.Event()
    threading.Thread(target=keep_sudo_alive, args=(stop,), daemon=True).start()
    return stop


def username():
    return pwd.getpwuid(os.getuid()).pw_name


# ------------------------------------------------------ stage bookkeeping

def saved_stage():
    """1, 2, 3, or 4 (= all done). A missing file means a fresh start."""
    try:
        text = STAGE_FILE.read_text().strip()
    except OSError:
        return 1
    return int(text) if text in ("1", "2", "3", "4") else 1


def save_stage(n):
    if DRY_RUN:
        say(f"[dry-run] would record: next stage = {n}")
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STAGE_FILE.write_text(f"{n}\n")


def install_copy():
    """Copy this script to a fixed place for the after-reboot autostart."""
    me = Path(__file__).resolve()
    if me == INSTALLED_COPY.resolve() or DRY_RUN:
        return
    INSTALLED_COPY.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(me, INSTALLED_COPY)


def set_autostart(on):
    """KDE starts everything in ~/.config/autostart at login. The entry opens
    Konsole running this script, which continues from the saved stage."""
    if DRY_RUN:
        say(f"[dry-run] would {'create' if on else 'remove'} {AUTOSTART}")
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


def reboot_and_continue(next_stage):
    """Save progress, set up the autostart, and offer the reboot."""
    save_stage(next_stage)
    set_autostart(True)
    say(f"\nStage {next_stage - 1} is finished. After the reboot, log in and stage "
        f"{next_stage} starts by itself in a Konsole window.")
    say(f"(Or run it yourself any time: python3 {INSTALLED_COPY})")
    if DRY_RUN:
        say("[dry-run] would ask 'Reboot now? [Y/n]' and on yes run: sudo systemctl reboot")
        return
    if yes("Reboot now?"):
        run(["sudo", "systemctl", "reboot"])
    else:
        say("Not rebooting yet. Reboot with: sudo systemctl reboot")


# ---------------------------------------------------------------- dnf helpers

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
        # Group installs are recorded by dnf; also accept the group's key package.
        return name == "@virtualization" and succeeds(["rpm", "-q", "virt-manager", "qemu-kvm"])
    return succeeds(["rpm", "-q", "--whatprovides", name])


def install_packages(names, label):
    """Install a group of packages. Missing ones are skipped with a warning.
    If the group transaction fails, retry one by one to isolate the bad one."""
    say(f"-- {label}")
    missing = [name for name in names if not rpm_installed(name)]
    if not missing:
        say("   already installed")
        return
    wanted = []
    for name in missing:
        # A dry run doesn't add the repos, so vendor packages can't be found yet.
        if DRY_RUN or package_available(name):
            wanted.append(name)
        elif name in OPTIONAL_RPMS:
            skipped(f"{name}: optional, not in this Fedora release")
        else:
            failed(f"{name}: not found in the enabled repositories")
    if not wanted:
        return
    if run(["sudo", "dnf", "install", "-y", *wanted]).returncode == 0:
        return
    warn(f"installing '{label}' failed; retrying one package at a time")
    for name in wanted:
        if run(["sudo", "dnf", "install", "-y", name]).returncode != 0:
            failed(f"install {name}")


def swap_package(old, new):
    """Replace a Fedora package with its RPM Fusion build (or install it)."""
    if rpm_installed(new):
        return
    if rpm_installed(old):
        cmd = ["sudo", "dnf", "swap", "-y", old, new, "--allowerasing"]
    else:
        cmd = ["sudo", "dnf", "install", "-y", new]
    if run(cmd).returncode != 0:
        failed(f"swap {old} -> {new}")


def write_root_file(path, content, mode="644"):
    """Write a root-owned file via `sudo tee`, only if its content differs."""
    try:
        if Path(path).read_text() == content:
            say(f"{path}: already up to date")
            return True
    except OSError:
        pass  # doesn't exist yet (or unreadable): write it
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
        say(f"[dry-run] would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with the final mode so a private file is never briefly readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, mode)
    say(f"wrote {path}")


# ------------------------------------------------------------ systemd helpers

def unit_exists(unit, user=False):
    cmd = ["systemctl"] + (["--user"] if user else []) + ["list-unit-files", "--no-legend", unit]
    r = run(cmd, changes_system=False)
    return r.returncode == 0 and unit in r.stdout


def unit_enabled(unit, user=False):
    cmd = ["systemctl"] + (["--user"] if user else []) + ["is-enabled", unit]
    return output_of(cmd) == "enabled"


# ================================================================ stage 1

def stage1_clean_base():
    step("Stage 1: clean base (system update)")
    say("A full update on a fresh install can take a while.")
    ensure_ac_power("the system update")
    if run(["sudo", "dnf", "upgrade", "--refresh", "-y"]).returncode != 0:
        fatal("`dnf upgrade` failed. Fix networking/repos and rerun.")
    firmware_updates()

    say("-- Flatpaks that came with the system")
    if run(["sudo", "flatpak", "update", "-y", "--noninteractive"]).returncode != 0:
        warn("flatpak update reported a problem (see log); continuing")


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
    Lenovo, SSD and dock makers, ...) publish it. What it finds depends on the
    machine, so the pending list is shown before anything is flashed."""
    step("Firmware (fwupd)")
    hw = FACTS["hw"]
    if hw["virt"] != "none":
        skipped(f"firmware: this is a virtual machine ({hw['virt']})")
        return
    # Download the current catalogue first (the one on a fresh install is old).
    run(["sudo", "fwupdmgr", "refresh", "--force"])
    devices = output_of(["fwupdmgr", "get-devices", "--json"])
    try:
        count = sum(1 for d in json.loads(devices).get("Devices", [])
                    if "updatable" in d.get("Flags", []))
        say(f"Devices fwupd can update on this {hw['vendor'] or 'machine'}: {count}")
    except ValueError:
        pass
    pending = pending_firmware()
    if not pending:
        say("No firmware updates pending." + (" (dry run: the catalogue isn't refreshed, so "
                                              "a real run may find some)" if DRY_RUN else ""))
        return
    say("Pending firmware updates:")
    for name, cur, new in pending:
        say(f"  - {name}: {cur} -> {new}")
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


# ================================================================ stage 2

def setup_repos():
    step("Repositories")
    rel = FACTS.get("version") or output_of(["rpm", "-E", "%fedora"])

    say("-- RPM Fusion (free + nonfree)")
    if not succeeds(["rpm", "-q", "rpmfusion-free-release", "rpmfusion-nonfree-release"]):
        if run(["sudo", "dnf", "install", "-y", *[u.format(rel=rel) for u in RPMFUSION]]).returncode != 0:
            failed("enable RPM Fusion")

    say("-- vendor repos")
    for name, content in REPO_FILES.items():
        write_root_file(f"/etc/yum.repos.d/{name}.repo", content)
    for name, url in REPO_URLS.items():
        path = f"/etc/yum.repos.d/{name}.repo"
        if Path(path).exists():
            say(f"{path}: already present")
            continue
        r = run(["curl", "-fsSL", url], changes_system=False)
        if r.returncode == 0 and "[" in r.stdout:
            write_root_file(path, r.stdout)
        else:
            failed(f"download {url}")

    say("-- ChatGPT (its RPM adds OpenAI's signed repo)")
    if rpm_installed("chatgpt"):
        say("   already installed")
    elif run(["sudo", "dnf", "install", "-y", CHATGPT_RPM]).returncode != 0:
        failed("install ChatGPT (adds OpenAI's repo)")

    say("-- COPRs")
    for copr, what in COPRS.items():
        say(f"{copr}: {what}")
        if run(["sudo", "dnf", "copr", "enable", "-y", copr]).returncode != 0:
            failed(f"enable COPR {copr}")

    say("-- refreshing package lists")
    # Vendor repos' signing keys are imported here (first use asks dnf to trust them).
    if run(["sudo", "dnf", "makecache", "--refresh", "-y"]).returncode != 0:
        failed("dnf makecache")

    say("-- Flathub (for your user)")
    if run(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_URL]).returncode != 0:
        failed("add Flathub")


def gpu_makers():
    return [m for m in ("amd", "intel") if m in FACTS["hw"]["gpu_makers"]]


def install_codecs():
    step("Codecs and GPU video drivers (RPM Fusion)")
    for old, new in CODEC_SWAPS:
        swap_package(old, new)
    for maker in gpu_makers():
        say(f"-- {maker.upper()} GPU")
        for old, new in GPU_SWAPS.get(maker, []):
            swap_package(old, new)
        if GPU_PACKAGES.get(maker):
            install_packages(GPU_PACKAGES[maker], f"{maker.upper()} GPU compute")
    install_packages(CODEC_PACKAGES, "GStreamer codecs")


def install_rpms():
    step("RPM packages")
    for label, names in RPM_GROUPS.items():
        install_packages(names, label)


def install_vendor_rpms():
    step("Vendor packages")
    for label, names in VENDOR_GROUPS.items():
        install_packages(names, label)


def flatpak_installed(app_id, branch=None):
    ref = f"{app_id}//{branch}" if branch else app_id
    return succeeds(["flatpak", "info", "--user", ref])


def install_flatpaks():
    step("Flatpak apps (Flathub, your user)")
    missing = [a for a in FLATPAKS if not flatpak_installed(a)]
    if not missing:
        say("all installed")
        return
    for app in missing:
        say(f"-- {FLATPAKS[app]} ({app})")
    cmd = ["flatpak", "install", "--user", "-y", "--noninteractive", "flathub"]
    if run(cmd + missing).returncode == 0:
        return
    warn("the batch install failed; retrying one app at a time")
    for app in missing:
        if run(cmd + [app]).returncode != 0:
            failed(f"flatpak {app}")


def steam_branch():
    """Steam's runtime branch (e.g. 24.08), read from `flatpak info`."""
    info = output_of(["flatpak", "info", "--user", "com.valvesoftware.Steam"])
    m = re.search(r"^\s*Runtime:\s*\S+/(\S+)\s*$", info, re.M)
    return m.group(1) if m else ""


def install_steam_extensions():
    step("Steam extensions (MangoHud, gamescope, vkBasalt, OBS capture)")
    branch = steam_branch()
    if not branch:
        if DRY_RUN:
            say("(dry run: Steam isn't installed yet; a real run reads its runtime branch here)")
        else:
            failed("Steam extensions: Steam isn't installed, so its runtime branch is unknown")
        return
    say(f"Steam runtime branch: {branch}")
    for ext, label in STEAM_EXTENSIONS.items():
        if flatpak_installed(ext, branch):
            continue
        say(f"-- {label}")
        if run(["flatpak", "install", "--user", "-y", "--noninteractive", "flathub",
                f"{ext}//{branch}"]).returncode != 0:
            failed(f"flatpak {ext}//{branch}")


def tool_env():
    """Environment for user-level installs: ~/.local/bin first on PATH."""
    env = dict(os.environ)
    env["PATH"] = f"{LOCAL_BIN}:{env.get('PATH', '')}"
    return env


def have_tool(cmd):
    return shutil.which(cmd, path=tool_env()["PATH"]) is not None


def install_cli_tools():
    step("AI command-line tools (Claude Code, Gemini, Codex, Hugging Face)")
    env = tool_env()

    say("-- Claude Code (Anthropic's installer, updates itself)")
    if have_tool("claude"):
        say("   already installed")
    elif run(["bash", "-c", f"set -o pipefail; curl -fsSL {CLAUDE_INSTALLER} | bash"], env=env).returncode != 0:
        failed("install Claude Code")

    say(f"-- npm packages into {NPM_PREFIX} (no sudo)")
    run(["npm", "config", "set", "prefix", str(NPM_PREFIX)], env=env)
    missing = [p for p, cmd in NPM_GLOBALS.items() if not have_tool(cmd)]
    for pkg in missing:
        if run(["npm", "install", "-g", pkg], env=env).returncode != 0:
            failed(f"npm {pkg}")
    if not missing:
        say("   already installed")

    say("-- uv tools")
    for pkg, cmd in UV_TOOLS.items():
        if have_tool(cmd):
            say(f"   {cmd}: already installed")
        elif run(["uv", "tool", "install", pkg], env=env).returncode != 0:
            failed(f"uv tool {pkg}")


def install_agent_sdks():
    step(f"Agent SDK environment ({AGENTS_VENV})")
    env = tool_env()
    python = AGENTS_VENV / "bin" / "python"
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
    step("Shell setup (~/.bashrc.d)")
    write_user_file(SHELL_SNIPPET, SHELL_SNIPPET_CONTENT)
    # Fedora's default ~/.bashrc already loads ~/.bashrc.d/*; add it if missing.
    bashrc = HOME / ".bashrc"
    try:
        text = bashrc.read_text()
    except OSError:
        text = ""
    if "bashrc.d" not in text:
        if DRY_RUN:
            say(f"[dry-run] would add a ~/.bashrc.d loader to {bashrc}")
        else:
            with open(bashrc, "a") as f:
                f.write('\nfor rc in ~/.bashrc.d/*; do [ -f "$rc" ] && . "$rc"; done; unset rc\n')


def installed_vscode_extensions():
    return set(output_of(["code", "--list-extensions"]).lower().split())


def install_vscode_extensions():
    step("VS Code extensions")
    if not shutil.which("code") and not DRY_RUN:
        failed("VS Code extensions: VS Code isn't installed")
        return
    have = installed_vscode_extensions()
    for ext, label in VSCODE_EXTENSIONS.items():
        if ext.lower() in have:
            continue
        say(f"-- {label}")
        if run(["code", "--install-extension", ext]).returncode != 0:
            failed(f"VS Code extension {ext}")


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
    step("Antigravity (Google)")
    published = antigravity_published()
    for key, p in ANTIGRAVITY.items():
        ver, url = published[key]
        say(f"-- {p['label']}: Google's download page has {ver}")
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
        say(f"[dry-run] download {url}")
        say(f"[dry-run] unpack it to {dest}, link {LOCAL_BIN / key}, add a menu entry")
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


def gpu_settings():
    step("GPU settings")
    gpus = "\n".join(FACTS["hw"]["gpus"])
    if "[1002:" not in gpus:
        say("No AMD GPU, so no ROCm overrides are needed.")
        return
    if not NAVI_23_24.search(gpus):
        say("This AMD GPU is supported by ROCm; no override needed.")
        return
    say("RX 6600-class GPU (gfx1032/gfx1034): ROCm doesn't support it, so:")
    say("  - ollama runs it as gfx1030 (same instruction set)")
    say("  - ramalama uses its Vulkan image instead of ROCm")
    FACTS["gfx_override"] = True
    write_root_file(OLLAMA_OVERRIDE, OLLAMA_OVERRIDE_CONTENT)
    write_root_file(RAMALAMA_CONF, RAMALAMA_CONF_CONTENT)
    write_root_file(ROCM_PROFILE, ROCM_PROFILE_CONTENT)
    run(["sudo", "systemctl", "daemon-reload"])


def user_in_group(group):
    try:
        return username() in grp.getgrnam(group).gr_mem
    except KeyError:
        return False


def setup_services():
    step("Services, groups and firewall")
    for unit in SYSTEM_SERVICES:
        if unit_enabled(unit):
            say(f"{unit}: already enabled")
        elif unit_exists(unit) or DRY_RUN:
            if run(["sudo", "systemctl", "enable", "--now", unit]).returncode != 0:
                failed(f"enable {unit}")
        else:
            failed(f"enable {unit}: not installed")
    for unit in USER_SERVICES:
        if unit_enabled(unit, user=True):
            say(f"{unit} (user): already enabled")
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
            if not DRY_RUN:
                warn(f"group {group} doesn't exist; skipping")
            continue
        if user_in_group(group):
            continue
        if run(["sudo", "usermod", "-aG", group, me]).returncode != 0:
            failed(f"add {me} to {group}")

    if not succeeds(["systemctl", "is-active", "--quiet", "firewalld"]):
        say("firewalld isn't running; no ports to open")
        return
    zone = output_of(["firewall-cmd", "--get-default-zone"])
    if not zone:
        failed("firewall: couldn't read the default zone")
        return
    for what, ports in STREAMING_PORTS.items():
        say(f"-- firewall ({zone}): {what}")
        for port in ports:
            run(["sudo", "firewall-cmd", "--permanent", f"--zone={zone}", f"--add-port={port}"])
    run(["sudo", "firewall-cmd", "--reload"])


def stage2_install():
    step("Stage 2: install")
    setup_repos()
    install_codecs()
    install_rpms()
    install_vendor_rpms()
    install_flatpaks()
    install_steam_extensions()
    install_cli_tools()
    install_agent_sdks()
    install_shell_config()
    install_vscode_extensions()
    install_antigravity()
    gpu_settings()
    setup_services()


# ------------------------------------------------------------ validation

def collect_checks():
    """Every item stage 2 installs, as (area, item, ok). Read-only."""
    rows = []
    for label, names in {**RPM_GROUPS, **VENDOR_GROUPS}.items():
        for name in names:
            if name in OPTIONAL_RPMS:
                continue
            rows.append(("rpm", f"{name} ({label})", rpm_installed(name)))
    for old, new in CODEC_SWAPS + [sw for m in gpu_makers() for sw in GPU_SWAPS.get(m, [])]:
        rows.append(("codecs", new, rpm_installed(new)))
    for m in gpu_makers():
        for name in GPU_PACKAGES.get(m, []):
            rows.append(("codecs", name, rpm_installed(name)))
    for name in CODEC_PACKAGES:
        rows.append(("codecs", name, rpm_installed(name)))

    for app, label in FLATPAKS.items():
        rows.append(("flatpak", label, flatpak_installed(app)))
    branch = steam_branch()
    for ext, label in STEAM_EXTENSIONS.items():
        rows.append(("steam-ext", label, bool(branch) and flatpak_installed(ext, branch)))

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
        have = antigravity_installed_version(key)
        want = published[key][0]  # current version on Google's download page
        ok = bool(have) and version_key(have) >= version_key(want)
        rows.append(("antigravity", f"{p['label']} {want}", ok))

    for unit in SYSTEM_SERVICES:
        rows.append(("service", unit, unit_enabled(unit)))
    for unit in USER_SERVICES:
        rows.append(("service", f"{unit} (user)", unit_enabled(unit, user=True)))
    for group in GROUPS:
        rows.append(("group", group, user_in_group(group)))
    rows.append(("shell", "~/.bashrc.d snippet", SHELL_SNIPPET.exists()))
    return rows


# What to re-run when an area has failures.
RETRY = {
    "rpm": lambda: (install_rpms(), install_vendor_rpms()),
    "codecs": install_codecs,
    "flatpak": install_flatpaks,
    "steam-ext": install_steam_extensions,
    "cli": install_cli_tools,
    "sdk": install_agent_sdks,
    "vscode": install_vscode_extensions,
    "antigravity": lambda: install_antigravity(),
    "service": setup_services,
    "group": setup_services,
    "shell": install_shell_config,
}


def print_table(rows, retried=()):
    width = max(len(item) for _, item, _ in rows)
    for area, item, ok in rows:
        if ok:
            mark = "FIXED" if (area, item) in retried else "ok"
        else:
            mark = "FAILED"
        say(f"  {area:<12} {item:<{width}}  {mark}")


def validate(retry=True):
    step("Validation")
    rows = collect_checks()
    bad = [(a, i) for a, i, ok in rows if not ok]
    if bad and retry and not DRY_RUN:
        areas = sorted({a for a, _ in bad})
        say(f"{len(bad)} item(s) missing; retrying once: {', '.join(areas)}")
        done = set()
        for area in areas:
            fix = RETRY[area]
            if fix not in done:
                fix()
                done.add(fix)
        rows = collect_checks()
    elif bad and DRY_RUN:
        say("(dry run: nothing is installed yet, so most checks below fail; a real run retries these)")
    step("Validation results")
    print_table(rows, retried=set(bad))
    still_bad = [f"{a}: {i}" for a, i, ok in rows if not ok]
    say(f"\n{len(rows) - len(still_bad)} of {len(rows)} OK")
    if still_bad and not DRY_RUN:
        for item in still_bad:
            failed(f"check {item}")
    return not still_bad


# ================================================================ stage 3

def signin(title, is_done, action, todo_text):
    """One sign-in: skip if already done, else offer to run it now."""
    say(f"\n-- {title}")
    if DRY_RUN:
        say(f"[dry-run] if not already done, would offer to run: {todo_text}")
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
        say(f"[dry-run] xdg-open {url}")
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
    step("API keys for the agent SDKs")
    say(f"Saved to {API_KEYS_FILE} (only you can read it) and loaded only when you run")
    say("`agents` in a terminal, so they never override the CLIs' subscription logins.")
    keys = read_api_keys()
    changed = False
    for var, page in API_KEYS.items():
        if var in keys:
            say(f"-- {var}: already saved")
            continue
        if DRY_RUN:
            say(f"[dry-run] would offer to open {page} and ask for {var} (hidden input)")
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
    step("Apps that need a sign-in")
    say("Each app opens in turn. Sign in, then come back here and press Enter.")
    say("Type s then Enter to skip one (it goes on the to-do list).")
    for label, ids, what in SIGNIN_APPS:
        say(f"\n-- {label}: {what}")
        if DRY_RUN:
            say(f"[dry-run] would open {label} and wait for Enter")
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

    say(f"\n-- Sunshine: create its admin username and password at {SUNSHINE_WEB_UI}")
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
    write_user_file(TODO_FILE, text)


def stage3_signin():
    step("Stage 3: sign-ins")
    say("Each item is checked first; finished ones are skipped. Answer n to skip one.")

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
    signin("Tailscale", lambda: succeeds(["tailscale", "status"]), tailscale_login,
           f"sudo tailscale up --operator={username()}")
    api_keys()
    open_apps()
    write_todo()


# ================================================================ summary

def summary():
    step("Summary")
    for item in SKIPPED:
        say(f"- Skipped: {item}")
    for item in FAILURES:
        say(f"- Failed: {item}")
    if not FAILURES:
        say("- Failed: nothing")
    for note in NOTES:
        say(f"- Note: {note}")
    say(f"- Full log: {LOG_FILE}")


# ---------------------------------------------------------------- main

def main():
    global DRY_RUN, LOG

    parser = argparse.ArgumentParser(description="Fedora KDE desktop post-install: personal software and tooling.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every command that would change the system, without running it")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3), help="run only this stage")
    parser.add_argument("--auth", action="store_true", help="sign-ins only (same as --stage 3)")
    parser.add_argument("--check", action="store_true", help="only the validation pass (read-only)")
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    only = 3 if args.auth else args.stage

    # Root would put the Flatpaks, CLIs and sign-ins in /root instead of your home.
    if os.geteuid() == 0:
        print("Don't run this as root or with sudo. Rerun it as your normal user:\n"
              f"    python3 {SCRIPT}")
        sys.exit(1)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG = open(LOG_FILE, "w")
    say(f"{SCRIPT} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        f"{' (DRY RUN: nothing will be changed)' if DRY_RUN else ''}")
    say(f"Log: {LOG_FILE}")

    # Tools installed into ~/.local/bin (Claude, Gemini, Codex, hf) must be
    # findable even when Konsole was started by the autostart, not a login shell.
    os.environ["PATH"] = f"{LOCAL_BIN}:{os.environ.get('PATH', '')}"

    check_fedora()
    detect_hardware()
    if args.check:
        validate(retry=False)
        return

    # The autostart entry is only for the first login after a reboot.
    if not DRY_RUN:
        set_autostart(False)
    install_copy()
    check_wheel()
    stop_sudo = start_sudo()
    try:
        stage = only or saved_stage()
        if stage == 4:
            say("All three stages are already done. Checking everything is still installed.")
            say("(Sign-ins again: --auth. A single stage again: --stage N.)")
            validate(retry=True)
        elif DRY_RUN:
            # Preview from here to the end, without saving progress or rebooting.
            for n in range(stage, 4 if not only else stage + 1):
                (stage1_clean_base, lambda: (stage2_install(), validate()), stage3_signin)[n - 1]()
        elif stage == 1:
            stage1_clean_base()
            summary()
            if not only:
                reboot_and_continue(2)
        elif stage == 2:
            stage2_install()
            validate()
            summary()
            if not only:
                reboot_and_continue(3)
        elif stage == 3:
            stage3_signin()
            summary()
            if not only:
                save_stage(4)
            say(f"\nAll done. Remaining manual steps: {TODO_FILE}")
    except KeyboardInterrupt:
        # Keep the autostart so the next login picks this stage up again.
        if not only and not DRY_RUN and saved_stage() in (2, 3):
            set_autostart(True)
        fatal("interrupted. Run the same command again to continue.")
    finally:
        stop_sudo.set()


if __name__ == "__main__":
    main()
