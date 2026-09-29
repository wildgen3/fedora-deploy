# desktop/postinstall.py

Personal software and tooling for a fresh **Fedora KDE** desktop. It's separate from the server scripts and has its own flow. Everything comes from Fedora, RPM Fusion, the vendor's own repo, the developer's own COPR, or Flathub.

## Run it

Install Fedora KDE, log in, open Konsole:

```bash
curl -fsSLO https://raw.githubusercontent.com/wildgen3/fedora-deploy/main/desktop/postinstall.py
python3 postinstall.py --dry-run   # preview, changes nothing
python3 postinstall.py
```

| Stage | What happens | Ends with |
|---|---|---|
| 1. Clean base | Hardware detection, system update (dnf), firmware (fwupd: pending updates listed first; laptops must be on the charger), Flatpak updates | Reboot |
| 2. Install | Repos, RPMs, Flatpaks, AI CLIs, agent SDKs, VS Code extensions, Antigravity, GPU drivers for the detected AMD or Intel GPU, services. Then a validation pass: each item is checked, anything missing is retried once, and a pass/fail table is printed. | Reboot |
| 3. Sign-in | git, SSH key, GitHub, gcloud (+ ADC), Claude, Gemini, Codex, Hugging Face, Tailscale, API keys, then each app that needs a sign-in | To-do file on the Desktop |

After each reboot, Konsole opens by itself once you log in and continues. You can also just run `python3 postinstall.py` again.

| Option | Does |
|---|---|
| `--dry-run` | Print what would change |
| `--check` | Validation table only (read-only) |
| `--auth` | Sign-ins only; finished ones are skipped |
| `--stage N` | Run only stage 1, 2 or 3 |

Logs: `~/postinstall-logs/`. Re-running is safe; finished steps are skipped.

## What gets installed

| Area | Items | Source |
|---|---|---|
| Browsers and editors | Google Chrome, VS Code | Google / Microsoft RPM repos |
| Antigravity | Antigravity (agent app) and Antigravity IDE | Google's download page (tarballs), current version read on each run |
| AI apps and CLIs | ChatGPT desktop; Claude Code, Gemini CLI, Codex CLI, Hugging Face `hf` | OpenAI RPM repo; Anthropic installer, npm, uv |
| Agent SDKs | anthropic, claude-agent-sdk, google-genai, google-adk, openai, openai-agents, mcp, litellm | `~/.venvs/agents` (activate with `agents`) |
| Local AI | ollama, ramalama, llama.cpp, ROCm runtime | Fedora |
| Cloud | Google Cloud CLI, GKE auth plugin, kubectl, skaffold | Google RPM repo |
| Remote | Remmina (RDP, VNC, KWallet passwords), Tailscale | Fedora; Tailscale RPM repo |
| Virtual machines | QEMU/KVM, libvirt, virt-manager, swtpm, UEFI, virtio-win drivers | Fedora; virtio-win repo |
| Gaming and streaming | Steam (+ MangoHud, gamescope, vkBasalt), ProtonPlus, Sunshine, Moonlight, OBS + VkCapture | Flathub; Sunshine's COPR |
| Apps | Discord, Spotify, Obsidian, OrcaSlicer, Lychee Slicer | Flathub |
| Dev basics | git, gh, Python 3.13, uv, Node + npm, podman, jq, ripgrep, fzf, btop, tmux | Fedora |
| Codecs and GPU | ffmpeg; AMD: freeworld Mesa + ROCm; Intel: intel-media-driver (H.264/HEVC for Sunshine and OBS) | RPM Fusion; Fedora |

The package lists are at the top of the script. Edit them there.

## Notes

- **RX 6600 (gfx1032):** ROCm doesn't support this GPU, so ollama gets `HSA_OVERRIDE_GFX_VERSION=10.3.0` and ramalama uses its Vulkan image. This only applies when an RX 6600-class card is detected.
- **API keys** go in `~/.config/api-keys.env` (mode 600) and are loaded only by `agents`. Exported globally, they would make the Claude, Gemini and Codex CLIs bill the key instead of your subscription.
- **Hardware detection:** every run reads the maker, model, CPU, GPUs, whether it's a laptop, and charger state. Firmware updates are skipped in VMs, and pending updates are listed before flashing. On a laptop the script waits for the charger before the system update and firmware. GPU drivers match the GPU found.
- **Antigravity:** Google publishes 2.x for Linux only as tarballs on antigravity.google/download (2026-09-29: Antigravity 2.18.1, IDE 2.5.5). The script reads that page for the current versions and also checks Google's older RPM repo. The IDE installs from the RPM repo only if that has caught up. Tarball installs go to `~/.local/opt` and can't update themselves, so when Antigravity says an update is out, run `--stage 2` again.
- **ChatGPT:** OpenAI publishes no key URL, so the script installs the first RPM directly. That RPM adds OpenAI's signed repo, and later updates come through `dnf upgrade`.
- **Steam Deck streaming:** install Moonlight on the Deck from Discover, then pair it with Sunshine at `https://localhost:47990`. Steam Remote Play works too, with no setup.
