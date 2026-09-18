#!/usr/bin/env bash
# deploy-user.sh — per-user setup. Runs as YOUR user (not root). Normally run by
# ../bootstrap.sh right after deploy-system.sh; safe to re-run any time:
#     ~/.local/share/fedora-deploy/scripts/deploy-user.sh
set -uo pipefail

DEPLOY_DIR=${DEPLOY_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}
M="$DEPLOY_DIR/manifests"
STATE="$HOME/.local/state/fedora-deploy"
mkdir -p "$STATE" "$HOME/.local/bin" "$HOME/.bashrc.d"
exec > >(tee -a "$STATE/user.log") 2>&1

NODE_MAJOR=${NODE_MAJOR:-22}
AGENTS_VENV="$HOME/.venvs/agents"

FAILED=()
log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*"; }
step() { local n=$1; shift; log "$n"; "$@" || { warn "FAILED: $n"; FAILED+=("$n"); }; }
manifest() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' -e '/^[[:space:]]*$/d' "$1"; }

[ "$(id -u)" -ne 0 ] || { warn "run as your normal user, not root"; exit 1; }
log "fedora-deploy user setup for $USER — $(date -Is)"
[ -f /var/lib/fedora-deploy/failed-steps.txt ] &&
  warn "system setup had failures (see /var/lib/fedora-deploy/failed-steps.txt); re-run: sudo $DEPLOY_DIR/scripts/deploy-system.sh"

# ---------------------------------------------------------------- flatpaks
flatpaks() {
  flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || return 1
  mapfile -t apps < <(manifest "$M/flatpak.txt")
  flatpak install --user -y --noninteractive --or-update flathub "${apps[@]}"
}
steam_extensions() {
  # Match the extension branch to Steam's runtime (e.g. org.freedesktop.Platform/x86_64/25.08).
  local branch ext rc=0
  branch=$(flatpak info --user com.valvesoftware.Steam 2>/dev/null | awk '/Runtime:/{n=split($2,a,"/"); print a[n]}')
  [ -n "$branch" ] || { warn "Steam flatpak not installed"; return 1; }
  for ext in $(manifest "$M/flatpak-steam-extensions.txt"); do
    flatpak install --user -y --noninteractive --or-update flathub "$ext//$branch" || { warn "$ext//$branch unavailable"; rc=1; }
  done
  return $rc
}
step "Flatpak apps (Flathub, --user)" flatpaks
step "Steam Vulkan layers (MangoHud, gamescope, vkBasalt)" steam_extensions

# ---------------------------------------------------------------- shell config
shell_config() {
  install -m 0644 "$DEPLOY_DIR"/files/bashrc.d/*.sh "$HOME/.bashrc.d/"
  # Fedora's default ~/.bashrc already sources ~/.bashrc.d/*; add it if missing.
  grep -q 'bashrc.d' "$HOME/.bashrc" 2>/dev/null ||
    printf '\nfor rc in ~/.bashrc.d/*; do [ -f "$rc" ] && . "$rc"; done; unset rc\n' >> "$HOME/.bashrc"
  # atuin's bash integration needs bash-preexec (not packaged in Fedora 44).
  curl -fsSL -o "$HOME/.local/share/bash-preexec.sh" \
    https://raw.githubusercontent.com/rcaloras/bash-preexec/master/bash-preexec.sh
}
git_config() {
  git config --global init.defaultBranch main
  if command -v delta >/dev/null; then
    git config --global core.pager delta
    git config --global interactive.diffFilter 'delta --color-only'
  fi
}
step "shell config (~/.bashrc.d)" shell_config
step "git config" git_config

# ---------------------------------------------------------------- node / npm
node_setup() {
  export NVM_DIR="$HOME/.nvm"
  if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    local tag
    tag=$(curl -fsSL https://api.github.com/repos/nvm-sh/nvm/releases/latest | grep -oE '"tag_name": *"[^"]+"' | cut -d'"' -f4)
    git clone --quiet --depth 1 --branch "${tag:-master}" https://github.com/nvm-sh/nvm.git "$NVM_DIR" || return 1
  fi
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh"
  nvm install "$NODE_MAJOR" && nvm alias default "$NODE_MAJOR" || return 1
  mapfile -t pkgs < <(manifest "$M/npm-global.txt")
  npm install -g "${pkgs[@]}"
}
step "nvm + Node $NODE_MAJOR + npm globals" node_setup

claude_code() {
  command -v claude >/dev/null && return 0
  curl -fsSL https://claude.ai/install.sh | bash
}
step "Claude Code (native installer)" claude_code

# ---------------------------------------------------------------- python / uv
uv_tools() {
  local t rc=0
  for t in $(manifest "$M/uv-tools.txt"); do
    uv tool install --quiet "$t" 2>/dev/null || uv tool upgrade "$t" || { warn "uv tool: $t"; rc=1; }
  done
  return $rc
}
agents_venv() {
  [ -x "$AGENTS_VENV/bin/python" ] || uv venv --python 3.13 "$AGENTS_VENV" || return 1
  uv pip install --python "$AGENTS_VENV/bin/python" --upgrade -r <(manifest "$M/agent-sdk-python.txt") || return 1
  "$AGENTS_VENV/bin/python" -m ipykernel install --user --name agents --display-name "Python (agents)"
}
templates() {
  mkdir -p "$HOME/dev/templates"
  cp -rn "$DEPLOY_DIR"/templates/* "$HOME/dev/templates/"
}
step "uv tools" uv_tools
step "agent SDK venv (~/.venvs/agents)" agents_venv
step "agent project templates (~/dev/templates)" templates

# ---------------------------------------------------------------- editors / apps
vscode_ext() {
  command -v code >/dev/null || { warn "VS Code not installed"; return 1; }
  local have e rc=0
  have=$(code --list-extensions 2>/dev/null | tr 'A-Z' 'a-z')
  for e in $(manifest "$M/vscode-extensions.txt"); do
    grep -qx "$(tr 'A-Z' 'a-z' <<<"$e")" <<<"$have" && continue
    code --install-extension "$e" >/dev/null 2>&1 || { warn "VS Code extension not found: $e"; rc=1; }
  done
  return $rc
}
keymapp() { # ZSA Keymapp (vendor tarball; udev rule installed by deploy-system.sh)
  local d="$HOME/.local/share/keymapp"
  mkdir -p "$d" "$HOME/.local/share/applications"
  curl -fsSL https://oryx.nyc3.cdn.digitaloceanspaces.com/keymapp/keymapp-latest.tar.gz | tar xz -C "$d" || return 1
  chmod +x "$d/keymapp"
  ln -sf "$d/keymapp" "$HOME/.local/bin/keymapp"
  cat > "$HOME/.local/share/applications/keymapp.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Keymapp
Comment=ZSA keyboard configurator
Exec=$d/keymapp
Icon=$d/icon.png
Categories=Utility;
EOF
}
syncthing_user() { systemctl --user enable --now syncthing.service; }
step "VS Code extensions" vscode_ext
step "ZSA Keymapp" keymapp
step "Syncthing (user service)" syncthing_user

# ---------------------------------------------------------------- manual to-do
TODO="$HOME/Desktop/fedora-deploy-TODO.txt"
mkdir -p "$HOME/Desktop"
cat > "$TODO" <<'EOF'
fedora-deploy — things that can't be automated
================================================
Sign-ins
  [ ] git config --global user.name "..." && git config --global user.email "..."
  [ ] gcloud auth login && gcloud auth application-default login
  [ ] gh auth login
  [ ] sudo tailscale up            (then open Trayscale)
  [ ] Steam, Discord, Spotify, Heroic, Chrome/Firefox sync
  [ ] VS Code Settings Sync, Claude Code (`claude`), Codex (`codex`), Antigravity, ChatGPT
Apps without a repo
  [ ] (optional) DaVinci Resolve: Blackmagic's installer. On AMD, the ROCm runtime is installed; RX 6600-class
      cards (gfx1032) aren't officially supported, so check `clinfo` and /etc/profile.d/rocm-gfx-override.sh
Gaming
  [ ] Open ProtonPlus and install GE-Proton for Steam (Flatpak) and Heroic
  [ ] Steam > Settings > Compatibility: enable Steam Play for all titles
Local AI (on AMD gfx1032/1034 cards, see /etc/ramalama/ramalama.conf)
  [ ] ramalama run llama3.2      (Vulkan image)
  [ ] sudo systemctl enable --now ollama   (ROCm with gfx override) — compare tokens/s, keep the faster
Check
  [ ] ~/.local/share/fedora-deploy/scripts/inventory.sh   (drift + no-Nobara check)
EOF

log "done — $(date -Is)"
if [ ${#FAILED[@]} -gt 0 ]; then
  printf '!!  %d step(s) failed; re-run %s after fixing:\n' "${#FAILED[@]}" "$0"
  printf '    - %s\n' "${FAILED[@]}"
else
  echo "all steps OK"
fi
touch "$STATE/done"   # used by the optional kickstart first-login hook
echo "Manual to-do list: $TODO"
