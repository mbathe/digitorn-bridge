#!/usr/bin/env sh
# Digitorn installer for macOS and Linux.
#
# Usage:
#   curl -fsSL https://digitorn.ai/install.sh | sh
#
# What it does:
#   1. Installs uv (Python manager) if not present.
#   2. Installs Digitorn into an isolated uv tool environment.
#   3. Registers Digitorn as a launchd agent (macOS) or systemd user
#      unit (Linux) and starts it.
#
# Re-running the script upgrades to the latest release.

set -eu

if [ "${DEBUG:-}" = "1" ]; then
    set -x
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

step() {
    printf '\n==> %s\n' "$1"
}

info() {
    printf '    %s\n' "$1"
}

die() {
    printf '\nError: %s\n' "$1" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

OS=$(uname -s)
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *)      die "Unsupported OS: $OS. Windows users: run install.ps1 from PowerShell." ;;
esac

ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64) ;;
    arm64|aarch64) ;;
    *) die "Unsupported architecture: $ARCH" ;;
esac

printf '\nDigitorn installer\n'
printf -- '------------------\n'
printf 'Platform: %s (%s)\n' "$PLATFORM" "$ARCH"

# ---------------------------------------------------------------------------
# 1. uv (manages Python 3.12 transparently)
# ---------------------------------------------------------------------------

step "Checking for uv"

if ! command -v uv >/dev/null 2>&1; then
    info "Not found. Installing from astral.sh..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # The installer writes uv to $HOME/.local/bin and updates the shell
    # rc files for future sessions. Pick it up for the current session.
    if [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    elif [ -x "$HOME/.cargo/bin/uv" ]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
fi

command -v uv >/dev/null 2>&1 || die "uv install failed. See https://docs.astral.sh/uv/getting-started/installation/"

UV_VERSION=$(uv --version | sed 's/^uv //')
info "uv $UV_VERSION"

# ---------------------------------------------------------------------------
# 2. Digitorn
# ---------------------------------------------------------------------------

step "Installing Digitorn"
info "First run downloads Python 3.12 and ~2 GB of model weights."
info "Subsequent runs use the cache."

uv tool install --python 3.12 --force digitorn

# Make sure the tool dir is on PATH for the current session.
if [ -x "$HOME/.local/bin/digitorn" ]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

command -v digitorn >/dev/null 2>&1 || die "digitorn entry point not found after install. Check uv output above."

DIGITORN_VERSION=$(digitorn version 2>/dev/null | head -n 1)
info "digitorn installed ($DIGITORN_VERSION)"

# ---------------------------------------------------------------------------
# 3. Service registration
# ---------------------------------------------------------------------------

step "Registering the background service"

# Idempotent: stop + uninstall any previous install first.
digitorn service stop >/dev/null 2>&1 || true
digitorn service uninstall >/dev/null 2>&1 || true

digitorn service install
digitorn service start

# Linux: user-mode systemd units don't start at boot unless lingering is
# enabled. Try to enable it without forcing it (no sudo prompt).
if [ "$PLATFORM" = "linux" ] && command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
        info "To start the daemon at boot, run once:"
        info "  sudo loginctl enable-linger $USER"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Done
# ---------------------------------------------------------------------------

printf '\nDone. Daemon listening on http://127.0.0.1:8000\n\n'
printf '  digitorn doctor            check the environment\n'
printf '  digitorn init my-app       scaffold a project\n'
printf '  digitorn service status    is the daemon up?\n'
printf '  digitorn service logs      recent log lines\n\n'
printf 'Documentation: https://docs.digitorn.ai\n\n'

# Path hint if the user's shell didn't pick up the new entries.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        info "Tip: add $HOME/.local/bin to your PATH so 'digitorn' is found in new shells."
        info "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
        ;;
esac
