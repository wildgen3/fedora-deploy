#!/usr/bin/env bash
# Autostart hook: runs deploy-user.sh once per user, in a visible Konsole window.
STAMP="$HOME/.local/state/fedora-deploy/done"
[ -f "$STAMP" ] && exit 0
[ "$(id -u)" -ge 1000 ] || exit 0
SCRIPT=/opt/fedora-deploy/scripts/deploy-user.sh
sleep 5   # let the Plasma session and network settle
if command -v konsole >/dev/null; then
  exec konsole --hold -p tabtitle="fedora-deploy first login" -e bash "$SCRIPT"
else
  exec bash "$SCRIPT"
fi
