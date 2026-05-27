"""Phase 0+1 scribe test: prove the LLM tool schema for digitorn-scribe
contains ZERO Preview* tools after the blocked_actions patch.

Bucket 3 from the bug list: scribe was offered PreviewAttach /
PreviewPublish / PreviewDetach, all of which expect package.json and
crash on LaTeX projects. The fix patches the app YAML with
``web_preview.constraints.blocked_actions: [preview_attach,
preview_publish, preview_detach]``.

This script proves the patch landed at the indexed-tools layer (the
schema sent to the LLM), not just at execution time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from digitorn.testing import DevClient


APP_ID = "digitorn-scribe"


def _read_token() -> str | None:
    creds = Path.home() / ".digitorn" / "credentials.json"
    if not creds.exists():
        return None
    try:
        return json.loads(creds.read_text(encoding="utf-8")).get("access_token")
    except Exception:
        return None


def main() -> int:
    token = _read_token()
    if not token:
        print("FAIL: no access_token in ~/.digitorn/credentials.json")
        return 1
    client = DevClient(token=token)
    print(f"daemon={client.daemon_url}")

    # 1. confirm app is deployed
    apps = client.list_apps()
    ids = [a.get("app_id") for a in apps]
    if APP_ID not in ids:
        print(f"FAIL: {APP_ID} not deployed. apps={ids}")
        return 1
    print(f"app deployed: {APP_ID}")

    # 2. search the LLM-visible tools for anything Preview-shaped
    results = client.search_tools(APP_ID, "preview")
    preview_tools = [
        t for t in results
        if any(needle in str(t.get("name", "")).lower()
               for needle in ("preview_attach", "preview_publish",
                              "preview_detach", "previewattach",
                              "previewpublish", "previewdetach"))
    ]
    print(f"\nsearch_tools('preview') returned {len(results)} hits")
    for r in results:
        print(f"  - {r.get('name')!r:40s}  {str(r.get('description',''))[:60]}")

    if preview_tools:
        print("\nFAIL: blocked tools STILL visible to LLM:")
        for t in preview_tools:
            print(f"  - {t.get('name')}")
        return 1

    # 3. extra paranoia: try direct get_tool on each blocked name
    print("\ndirect get_tool() probes:")
    leaked = []
    for short_name in ("PreviewAttach", "PreviewPublish", "PreviewDetach"):
        try:
            t = client.get_tool(APP_ID, short_name)
            if t and t.get("name"):
                leaked.append(short_name)
                print(f"  {short_name:18s}  LEAKED (schema returned)")
            else:
                print(f"  {short_name:18s}  ok (empty)")
        except Exception as exc:
            msg = str(exc)[:80]
            print(f"  {short_name:18s}  ok (rejected: {msg})")

    if leaked:
        print(f"\nFAIL: {len(leaked)} blocked tool(s) leaked via direct get_tool")
        return 1

    print("\nPASS: no Preview* tool visible to the scribe LLM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
