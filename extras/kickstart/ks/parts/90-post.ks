# 1) Copy the deploy kit from the install USB into the new system.
%post --nochroot --interpreter=/usr/bin/bash --log=/mnt/sysroot/root/fedora-deploy-post-nochroot.log
set -x
SRC=""
for d in /run/install/repo /run/install/source /run/install/isodir /run/install/sources/mount-*; do
  [ -d "$d/fedora-deploy" ] && { SRC="$d/fedora-deploy"; break; }
done
if [ -z "$SRC" ]; then   # mount the USB by the label mkksiso put in inst.ks=hd:LABEL=...
  label=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^inst\.\(ks\|stage2\)=hd:LABEL=\([^:]*\).*/\2/p' | head -1)
  label=$(printf '%b' "$label")
  dev=$(blkid -L "$label" 2>/dev/null)
  mkdir -p /tmp/deploy-media
  [ -n "$dev" ] && mount -o ro "$dev" /tmp/deploy-media && [ -d /tmp/deploy-media/fedora-deploy ] &&
    SRC=/tmp/deploy-media/fedora-deploy
fi
if [ -n "$SRC" ]; then
  mkdir -p /mnt/sysroot/opt && cp -a "$SRC" /mnt/sysroot/opt/ && chmod +x /mnt/sysroot/opt/fedora-deploy/scripts/*.sh /mnt/sysroot/opt/fedora-deploy/*.sh
else
  echo "fedora-deploy directory NOT found on install media" > /mnt/sysroot/root/FEDORA-DEPLOY-MISSING
fi
%end

# 2) Run the system setup inside the new system (network is up during %post).
%post --interpreter=/usr/bin/bash --log=/root/fedora-deploy-post.log
if [ -f /opt/fedora-deploy/scripts/deploy-system.sh ]; then
  bash /opt/fedora-deploy/scripts/deploy-system.sh
  # Kickstart path only: run deploy-user.sh in Konsole at each user's first login.
  chmod +x /opt/fedora-deploy/extras/kickstart/firstboot-launch.sh
  install -m 0644 /opt/fedora-deploy/extras/kickstart/firstboot.desktop /etc/xdg/autostart/fedora-deploy-firstboot.desktop
else
  echo "deploy kit missing; run it manually after boot" >&2
fi
%end
