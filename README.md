# fedora-deploy

Turns a **stock Fedora 44 KDE** install into my full setup with one command. Packages come only from Fedora, RPM Fusion, Flathub, vendors' own repos, and upstream authors' own COPRs or releases. Nothing from Nobara or PikaOS.

## Use it

1. **Install Fedora 44 KDE** from the official download (fedoraproject.org/kde). Pick the disk and create your user in the installer, then reboot and log in.
2. **Open Konsole and run:**
   ```bash
   curl -fsSL https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/bootstrap.sh | bash
   ```
   It asks only for your sudo password, then runs unattended, usually 20–40 minutes depending on bandwidth.
3. **Reboot** if it says so, then work through `~/Desktop/fedora-deploy-TODO.txt`. That file covers the things a script can't do: sign-ins and the optional DaVinci Resolve install.

## What it does
```
bootstrap.sh
 ├─ clones this repo to ~/.local/share/fedora-deploy (or git pull on re-runs)
 ├─ sudo scripts/deploy-system.sh   RPM Fusion + vendor repos, ~250 RPMs, codecs and freeworld Mesa,
 │                                  GPU auto-detect (AMD LACT/ROCm, NVIDIA akmod, Intel VA-API),
 │                                  controller udev rules, snapper, firewall, services, groups
 ├─ scripts/deploy-user.sh          Flatpaks (Steam + Vulkan layers, OBS + plugins, …), nvm/Node,
 │                                  npm globals, Claude Code, uv tools, agent-SDK venv, templates,
 │                                  VS Code extensions, Keymapp, shell config, Syncthing
 └─ scripts/inventory.sh            what's missing, what's extra, and a Nobara/PikaOS check
```

## Re-run / update
Every step is idempotent, so re-running only installs what's missing:
```bash
~/.local/share/fedora-deploy/bootstrap.sh            # pull latest manifests + apply
~/.local/share/fedora-deploy/bootstrap.sh --user-only
~/.local/share/fedora-deploy/scripts/inventory.sh    # drift check only (read-only)
```
Options: `--system-only`, `--user-only`, `--no-pull`. With the one-liner, pass them after `| bash -s --`.

To change what gets installed, edit a manifest, commit, and push. The next run on any machine picks it up.

## Manifests (single source of truth)
| file | installed by |
|---|---|
| `rpm-fedora.txt`, `rpm-rpmfusion.txt` | deploy-system.sh |
| `rpm-vendor.txt` (repo files in `repos/`) | deploy-system.sh |
| `rpm-remove.txt` | deploy-system.sh |
| `flatpak.txt`, `flatpak-steam-extensions.txt` | deploy-user.sh (Flathub, `--user`) |
| `uv-tools.txt`, `npm-global.txt`, `vscode-extensions.txt` | deploy-user.sh |
| `agent-sdk-python.txt` → `~/.venvs/agents` (`agents` alias) | deploy-user.sh |
| `agent-sdk-node.txt` → `templates/node-agent` | per project |
| `forbidden.txt` (nobara, pikaos, snapd) | checked by inventory.sh |

## GPU notes
- **AMD:** LACT (from its developer's COPR `ilyaz/LACT`) and the ROCm runtime.
- **RX 6600/6600 XT (Navi 23, gfx1032) and Navi 24:** Fedora's rocBLAS ships no kernels for these, and AMD doesn't support them in ROCm. On these cards the script does three things:
  - ramalama uses the **Vulkan** llama.cpp image (`/etc/ramalama/ramalama.conf`)
  - ollama gets `HSA_OVERRIDE_GFX_VERSION=10.3.0`
  - an opt-in session-wide override is left in `/etc/profile.d/rocm-gfx-override.sh`
- **NVIDIA:** `akmod-nvidia` from RPM Fusion. With Secure Boot on, enroll the MOK key after the reboot.
- **Intel:** `intel-media-driver`.

## Optional: fully unattended install
`extras/kickstart/` holds a kickstart and a USB builder that do the install *and* this setup in one pass. It isn't needed for the normal flow. See its README.
