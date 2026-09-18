# Optional: unattended install via kickstart

**You don't need this.** The normal flow is a stock Fedora KDE install followed by `bootstrap.sh`. Use this only if you want the install *and* the setup to happen in one pass from a USB stick. It passes `ksvalidator`, but it hasn't been tested on hardware yet. Test it in a VM first.

## What it does
- **`ks/parts/10-pre-disk.ks`** chooses the ONE disk to erase. It never touches USB or removable disks, the install media, disks under 64 GiB, or disks with NTFS, HFS+, exFAT, APFS, BitLocker or LUKS on them. It picks a disk only when there's one obvious choice:
  - the `inst.deploy.disk=` boot argument names it,
  - it's the only disk with an existing Linux install, or
  - it's the only internal disk.

  Otherwise Anaconda asks you.
- **`00-base.ks`** presets language, timezone and network, but has no user or root lines. Anaconda therefore stops on the summary screen, where you create your user and confirm the disk.
- **`90-post.ks`** copies this repo to `/opt/fedora-deploy`, runs the same `scripts/deploy-system.sh`, and installs a first-login hook (`firstboot-launch.sh`). That hook runs `deploy-user.sh` in Konsole.

## Build the USB
```bash
sudo dnf install lorax xorriso pykickstart
extras/kickstart/build-iso.sh          # downloads and verifies the F44 netinstall ISO, bakes in ks + repo
# write build/fedora44-deploy.iso with Fedora Media Writer
EXTRA_KARGS="inst.deploy.disk=nvme0n1" extras/kickstart/build-iso.sh   # force a target disk
```
It uses the **Everything netinstall** ISO, because the KDE live image can't run a full kickstart. The installer needs network access.
