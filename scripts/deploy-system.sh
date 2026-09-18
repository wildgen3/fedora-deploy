#!/usr/bin/env bash
# deploy-system.sh — system-level setup for Fedora 44 KDE. Runs as root.
#
# Normally run by ../bootstrap.sh on a stock Fedora 44 KDE install. Safe to re-run:
#     sudo ~/.local/share/fedora-deploy/scripts/deploy-system.sh
# (The optional kickstart in extras/kickstart/ also calls it from %post.)
#
# Sourcing rules: Fedora, RPM Fusion, Flathub, vendor repos, upstream authors.
# Nothing from Nobara / PikaOS (see manifests/forbidden.txt).
set -uo pipefail

DEPLOY_DIR=${DEPLOY_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}
M="$DEPLOY_DIR/manifests"
STATE=/var/lib/fedora-deploy
LOGDIR=/var/log/fedora-deploy
mkdir -p "$STATE" "$LOGDIR"
exec > >(tee -a "$LOGDIR/system.log") 2>&1

FAILED=()
REL=$(rpm -E %fedora)

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*"; }
step() { # step "name" cmd...   — run, record failure, never abort
  local name=$1; shift
  log "$name"
  if "$@"; then return 0; fi
  warn "FAILED: $name"; FAILED+=("$name"); return 1
}
# Manifest lines minus comments/blank lines.
manifest() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e '/^[[:space:]]*$/d' "$1"; }
# Write stdin to a file only if the content differs (keeps mtime stable on re-runs).
put() { local f=$1 t; t=$(mktemp); cat > "$t"; if cmp -s "$t" "$f"; then rm -f "$t"; else install -D -m 0644 "$t" "$f"; rm -f "$t"; fi; }
in_chroot() { systemd-detect-virt --chroot >/dev/null 2>&1; }
# Human users (uid 1000-59999) — Anaconda has created them before %post runs.
human_users() { getent passwd | awk -F: '$3>=1000 && $3<60000 {print $1}'; }

# Bulk install; if the transaction fails, retry one at a time so a single bad
# package can't block the rest, and report exactly which ones failed.
dnf_install() {
  local pkgs=("$@")
  [ ${#pkgs[@]} -eq 0 ] && return 0
  dnf install -y --skip-unavailable "${pkgs[@]}" && return 0
  warn "bulk install failed; retrying individually"
  local p rc=0
  for p in "${pkgs[@]}"; do
    dnf install -y "$p" >/dev/null 2>&1 || { warn "could not install: $p"; rc=1; }
  done
  return $rc
}

# ---------------------------------------------------------------- guards
. /etc/os-release
if [ "${ID:-}" != "fedora" ]; then
  warn "This is ${PRETTY_NAME:-unknown}, not Fedora. Refusing (set FORCE=1 to override)."
  [ "${FORCE:-0}" = 1 ] || exit 1
fi
[ "$(id -u)" -eq 0 ] || { warn "run as root (sudo)"; exit 1; }
RUN_START=$(date +%s); touch "$STATE/.run-start"
log "fedora-deploy system setup — Fedora $REL — $(date -Is) — chroot=$(in_chroot && echo yes || echo no)"

# ---------------------------------------------------------------- dnf + repos
setup_dnf() {
  dnf config-manager setopt max_parallel_downloads=10 >/dev/null 2>&1 || true
  rpm -q dnf5-plugins >/dev/null || dnf install -y dnf5-plugins
}
setup_rpmfusion() {
  rpm -q rpmfusion-free-release rpmfusion-nonfree-release >/dev/null 2>&1 && return 0
  dnf install -y \
    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-${REL}.noarch.rpm" \
    "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-${REL}.noarch.rpm"
}
setup_cisco() { dnf config-manager setopt fedora-cisco-openh264.enabled=1; }
setup_vendor_repos() {
  install -m 0644 "$DEPLOY_DIR"/files/keys/*.asc /etc/pki/rpm-gpg/
  install -m 0644 "$DEPLOY_DIR"/repos/*.repo /etc/yum.repos.d/
  dnf makecache -y --refresh >/dev/null
}
step "dnf settings"          setup_dnf
step "RPM Fusion repos"      setup_rpmfusion
step "Cisco openh264 repo"   setup_cisco
step "vendor repos"          setup_vendor_repos
step "system upgrade"        dnf upgrade -y --refresh

# ---------------------------------------------------------------- packages
mapfile -t FEDORA_PKGS < <(manifest "$M/rpm-fedora.txt")
step "Fedora packages (${#FEDORA_PKGS[@]})" dnf_install "${FEDORA_PKGS[@]}"

swap_if() { # swap_if <from> <to>
  rpm -q "$2" >/dev/null 2>&1 && return 0
  if rpm -q "$1" >/dev/null 2>&1; then dnf swap -y "$1" "$2" --allowerasing
  else dnf install -y "$2"; fi
}
multimedia() {
  swap_if ffmpeg-free ffmpeg &&
  swap_if mesa-va-drivers mesa-va-drivers-freeworld &&
  swap_if mesa-vulkan-drivers mesa-vulkan-drivers-freeworld &&
  dnf update -y @multimedia --setopt=install_weak_deps=False --exclude=PackageKit-gstreamer-plugin
}
step "multimedia codecs + freeworld mesa (RPM Fusion)" multimedia
mapfile -t RPMF_PKGS < <(manifest "$M/rpm-rpmfusion.txt")
step "RPM Fusion packages" dnf_install "${RPMF_PKGS[@]}"

mapfile -t VENDOR_PKGS < <(manifest "$M/rpm-vendor.txt" | awk '{print $2}')
step "vendor packages (${#VENDOR_PKGS[@]})" dnf_install "${VENDOR_PKGS[@]}"

# umu-launcher: upstream Open-Wine-Components release RPM (not in Fedora 44).
umu() {
  rpm -q umu-launcher >/dev/null 2>&1 && return 0
  local url
  url=$(curl -fsSL https://api.github.com/repos/Open-Wine-Components/umu-launcher/releases/latest |
        grep -oE "https://[^\"]*umu-launcher-[^\"]*\.fc${REL}\.x86_64\.rpm" | head -1)
  [ -n "$url" ] || { warn "no fc${REL} x86_64 umu-launcher asset in latest release"; return 1; }
  dnf install -y "$url"
}
step "umu-launcher (upstream release)" umu

# ---------------------------------------------------------------- GPU (auto-detect)
# PCI display controllers: class 0300 (VGA) / 0302 (3D) / 0380 (display).
GPU_IDS=$(lspci -nn -d ::0300; lspci -nn -d ::0302; lspci -nn -d ::0380) 2>/dev/null
has_gpu() { grep -qi "\[$1:" <<<"$GPU_IDS"; }
# Navi 23 (gfx1032: 73e0-73ff) and Navi 24 (gfx1034: 7420-743f): Fedora's rocBLAS
# ships no kernels for these; they run as gfx1030 with HSA_OVERRIDE_GFX_VERSION.
needs_gfx_override() {
  grep -oiE '\[1002:(73[ef][0-9a-f]|74[23][0-9a-f])\]' <<<"$GPU_IDS" | grep -q .
}

amd_gpu() {
  dnf copr enable -y ilyaz/LACT &&         # LACT's own developer's COPR
  dnf install -y lact rocm-opencl rocm-hip rocminfo &&
  systemctl enable lactd.service
}
amd_override() {
  install -D -m 0644 "$DEPLOY_DIR/files/systemd/ollama.service.d/10-gfx-override.conf" \
    /etc/systemd/system/ollama.service.d/10-gfx-override.conf
  # ramalama: route AMD detection to the generic (Vulkan) llama.cpp image instead of ROCm.
  install -d /etc/ramalama
  cat > /etc/ramalama/ramalama.conf <<'EOF'
# fedora-deploy: this GPU (RDNA2 gfx1032/gfx1034) has no ROCm kernels in Fedora's
# rocBLAS, so use the Vulkan (RADV) llama.cpp image. Delete this file to restore defaults.
[ramalama]
image = "quay.io/ramalama/ramalama:latest"

[ramalama.images]
HIP_VISIBLE_DEVICES = "quay.io/ramalama/ramalama:latest"
EOF
  # Opt-in session-wide override for other HIP apps (Blender, Resolve): uncomment to enable.
  cat > /etc/profile.d/rocm-gfx-override.sh <<'EOF'
# fedora-deploy: uncomment to run HIP/ROCm apps on gfx1032/1034 as gfx1030 (unofficial).
# export HSA_OVERRIDE_GFX_VERSION=10.3.0
EOF
}
nvidia_gpu() { dnf install -y akmod-nvidia xorg-x11-drv-nvidia-cuda; }
intel_gpu()  { dnf install -y intel-media-driver; }

log "GPUs detected:"; echo "${GPU_IDS:-none}"
if has_gpu 1002; then
  step "AMD GPU: LACT + ROCm runtime" amd_gpu
  needs_gfx_override && step "AMD GPU: gfx1030 override (Navi 23/24)" amd_override
fi
has_gpu 10de && step "NVIDIA GPU: akmod-nvidia (RPM Fusion)" nvidia_gpu
has_gpu 8086 && step "Intel GPU: intel-media-driver" intel_gpu

# ---------------------------------------------------------------- udev / devices
game_devices_udev() { # upstream fabiscafe/game-devices-udev (Codeberg)
  local t; t=$(mktemp -d)
  curl -fsSL https://codeberg.org/fabiscafe/game-devices-udev/archive/main.tar.gz | tar xz -C "$t" &&
  install -C -m 0644 "$t"/game-devices-udev/src/*.rules /etc/udev/rules.d/ &&
  echo uinput | put /etc/modules-load.d/uinput.conf
  local rc=$?; rm -rf "$t"; return $rc
}
local_udev() {
  install -C -m 0644 "$DEPLOY_DIR"/files/udev/*.rules /etc/udev/rules.d/
  # ntsync: Wine/Proton sync primitive (in the Fedora kernel). Grant the seat user access.
  echo 'KERNEL=="ntsync", MODE="0660", TAG+="uaccess"' | put /etc/udev/rules.d/70-ntsync.rules
  echo ntsync | put /etc/modules-load.d/ntsync.conf
  getent group plugdev >/dev/null || groupadd -r plugdev   # ZSA rules use it
}
step "game controller udev rules (upstream)" game_devices_udev
step "local udev rules (ZSA, ntsync)"        local_udev
in_chroot || udevadm control --reload-rules 2>/dev/null || true

# ---------------------------------------------------------------- services / firewall
services() {
  systemctl set-default graphical.target
  # sshd is installed but left disabled; enable it yourself if you want remote logins.
  local u rc=0
  for u in sddm.service smartd.service fstrim.timer tailscaled.service; do
    systemctl enable "$u" >/dev/null 2>&1 || { warn "could not enable $u"; rc=1; }
  done
  return $rc
}
firewall() {
  local svc
  for svc in kdeconnect syncthing; do
    if in_chroot || ! systemctl is-active -q firewalld; then firewall-offline-cmd -q --add-service="$svc"
    else firewall-cmd -q --permanent --add-service="$svc" && firewall-cmd -q --reload; fi
  done
}
step "services" services
step "firewall (kdeconnect, syncthing)" firewall

# ---------------------------------------------------------------- snapper (btrfs)
snapper_setup() {
  [ "$(findmnt -no FSTYPE /)" = btrfs ] || { echo "root is not btrfs; skipping"; return 0; }
  [ -f /etc/snapper/configs/root ] || snapper --no-dbus -c root create-config /
  if [ "$(findmnt -no FSTYPE /home)" = btrfs ] && [ ! -f /etc/snapper/configs/home ]; then
    snapper --no-dbus -c home create-config /home
  fi
  local c
  for c in /etc/snapper/configs/root /etc/snapper/configs/home; do
    [ -f "$c" ] || continue
    sed -i -e 's/^TIMELINE_LIMIT_HOURLY=.*/TIMELINE_LIMIT_HOURLY="5"/' \
           -e 's/^TIMELINE_LIMIT_DAILY=.*/TIMELINE_LIMIT_DAILY="7"/' \
           -e 's/^TIMELINE_LIMIT_WEEKLY=.*/TIMELINE_LIMIT_WEEKLY="2"/' \
           -e 's/^TIMELINE_LIMIT_MONTHLY=.*/TIMELINE_LIMIT_MONTHLY="1"/' \
           -e 's/^TIMELINE_LIMIT_YEARLY=.*/TIMELINE_LIMIT_YEARLY="0"/' "$c"
  done
  systemctl enable snapper-timeline.timer snapper-cleanup.timer
}
step "snapper snapshots" snapper_setup

# Anaconda sets compress=zstd:1 on btrfs by default; make sure.
btrfs_compress() {
  grep -qE '\sbtrfs\s' /etc/fstab || return 0
  grep -E '\sbtrfs\s' /etc/fstab | grep -q 'compress=' && return 0
  sed -i -E '/\sbtrfs\s/ s/(subvol=[^, \t]+)/\1,compress=zstd:1/' /etc/fstab
}
step "btrfs compression in fstab" btrfs_compress

# ---------------------------------------------------------------- users / groups
user_groups() {
  local u g
  for u in $(human_users); do
    for g in libvirt wireshark plugdev render video; do
      getent group "$g" >/dev/null || continue
      id -nG "$u" | tr ' ' '\n' | grep -qx "$g" || usermod -aG "$g" "$u"
    done
    echo "groups for $u: $(id -nG "$u")"
  done
}
step "user groups" user_groups

# ---------------------------------------------------------------- cleanup
remove_pkgs() {
  local p; for p in $(manifest "$M/rpm-remove.txt"); do
    rpm -q "$p" >/dev/null 2>&1 && dnf remove -y "$p"
  done; return 0
}
step "remove unwanted packages" remove_pkgs

# ---------------------------------------------------------------- reboot needed?
# New kernel/Mesa/drivers, udev rules, modules-load entries and group membership
# only take full effect after a reboot. Flag it if anything relevant changed.
reboot_check() {
  local marker="$STATE/reboot-required" running newest boot
  boot=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
  # A marker from before the current boot is stale (you already rebooted).
  [ -f "$marker" ] && [ "$(stat -c %Y "$marker")" -lt "$boot" ] && rm -f "$marker"
  running=$(uname -r)
  newest=$(rpm -q --last kernel-core 2>/dev/null | head -1 | awk '{print $1}' | sed 's/^kernel-core-//')
  if in_chroot || [ "$newest" != "$running" ] ||
     [ -n "$(find /etc/udev/rules.d /etc/modules-load.d /etc/group -newer "$STATE/.run-start" 2>/dev/null)" ] ||
     [ -n "$(rpm -qa --qf '%{INSTALLTIME} %{NAME}\n' | awk -v t="$RUN_START" '$1>=t' |
             grep -E ' (mesa-|akmod-|kmod-|lact|rocm|kernel)' )" ]; then
    date -Is > "$marker"; echo "reboot required"
  else
    echo "no reboot needed"
  fi
}
step "reboot check" reboot_check

# Baseline for inventory.sh drift detection (first run only).
[ -f "$STATE/baseline-rpms.txt" ] || rpm -qa --qf '%{NAME}\n' | sort -u > "$STATE/baseline-rpms.txt"

# ---------------------------------------------------------------- summary
log "done — $(date -Is)"
if [ ${#FAILED[@]} -gt 0 ]; then
  printf '!!  %d step(s) failed (re-run this script after fixing network/repos):\n' "${#FAILED[@]}"
  printf '    - %s\n' "${FAILED[@]}" | tee "$STATE/failed-steps.txt"
else
  rm -f "$STATE/failed-steps.txt"; echo "all steps OK"
fi
exit 0   # never fail the kickstart; failures are recorded above
