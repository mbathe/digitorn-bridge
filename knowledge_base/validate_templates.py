"""validate_templates.py — compile every template in knowledge_base/examples/

Runs the AppYAMLCompiler against each YAML file in
``knowledge_base/examples/`` and reports any errors. Templates MUST
compile cleanly — that's the whole point of having a curated library.

Usage::

    py -3.12 knowledge_base/validate_templates.py            # validate all
    py -3.12 knowledge_base/validate_templates.py 01 02      # only specific files

To make secret/env-var references compile without requiring real
credentials, this script seeds a handful of fake values into
``os.environ`` before compiling. The values are obviously fake and
should never reach a real provider — they exist only to satisfy the
compiler's variable resolver, which falls back to env vars when a
``{{secret.X}}`` lookup misses the secret store.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "knowledge_base" / "examples"
PACKAGES_DIR = REPO_ROOT / "packages"

# Force UTF-8 + quiet logger
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
logging.basicConfig(level=logging.ERROR)
logging.getLogger("digitorn").setLevel(logging.ERROR)


# ────────────────────────────────────────────────────────────────────
# Fake credentials — enough to satisfy the compiler's resolver
# ────────────────────────────────────────────────────────────────────

FAKE_ENV: dict[str, str] = {
    # LLM provider keys
    "ANTHROPIC_API_KEY": "sk-ant-fake-template-validation-key",
    "DEEPSEEK_API_KEY": "sk-fake-deepseek-template-validation",
    "OPENAI_API_KEY": "sk-fake-openai-template-validation",
    # Channels — Telegram / Slack / Discord
    "TELEGRAM_BOT_TOKEN": "fake-telegram-bot-token-for-template-validation",
    "SLACK_BOT_TOKEN": "xoxb-fake-template-validation",
    "SLACK_APP_TOKEN": "xapp-fake-template-validation",
    "DISCORD_BOT_TOKEN": "fake-discord-token-for-template-validation",
    # Channels — Email
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USER": "fake@example.com",
    "SMTP_PASSWORD": "fake-smtp-password",
    "IMAP_HOST": "imap.example.com",
    # Webhooks
    "WEBHOOK_SECRET": "fake-webhook-secret-for-template-validation",
    "WEBHOOK_HMAC_SECRET": "fake-hmac-secret-for-template-validation",
    "GITHUB_WEBHOOK_SECRET": "fake-github-webhook-secret",
    # Database
    "DATABASE_URL": "postgresql://fake:fake@localhost/fake",
}


def seed_fake_env() -> None:
    """Inject the fake credentials, but never overwrite a real value."""
    for key, value in FAKE_ENV.items():
        os.environ.setdefault(key, value)


# ────────────────────────────────────────────────────────────────────
# Compiler bootstrap
# ────────────────────────────────────────────────────────────────────


def build_compiler():
    sys.path.insert(0, str(PACKAGES_DIR))
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.core.loader import load_modules
    from digitorn.core.app.compiler import AppYAMLCompiler

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    return AppYAMLCompiler(registry)


# ────────────────────────────────────────────────────────────────────
# Validation
# ────────────────────────────────────────────────────────────────────


def validate_one(compiler, path: Path) -> tuple[bool, list[str]]:
    """Compile one YAML file. Returns ``(ok, errors)``."""
    from digitorn.core.app.errors import AppCompilationError

    try:
        compiled = compiler.compile_file(str(path))
    except AppCompilationError as exc:
        # AppCompilationError stringifies as "App compilation failed (N error(s)): ..."
        msg = str(exc)
        # Extract individual errors after the colon
        if ":" in msg:
            tail = msg.split(":", 1)[1]
            errors = [e.strip() for e in tail.split(";") if e.strip()]
        else:
            errors = [msg]
        return False, errors
    except Exception as exc:
        return False, [f"{type(exc).__name__}: {exc}"]

    # compile_file returns a CompiledApp on success — sanity check it
    if compiled is None:
        return False, ["compile_file returned None"]
    return True, []


def summarize_compiled(compiler, path: Path) -> dict:
    """Re-compile and return a small summary dict for printing."""
    compiled = compiler.compile_file(str(path))
    summary: dict = {
        "app_id": compiled.meta.app_id,
        "name": compiled.meta.name,
        "mode": compiled.execution.mode,
        "agents": len(compiled.agents),
        "modules": list(compiled.modules.keys()),
        "triggers": [(t.id, t.type) for t in compiled.execution.triggers],
        "channels": list(compiled.channels.keys()) if compiled.channels else [],
        "session_mode": compiled.execution.session_mode,
        "payload_schema_required": (
            compiled.execution.payload_schema.get("required")
            if compiled.execution.payload_schema
            else None
        ),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "patterns",
        nargs="*",
        help="Optional substring filters — only files containing these are validated",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a detailed summary of each compiled template",
    )
    args = parser.parse_args()

    seed_fake_env()
    print("[validate] bootstrapping compiler...", file=sys.stderr)
    compiler = build_compiler()

    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    targets = sorted(EXAMPLES_DIR.glob("*.yaml"))
    if args.patterns:
        targets = [
            p for p in targets if any(pat in p.name for pat in args.patterns)
        ]

    if not targets:
        print("[validate] no template files matched", file=sys.stderr)
        sys.exit(1)

    print(f"[validate] {len(targets)} template(s) to check\n", file=sys.stderr)

    failed = 0
    for path in targets:
        rel = path.relative_to(REPO_ROOT)
        ok, errors = validate_one(compiler, path)
        if ok:
            print(f"  ✓ {rel}")
            if args.verbose:
                summary = summarize_compiled(compiler, path)
                for k, v in summary.items():
                    print(f"      {k}: {v}")
                print()
        else:
            failed += 1
            print(f"  ✗ {rel}")
            for err in errors:
                print(f"      → {err}")
            print()

    print()
    if failed:
        print(f"[validate] {failed} template(s) FAILED", file=sys.stderr)
        sys.exit(1)
    print(f"[validate] all {len(targets)} template(s) compile cleanly", file=sys.stderr)


if __name__ == "__main__":
    main()
