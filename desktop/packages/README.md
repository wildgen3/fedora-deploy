# Package lists

`postinstall.py` reads every file here. Each file loads on its own, so a mistake in one line is reported with its file and line number and only that line is skipped.

## Format

```
# comment
[Group name]                   # packages under a header install together
[Group name | amd-gpu]         # ...only on machines that match this block
[Group name | opt:scx]         # ...only with  postinstall.py --with scx
package-name     # what it's for
maybe-package?   # "?" = fine if this Fedora release doesn't have it
```

- **Blocks:** `amd-cpu`, `intel-cpu`, `amd-gpu`, `intel-gpu`, `laptop`, `desktop`, `framework`, `framework-intel`, `framework-amd`, `hibernate`, `amd-gpu-tuning`
- **Extras:** `opt:NAME`, where NAME is one of `postinstall.py --list-options`
- If a group's install fails, its packages are retried one at a time. Anything that still fails is listed with dnf's error at the end, and in `~/postinstall-logs/problems-*.txt`.

| File | One line is | Installed |
|---|---|---|
| `rpm-core.txt` | `package` | First, before any other repo (git, CLI and code tools) |
| `rpm-*.txt` | `package` | Stage 2, in file-name order |
| `swaps.txt` | `fedora-package  replacement` | Stage 2: the Fedora package is replaced (or the replacement installed) |
| `flatpak.txt` | `app.id` | Stage 2, from Flathub, for your user |
| `vscode.txt` | `publisher.extension` | Stage 2 |
| `npm.txt` | `package  command` | Stage 2, into `~/.local` |
| `uv-tools.txt` | `package  command` | Stage 2 |
| `agent-sdks.txt` | `pip-package  import-name` | Stage 2, into `~/.venvs/agents` |
