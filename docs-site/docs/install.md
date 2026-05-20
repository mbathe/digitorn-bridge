---
id: install
title: Install
sidebar_label: Install
sidebar_position: 0
---

The installer fetches Python 3.12 via [uv](https://docs.astral.sh/uv/),
installs the `digitorn` CLI, registers a background service, and
starts the daemon on `http://127.0.0.1:8000`. Re-running the same
command upgrades to the latest release.

## Windows

Run from PowerShell (admin elevation will be requested for the
service registration):

```powershell
irm https://digitorn.ai/install.ps1 | iex
```

The script registers a Windows Service named `DigitornDaemon` that
auto-starts on boot and restarts on failure.

## macOS

```bash
curl -fsSL https://digitorn.ai/install.sh | sh
```

A LaunchAgent is registered under `~/Library/LaunchAgents/`. It
starts when you log in and keeps the daemon alive.

## Linux

```bash
curl -fsSL https://digitorn.ai/install.sh | sh
```

A user-mode systemd unit is registered under
`~/.config/systemd/user/`. To make it start at boot (before any
login), enable lingering for your account once:

```bash
sudo loginctl enable-linger $USER
```

## Already have Python 3.12?

If your environment is set up, install via pip or any tool that
reads `pyproject.toml`:

```bash
pip install digitorn
# or
uv tool install digitorn
# or
pipx install digitorn
```

Then register and start the service:

```bash
digitorn service install
digitorn service start
```

## Verifying

```bash
digitorn doctor                       # environment check
digitorn service status               # is the daemon up?
curl http://127.0.0.1:8000/healthz    # the daemon itself
```

`digitorn doctor` reports the Python version, the daemon directory,
required binaries (git, node, npx for MCP), missing optional
dependencies, and the service status.

## Upgrade

Re-running the same install command pulls the latest release and
restarts the service.

```powershell
irm https://digitorn.ai/install.ps1 | iex
```

```bash
curl -fsSL https://digitorn.ai/install.sh | sh
```

## Uninstall

```bash
digitorn service stop
digitorn service uninstall
uv tool uninstall digitorn      # or: pip uninstall digitorn
```

The daemon's data (apps, sessions, credentials, logs, model cache)
lives under `~/.digitorn/`. Delete it manually if you want a clean
slate.

## What gets installed

| Location | Contents |
|----------|----------|
| `~/.local/bin/digitorn` (Linux/macOS) or `%USERPROFILE%\.local\bin\digitorn.exe` (Windows) | CLI entry point |
| uv tool venv (under `~/.local/share/uv/tools/digitorn/`) | Daemon + dependencies in an isolated Python environment |
| `~/.digitorn/` | Per-user data: `config.yaml`, `digitorn.db`, `apps/`, `sessions/`, `logs/`, `kv/` |
| `~/.cache/fastembed/` (or platform equivalent) | Embedding model weights (~220 MB for the default `minilm-l12`) |
| Windows Service `DigitornDaemon` / macOS LaunchAgent `dev.digitorn.daemon` / Linux user unit `digitorn.service` | Background service |

## Configuration

The daemon reads `~/.digitorn/config.yaml` if present. Without it,
the defaults are sensible for a local single-user install
(localhost, SQLite, no auth). For a multi-user or hosted deploy,
see [Production Deployment](/docs/language/production).

## Troubleshooting

### `digitorn` not on PATH after install

The installer adds `~/.local/bin` (or `%USERPROFILE%\.local\bin`) to
your PATH. New shells pick this up; the current shell does not.
Open a new terminal, or add it manually:

```bash
export PATH="$HOME/.local/bin:$PATH"    # Linux/macOS
```

### Service won't start

```bash
digitorn service logs           # last 50 lines
digitorn service status         # current state
digitorn doctor                 # env check
```

Common causes: port 8000 already in use, the daemon's data
directory is not writable, the Python install is broken (rare:
re-run the installer to rebuild the tool venv).

### Windows: service registration fails with access denied

The installer self-elevates via UAC. If you launched PowerShell
without admin rights and skipped the UAC prompt, re-run the install
command from an elevated PowerShell.

### Linux: service stops when I log out

User systemd units stop with the user session unless lingering is
enabled:

```bash
sudo loginctl enable-linger $USER
```
