# desktop/postinstall.py

Personal software, tooling and CachyOS-style performance tweaks for a fresh **Fedora KDE Plasma** desktop or laptop. It's separate from the server scripts.

Fedora's own packages stay in place. Every tweak is added as a separate file you can delete to revert it, and the only package swaps are the RPM Fusion codec drivers. Packages come from Fedora, RPM Fusion, the vendor's own repo, a developer's own COPR (plus the CachyOS project's addons COPR, limited to the packages listed below), or Flathub.

## Run it

Install Fedora KDE, log in, open Konsole:

```bash
curl -fsSLO https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/desktop/postinstall.py
python3 postinstall.py --dry-run   # preview, changes nothing
python3 postinstall.py
```

| Stage | What happens | Ends with |
|---|---|---|
| 1. Clean base | Hardware detection, system update, firmware (pending updates listed, installed after you confirm; laptops must be on the charger), Flatpak updates | Reboot |
| 2. Install (Phase A) | Repos, packages, Flatpaks, AI CLIs and SDKs, VS Code extensions, Antigravity, Google Drive mount, performance tweaks, kernel arguments, GPU tooling, laptop power settings, Framework hibernation (swap file + resume). Then a validation pass that retries anything missing once. | Reboot |
| 3. Configure and sign in (Phase B) | Checks that kernel arguments and daemons are live, GameMode AMD GPU settings, variable refresh rate, Phoronix Test Suite and MangoHud logging, lid close = hibernate (Framework) with a test, every sign-in, each app | To-do file on the Desktop |
| 4. Baseline benchmarks (Phase C) | Runs `~/.config/desktop-postinstall/benchmarks.txt` at stock settings; results go to `~/benchmarks/<machine>/` | Offered at the end of stage 3, or `--benchmark` later |

After each reboot, Konsole opens by itself once you log in and continues. You can also just run `python3 postinstall.py` again.

| Option | Does |
|---|---|
| `--dry-run` | Print what would change |
| `--check` | Validation table, including post-reboot checks (read-only) |
| `--auth` | Sign-ins only; finished ones are skipped |
| `--benchmark` | Stage 4 only |
| `--stage N` | Run only stage 1–4 |
| `--with NAME[,NAME]` / `--without NAME` | Turn opt-in extras on or off (remembered); `--list-options` shows them |
| `--verbose` | Also print every command as it runs |

Logs: `~/postinstall-logs/`. Re-running is safe; finished steps are skipped.

## Hardware detection

Every run reads the hardware first and applies only the matching blocks. One machine can match several.

| Block | Detected by | Adds |
|---|---|---|
| AMD CPU | `AuthenticAMD` in `/proc/cpuinfo` | Checks `amd_pstate` is active; adds `amd_pstate=active` only if it isn't and the CPU supports it |
| Intel CPU | `GenuineIntel` | Checks `intel_pstate` is active; notes hybrid P/E cores |
| AMD GPU | `amdgpu` kernel driver in use | `mesa-va/vdpau-drivers-freeworld`, LACT + `lactd`, ROCm, `amdgpu.ppfeaturemask=0xffffffff` (desktop), GameMode GPU section, RX 6600 ROCm workaround |
| Intel GPU | `i915` / `xe` driver in use | `intel-media-driver` (full codecs), `igt-gpu-tools` |
| Laptop | `/sys/class/power_supply/BAT*` | Charger checks; verifies tuned-ppd (warns if TLP or power-profiles-daemon is present) |
| Framework | DMI vendor `Framework` (board from the product name) | KDE power profile per state; Intel boards: `acpi_osi="!Windows 2020"`; hibernation; firmware versions shown in stage 1 |

## What gets installed

The full list, one line per package with what it's for, is `RPM_GROUPS` at the top of the script. Edit it there.

| Area | Items | Source |
|---|---|---|
| Browsers and editors | Google Chrome, VS Code (+ 16 extensions) | Google / Microsoft repos |
| Antigravity | Antigravity (agent app) and Antigravity IDE | Google's download page, current version read on each run |
| AI | ChatGPT desktop (with Codex); Claude Code, Gemini CLI, Codex CLI, `hf`; agent SDKs in `~/.venvs/agents` | OpenAI repo; Anthropic installer, npm, uv |
| Local AI | ollama, ramalama, llama.cpp (+ ROCm on AMD) | Fedora |
| Cloud | Google Cloud CLI, GKE auth plugin, kubectl, skaffold | Google repo |
| Google Drive and Docs | Drive mounted at `~/GoogleDrive` (rclone), Docs Offline extension, Docs/Sheets/Slides/Gmail/Drive menu entries | Fedora; Chrome policy |
| Remote | Remmina, Tailscale | Fedora; Tailscale repo |
| Virtual machines | QEMU/KVM, libvirt, virt-manager, swtpm, UEFI, virtio-win | Fedora; virtio-win repo |
| Gaming | Steam (Flatpak, + MangoHud/gamescope/vkBasalt layers), ProtonPlus, GameMode, MangoHud, gamescope, Sunshine, Moonlight, OBS + VkCapture | Flathub; Fedora; Sunshine's COPR |
| Process priority | ananicy-cpp + CachyOS rules (as-is); your overrides in `/etc/ananicy.d/99-custom/` | CachyOS addons COPR |
| Power | tuned + tuned-ppd (Fedora's standard mapping, unchanged) | Fedora |
| Benchmarking and monitoring | phoronix-test-suite, vkmark, glmark2, stress-ng, sysbench, kernel-tools (turbostat), s-tui, lm_sensors, powertop, vulkan-tools, vainfo | Fedora |
| Apps | Discord, Spotify, Obsidian, OrcaSlicer, Lychee Slicer | Flathub |

## Tweaks (drop-in files)

| File | What |
|---|---|
| `/etc/sysctl.d/99-performance.conf` | swappiness 100 (zram), vfs_cache_pressure 50, split_lock_mitigate 0, BBR; max_map_count only if Fedora's is lower |
| `/etc/udev/rules.d/60-ioschedulers.rules` | NVMe `none`, SSD `mq-deadline`, HDD `bfq` |
| `/etc/fstab` | `noatime` added to Btrfs mounts (options field only; original kept as `/etc/fstab.before-postinstall`) |
| `/etc/gamemode.ini` | `renice=0` (ananicy-cpp owns priority), performance governor; AMD GPU section in stage 3 |
| `kwinrc` | Allow tearing in fullscreen; VRR set to Automatic per display in stage 3 |
| Kernel arguments (grubby, all kernels) | Per the hardware table above; never `mitigations=off` |

## Framework hibernation

On a Framework laptop, closing the lid puts it in the lowest-drain state, which is hibernate. The behaviour is set by `LID_ACTION` at the top of the script, and `suspend-then-hibernate` is the alternative.

- **Only with Secure Boot off.** Kernel lockdown, which comes with Secure Boot, blocks hibernation. Secure Boot stays on for these machines, so the script detects that, skips all of the hibernation steps, and lid close stays at sleep (s2idle). The Intel `acpi_osi` fix still applies.
- **Stage 2:** Btrfs subvolume `/swap` with a swap file the size of RAM (low priority, so zram still handles everyday swap), SELinux label, fstab entry, `resume=`/`resume_offset=`, dracut resume module, `HibernateDelaySec=30min`.
- **Stage 3:** lid action in systemd-logind (`10-lid.conf`: battery and charger = hibernate, docked = ignore) and in KDE PowerDevil, then an optional test hibernate that checks the journal and SELinux.
- **Encryption:** the hibernation image is a copy of RAM, and it's unencrypted unless the disk is.

## Opt-in extras (`--with`)

`heroic`, `lutris`, `scx` (sched_ext schedulers + tools + GUI), `rt-tests`, `fio`, `tuned-switcher`, `full-ffmpeg`, `thermald` (Intel), `ryzenadj` (AMD), `zenpower` (AMD; third-party COPR, needs Secure Boot off), `igpu-overclock` (laptop AMD iGPU), `framework-tool`, `audio-no-powersave`.

## Verify by hand

```bash
python3 ~/.local/share/desktop-postinstall/postinstall.py --check   # all of the below in one table
cat /proc/cmdline; tuned-adm active; powerprofilesctl get
systemctl status ananicy-cpp tuned lactd
sysctl vm.swappiness vm.max_map_count net.ipv4.tcp_congestion_control
cat /sys/block/nvme0n1/queue/scheduler; vainfo --display drm
cat /sys/power/mem_sleep /sys/power/state; swapon --show; mokutil --sb-state
```

## Notes

- **API keys** go in `~/.config/api-keys.env` (mode 600) and are loaded only by `agents`. Exported globally, they would make the Claude, Gemini and Codex CLIs bill the key instead of your subscription.
- **Antigravity** is installed from Google's Linux download to `~/.local/opt`. Those copies can't update themselves, so when Antigravity says an update is out, run `--stage 2` again.
- **Steam Deck streaming:** install Moonlight on the Deck from Discover, then pair it with Sunshine at `https://localhost:47990`. Steam Remote Play works too.
- **MangoHud logs** go to `~/benchmarks/<machine>/mangohud` (Shift_L+F2 to start and stop). Flatpak Steam is given access to `~/benchmarks` for this.
