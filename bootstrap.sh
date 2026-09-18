#!/usr/bin/env bash
# bootstrap.sh — turn a stock Fedora 44 KDE install into my full setup.
#
#   curl -fsSL https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/bootstrap.sh | bash
#
# Options (after `| bash -s --`, or when run from a checkout):
#   --system-only  only the root part (repos, RPMs, GPU, udev, services)
#   --user-only    only the per-user part (flatpaks, node, uv, VS Code, ...)
#   --no-pull      don't update an existing checkout
# Asks nothing except your sudo password. Safe to re-run: every step is idempotent.
#
# Everything lives in main() and runs on the last line, so a partially
# downloaded script can't execute half-way.
set -euo pipefail

REPO_URL=${FEDORA_DEPLOY_REPO:-https://github.com/wildgen3/fedora-deploy.git}
DIR=${FEDORA_DEPLOY_DIR:-$HOME/.local/share/fedora-deploy}

DO_SYSTEM=1 DO_USER=1 PULL=1
say()  { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
get_repo() {
  # Running from a checkout (not piped)? Use it as-is.
  local self_dir=""
  [ -f "${BASH_SOURCE[0]:-}" ] && self_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  if [ -n "$self_dir" ] && [ -f "$self_dir/scripts/deploy-system.sh" ]; then
    DIR=$self_dir; say "using local checkout $DIR"; return 0
  fi
  command -v git >/dev/null || sudo dnf install -y git
  if [ -d "$DIR/.git" ]; then
    if [ "$PULL" = 1 ]; then say "updating $DIR"; git -C "$DIR" pull --ff-only; fi
  else
    say "cloning $REPO_URL -> $DIR"
    mkdir -p "$(dirname "$DIR")"
    git clone --depth 1 "$REPO_URL" "$DIR"
  fi
}

main() {
  local a
  for a in "$@"; do
    case $a in
      --system-only) DO_USER=0 ;;
      --user-only)   DO_SYSTEM=0 ;;
      --no-pull)     PULL=0 ;;
      -h|--help)     sed -n '2,15p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; exit 0 ;;
      *) die "unknown option: $a" ;;
    esac
  done

  # --- guards
  [ "$(id -u)" -ne 0 ] || die "run as your normal user (it uses sudo when needed), not root"
  # shellcheck disable=SC1091
  . /etc/os-release
  if [ "${ID:-}" != fedora ]; then
    [ "${FORCE:-0}" = 1 ] || die "this is ${PRETTY_NAME:-an unknown OS}, not Fedora — refusing (FORCE=1 overrides)"
  fi
  [ "${VERSION_ID:-0}" = 44 ] || warn "built and tested for Fedora 44; this is Fedora ${VERSION_ID:-?}"
  rpm -q plasma-workspace >/dev/null 2>&1 || warn "KDE Plasma not detected; continuing anyway"
  curl -fsS --max-time 10 -o /dev/null https://github.com || die "no network (can't reach github.com)"

  # When piped (curl | bash) stdin is the script; give child processes the terminal.
  if [ -r /dev/tty ]; then exec </dev/tty; fi

  # --- sudo once, kept alive for the whole run
  if [ "$DO_SYSTEM" = 1 ]; then
    say "sudo password (asked once)"
    sudo -v || die "sudo failed"
    ( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done ) &
    KEEPALIVE=$!
    trap 'kill "$KEEPALIVE" 2>/dev/null || true' EXIT
  fi

  get_repo

  if [ "$DO_SYSTEM" = 1 ]; then
    say "system setup (this is the long part: repos, ~250 packages, codecs, GPU)"
    sudo bash "$DIR/scripts/deploy-system.sh"
  fi
  if [ "$DO_USER" = 1 ]; then
    say "user setup (flatpaks, node, uv, VS Code, agent SDKs, ...)"
    bash "$DIR/scripts/deploy-user.sh"
  fi

  say "inventory check"
  local inv=0
  bash "$DIR/scripts/inventory.sh" || inv=$?

  echo
  say "summary"
  if [ -f /var/lib/fedora-deploy/failed-steps.txt ]; then
    warn "system steps that failed:"; sed 's/^/     /' /var/lib/fedora-deploy/failed-steps.txt
  fi
  [ "$inv" = 2 ] && warn "forbidden (Nobara/PikaOS/snapd) packages or repos are present — see above"
  echo "   logs:   /var/log/fedora-deploy/system.log   ~/.local/state/fedora-deploy/user.log"
  echo "   to-do:  ~/Desktop/fedora-deploy-TODO.txt"
  echo "   re-run: $DIR/bootstrap.sh"
  local m=/var/lib/fedora-deploy/reboot-required boot
  boot=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
  if [ -f "$m" ] && [ "$(stat -c %Y "$m")" -ge "$boot" ]; then
    warn "REBOOT required (new kernel/drivers/udev rules/groups)"
  fi
}

# Same line on purpose: after main switches stdin to the tty, bash must not read on.
main "$@"; exit $?
