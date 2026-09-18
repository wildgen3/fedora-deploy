#!/usr/bin/env bash
# inventory.sh — compare this machine against the manifests. Read-only.
#   MISSING : in a manifest but not installed
#   EXTRA   : installed by you but not in any manifest (add it, or ignore it)
#   FORBIDDEN: anything Nobara/PikaOS/snapd — exit code 2 if found
# Also useful on a machine you are about to wipe, as a "before" scan.
set -uo pipefail
DEPLOY_DIR=${DEPLOY_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}
M="$DEPLOY_DIR/manifests"
manifest() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e '/^[[:space:]]*$/d' "$1"; }
hdr() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
list() { if [ -n "$1" ]; then sed 's/^/   /' <<<"$1"; else echo "   (none)"; fi; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

rpm -qa --qf '%{NAME}\n' | sort -u > "$T/rpms"
{ manifest "$M/rpm-fedora.txt"; manifest "$M/rpm-rpmfusion.txt"; manifest "$M/rpm-vendor.txt" | awk '{print $2}'; } |
  grep -v '^@' | sed 's/\.i686$//' | sort -u > "$T/want-rpms"

# ---------------------------------------------------------------- forbidden
hdr "FORBIDDEN (must be empty on the new system)"
pat=$(manifest "$M/forbidden.txt" | paste -sd'|')
bad_rpms=$(grep -Ei "$pat" "$T/rpms" || true)
bad_repos=$(dnf repolist --enabled 2>/dev/null | awk 'NR>1{print $1}' | grep -Ei "$pat" || true)
bad_copr=$(grep -lEi "$pat" /etc/yum.repos.d/*.repo 2>/dev/null | xargs -r -n1 basename || true)
list "$(printf '%s\n' ${bad_rpms:+"packages:"} $bad_rpms ${bad_repos:+"enabled repos:"} $bad_repos ${bad_copr:+"repo files:"} $bad_copr | sed '/^$/d')"
[ -z "$bad_rpms$bad_repos$bad_copr" ] && forbidden=0 || forbidden=1

# ---------------------------------------------------------------- rpm
hdr "RPM: MISSING from manifests"
list "$(comm -23 "$T/want-rpms" "$T/rpms")"

hdr "RPM: EXTRA (user-installed, not in manifests)"
dnf repoquery --userinstalled --qf '%{name}\n' 2>/dev/null | sort -u > "$T/user"
if [ -f /var/lib/fedora-deploy/baseline-rpms.txt ]; then
  comm -23 "$T/user" /var/lib/fedora-deploy/baseline-rpms.txt > "$T/user2"
else
  echo "   (no baseline yet — this machine wasn't built by fedora-deploy; showing all user-installed)"
  cp "$T/user" "$T/user2"
fi
list "$(comm -23 "$T/user2" "$T/want-rpms" | grep -Ev '^(kernel|gpg-pubkey)')"

# ---------------------------------------------------------------- flatpak
if command -v flatpak >/dev/null; then
  flatpak list --app --columns=application | sort -u > "$T/fp"
  manifest "$M/flatpak.txt" | grep -v '\.Plugin\.' | sort -u > "$T/want-fp"
  hdr "FLATPAK: MISSING";  list "$(comm -23 "$T/want-fp" "$T/fp")"
  hdr "FLATPAK: EXTRA";    list "$(comm -13 "$T/want-fp" "$T/fp")"
fi

# ---------------------------------------------------------------- user tools
if command -v uv >/dev/null; then
  uv tool list 2>/dev/null | awk '/^[^- ]/{print $1}' | sort -u > "$T/uv"
  manifest "$M/uv-tools.txt" | sort -u > "$T/want-uv"
  hdr "UV TOOLS: MISSING"; list "$(comm -23 "$T/want-uv" "$T/uv")"
  hdr "UV TOOLS: EXTRA";   list "$(comm -13 "$T/want-uv" "$T/uv")"
fi
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1
if command -v npm >/dev/null; then
  npm ls -g --depth=0 --parseable 2>/dev/null | tail -n +2 | sed 's|.*/node_modules/||' | sort -u > "$T/npm"
  manifest "$M/npm-global.txt" | sort -u > "$T/want-npm"
  hdr "NPM GLOBAL: MISSING"; list "$(comm -23 "$T/want-npm" "$T/npm")"
  hdr "NPM GLOBAL: EXTRA";   list "$(comm -13 "$T/want-npm" "$T/npm" | grep -vx -e npm -e corepack)"
fi
if command -v code >/dev/null; then
  code --list-extensions 2>/dev/null | tr 'A-Z' 'a-z' | sort -u > "$T/vs"
  manifest "$M/vscode-extensions.txt" | tr 'A-Z' 'a-z' | sort -u > "$T/want-vs"
  hdr "VS CODE: MISSING"; list "$(comm -23 "$T/want-vs" "$T/vs")"
  hdr "VS CODE: EXTRA";   list "$(comm -13 "$T/want-vs" "$T/vs")"
fi

hdr "LOOSE BINARIES in ~/.local/bin (not managed by uv/claude/keymapp)"
list "$(find "$HOME/.local/bin" -maxdepth 1 -type f -perm -u+x -printf "%f\n" 2>/dev/null |
        grep -vxFf <(uv tool list 2>/dev/null | awk '/^- /{print $2}') | grep -vx -e claude -e keymapp || true)"

echo
if [ "$forbidden" = 1 ]; then echo "RESULT: FORBIDDEN packages/repos present"; exit 2; fi
echo "RESULT: no forbidden packages or repos"
