#!/usr/bin/env bash
# bootstrap.sh - get (or update) the desktop post-install files, then run it.
#
#   curl -fsSL https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/desktop/bootstrap.sh | bash
#   curl -fsSL .../bootstrap.sh | bash -s -- --dry-run      # pass options through
#
# Installs git if it's missing (core tool), clones the repo to
# ~/.local/share/desktop-postinstall/repo (or pulls the latest), and starts
# desktop/postinstall.py from there so the script and its package lists
# always come from the same version.
set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/wildgen3/fedora-deploy.git}
BRANCH=${BRANCH:-main}
DIR="$HOME/.local/share/desktop-postinstall/repo"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not root." >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "==> Installing git (asks for your password once)"
    sudo dnf install -y git
fi

if [ -d "$DIR/.git" ]; then
    echo "==> Updating $DIR"
    git -C "$DIR" fetch --quiet origin "$BRANCH"
    git -C "$DIR" checkout --quiet "$BRANCH"
    git -C "$DIR" merge --quiet --ff-only "origin/$BRANCH"
else
    echo "==> Downloading to $DIR"
    mkdir -p "$(dirname "$DIR")"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR"
fi

# The script asks questions; read answers from the terminal, not this pipe.
exec python3 "$DIR/desktop/postinstall.py" "$@" </dev/tty
