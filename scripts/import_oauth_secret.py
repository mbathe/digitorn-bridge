"""Import a Google OAuth credentials JSON downloaded from Google Cloud Console
into the local .env file as DIGITORN_OAUTH__GOOGLE__CLIENT_ID/SECRET.

Usage:
    python scripts/import_oauth_secret.py "C:/path/to/client_secret_*.json"

Never prints the secret. Idempotent: appends fresh values, comments any
prior entry. Run from the digitorn-bridge repo root.
"""
import json, os, sys, re, time
from pathlib import Path

if len(sys.argv) != 2:
    print("usage: python scripts/import_oauth_secret.py <path-to-client_secret.json>")
    sys.exit(1)

src = Path(sys.argv[1])
if not src.is_file():
    print(f"ERROR: file not found: {src}")
    sys.exit(2)

with open(src, encoding="utf-8") as f:
    data = json.load(f)

# Google's downloaded JSON wraps under either "web" or "installed".
node = data.get("web") or data.get("installed")
if not node or "client_id" not in node or "client_secret" not in node:
    print("ERROR: invalid Google OAuth JSON (missing web.client_id/secret)")
    sys.exit(3)

env_path = Path(".env")
existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

# Comment any prior google oauth lines so we keep history without duplicating
existing = re.sub(
    r"^(DIGITORN_OAUTH__GOOGLE__CLIENT_(?:ID|SECRET)=.*)$",
    r"# rotated \1",
    existing,
    flags=re.MULTILINE,
)

stamp = time.strftime("%Y-%m-%d %H:%M:%S")
appended = (
    f"\n# Google OAuth credentials imported {stamp}\n"
    f"DIGITORN_OAUTH__GOOGLE__CLIENT_ID={node['client_id']}\n"
    f"DIGITORN_OAUTH__GOOGLE__CLIENT_SECRET={node['client_secret']}\n"
)

env_path.write_text(existing.rstrip() + "\n" + appended, encoding="utf-8")
print(f"OK - wrote {env_path.resolve()} (mode: append, secret never echoed)")
print(f"Verify: grep DIGITORN_OAUTH__GOOGLE__CLIENT_ID .env (you should see the prefix only)")
