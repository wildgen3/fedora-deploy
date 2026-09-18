%pre --interpreter=/usr/bin/bash --log=/tmp/fedora-deploy-pre-disk.log
# Pick the ONE disk to wipe. Rules:
#   0. boot arg inst.deploy.disk=<name> wins (e.g. inst.deploy.disk=nvme0n1)
#   never: removable/hotplug/USB disks, the install media (iso9660), disks < 64 GiB,
#          disks holding NTFS / HFS+ / exFAT / APFS / BitLocker / LUKS (data or Windows)
#   1. exactly one remaining disk already holds a Linux install (btrfs/ext4/xfs/LVM) -> use it
#   2. no Linux disks, and exactly one eligible internal disk exists            -> use it
#   otherwise write NO storage config, so Anaconda makes you choose by hand.
OUT=${DEPLOY_PART_OUT:-/tmp/part.ks}
MIN=$((64 * 1024 * 1024 * 1024))
say() { echo "[deploy-disk] $*"; }
: > "$OUT"

arg=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^inst\.deploy\.disk=//p' | tail -1)
arg=${arg#/dev/}

linux=() eligible=() internal=0
while read -r name size rm hotplug tran; do
  case $name in zram*|loop*|sr*|fd*) continue ;; esac
  fst=$(lsblk -nro FSTYPE "/dev/$name" 2>/dev/null | sort -u | tr '\n' ' ')
  if [ "$rm" = 1 ] || [ "$hotplug" = 1 ] || [ "$tran" = usb ]; then
    say "skip $name: removable/usb ($tran) [$fst]"; continue; fi
  if [[ $fst == *iso9660* ]]; then say "skip $name: install media"; continue; fi
  if [ "$size" -lt "$MIN" ]; then say "skip $name: smaller than 64 GiB"; continue; fi
  internal=$((internal + 1))
  if [[ $fst =~ (ntfs|hfsplus|hfs|exfat|apfs|BitLocker|crypto_LUKS) ]]; then
    say "PROTECT $name: holds data filesystem(s) [$fst]"; continue; fi
  eligible+=("$name")
  if [[ $fst =~ (btrfs|ext4|xfs|LVM2_member) ]]; then linux+=("$name"); say "candidate $name: existing Linux [$fst]"
  else say "candidate $name: no Linux filesystem [$fst]"; fi
done < <(lsblk -dnbo NAME,SIZE,RM,HOTPLUG,TRAN -e 7,11)

target=""
if [ -n "$arg" ]; then
  if lsblk -dn "/dev/$arg" >/dev/null 2>&1; then target=$arg; say "using boot arg inst.deploy.disk=$arg"
  else say "boot arg disk '$arg' not found; falling back to manual selection"; fi
elif [ ${#linux[@]} -eq 1 ]; then target=${linux[0]}; say "rule 1: the only disk with an existing Linux install"
elif [ ${#linux[@]} -eq 0 ] && [ ${#eligible[@]} -eq 1 ] && [ "$internal" -eq 1 ]; then
  target=${eligible[0]}; say "rule 2: the only internal disk"
else
  say "ambiguous (linux=${linux[*]:-none} eligible=${eligible[*]:-none} internal=$internal): leaving storage to you"
fi

if [ -n "$target" ]; then
  say "TARGET = /dev/$target ($(lsblk -dno SIZE,MODEL "/dev/$target" | xargs)) — will be ERASED"
  cat > "$OUT" <<PART
ignoredisk --only-use=$target
zerombr
clearpart --all --initlabel --drives=$target
reqpart --add-boot
part btrfs.01 --fstype=btrfs --grow --ondisk=$target
btrfs none --label=fedora btrfs.01
btrfs / --subvol --name=root LABEL=fedora
btrfs /home --subvol --name=home LABEL=fedora
PART
fi
%end
