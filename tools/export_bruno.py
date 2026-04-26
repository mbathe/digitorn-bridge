"""export_bruno.py — convert tools/openapi.json into a full Bruno collection.

Produces ``bruno/digitorn-api-full/`` with one ``.bru`` per operation,
grouped into folders by tag. Every request carries:

  - ``auth: bearer`` with ``token: {{access_token}}``
  - path params rendered as ``{{param}}`` so they auto-fill from env vars
    (``{{app_id}}`` / ``{{session_id}}`` when they match; else the raw name)
  - a sample JSON body generated from the requestBody schema

The curated collection at ``bruno/digitorn-api/`` stays untouched.

Usage::

    py -3.12 tools/export_bruno.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO_ROOT / "tools" / "openapi.json"
OUT_ROOT = REPO_ROOT / "bruno" / "digitorn-api-full"

# Path params that we want to pipe through Bruno env vars instead of
# leaving as raw names. Any param not in this map still becomes
# ``{{param_name}}`` but starts empty — the user fills it per request.
_ENV_MAPPED_PARAMS = {
    "app_id": "{{app_id}}",
    "session_id": "{{session_id}}",
    "provider_name": "{{provider_name}}",
    "user_id": "{{user_id}}",
    "task_id": "{{task_id}}",
    "agent_id": "{{agent_id}}",
    "credential_id": "{{credential_id}}",
    "draft_id": "{{draft_id}}",
    "request_id": "{{request_id}}",
    "server_id": "{{mcp_server_id}}",
    "image_id": "{{image_id}}",
    "revision": "{{revision}}",
    "file_path": "notes.md",
    "filename": "notes.md",
    "path": "notes.md",
}

# Folder execution priority for "Run all" — Bruno runs folders in the
# order set by their ``seq`` value in folder.bru. Lower = earlier.
# The chain ``auth → discovery → credentials → apps → messages`` has to
# complete before anything else so tokens + app_id + session_id are
# captured. Admin/security run last because they can delete/mutate
# state. Any tag not listed here gets default priority 1000.
_FOLDER_PRIORITY: dict[str, int] = {
    "auth":         10,   # login FIRST (captures access_token, refresh_token, user_id)
    "user":         20,   # needs access_token
    "discovery":    30,   # read-only, no deps
    "modules":      40,   # read-only
    "credentials":  50,   # set API keys before deploying apps that need them
    "mcp":          60,
    "packages":     70,
    "apps":         80,   # deploy + sessions + messages (biggest family)
    "builder":      90,
    "oauth":       100,
    "config":      110,
    "ui":          120,
    "transcribe":  130,
    "requires":    140,
    "untagged":    150,
    "security":    900,   # run late — can grant/revoke permissions
    "admin":       910,   # run last — can delete users / apps
}

# Request-body field names that should be substituted with Bruno env
# vars so "run all" works without editing each request. Fields not in
# this map keep their schema-derived default (empty string, 0, etc.).
_ENV_MAPPED_FIELDS: dict[str, Any] = {
    "email": "{{email}}",
    "password": "{{password}}",
    "username": "{{username}}",
    "display_name": "{{display_name}}",
    "refresh_token": "{{refresh_token}}",
    "access_token": "{{access_token}}",
    "app_id": "{{app_id}}",
    "session_id": "{{session_id}}",
    "user_id": "{{user_id}}",
    "task_id": "{{task_id}}",
    "agent_id": "{{agent_id}}",
    "credential_id": "{{credential_id}}",
    "draft_id": "{{draft_id}}",
    "request_id": "{{request_id}}",
    "provider": "{{provider_name}}",
    "provider_name": "{{provider_name}}",
    "message": "{{message}}",
    "prompt": "{{message}}",
    "query": "{{query}}",
    "yaml_path": "{{yaml_path}}",
    "name": "{{draft_name}}",
    "draft_name": "{{draft_name}}",
    "approved": True,
    "commit_message": "Bruno test commit",
}

# Operations that should capture response data into env vars. The key
# is matched with endswith() against operationId, so both
# ``login`` and ``post_login`` work.
_OPERATION_CAPTURES: dict[str, list[tuple[str, str]]] = {
    "login": [
        ("access_token", "access_token"),
        ("refresh_token", "refresh_token"),
        ("user_id", "user_id"),
    ],
    "register": [
        ("access_token", "access_token"),
        ("refresh_token", "refresh_token"),
        ("user_id", "user_id"),
    ],
    "refresh": [
        ("access_token", "access_token"),
        ("refresh_token", "refresh_token"),
    ],
    "deploy_app": [
        ("app_id", "data.app_id"),
    ],
    "create_session": [
        ("session_id", "data.session_id"),
    ],
    "session_send_message": [
        ("last_correlation_id", "data.correlation_id"),
    ],
}

# Safe filename: keep letters, digits, - _ space, collapse runs.
_FS_RESERVED = re.compile(r"[^A-Za-z0-9 _\-.]+")


def _slug_filename(name: str) -> str:
    name = _FS_RESERVED.sub("-", name).strip("- ")
    return (name or "untitled")[:120]


def _tag_to_folder(tag: str) -> str:
    return _slug_filename(tag or "untagged")


# ── Sample JSON generation from schemas ─────────────────────────────


def _resolve_ref(ref: str, root: dict) -> dict:
    # "#/components/schemas/Foo" → root["components"]["schemas"]["Foo"]
    parts = ref.lstrip("#/").split("/")
    cur: Any = root
    for p in parts:
        cur = cur.get(p, {})
    return cur if isinstance(cur, dict) else {}


def _sample_for_schema(schema: dict, root: dict, depth: int = 0) -> Any:
    """Produce a plausible JSON value matching the schema.

    Uses declared examples/defaults when present. Recurses into objects
    and arrays. Caps depth to avoid infinite recursion on self-
    referential schemas.
    """
    if depth > 5:
        return None
    if not schema:
        return None

    if "$ref" in schema:
        return _sample_for_schema(_resolve_ref(schema["$ref"], root), root, depth + 1)

    # Unions (anyOf / oneOf): pick the first non-null branch.
    for key in ("anyOf", "oneOf"):
        if key in schema:
            for branch in schema[key]:
                if branch.get("type") != "null":
                    return _sample_for_schema(branch, root, depth + 1)
            return None

    if "allOf" in schema:
        merged: dict = {}
        for branch in schema["allOf"]:
            merged.update(branch)
        return _sample_for_schema(merged, root, depth + 1)

    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    tp = schema.get("type")
    if tp == "string":
        fmt = schema.get("format", "")
        if fmt == "date-time":
            return "1970-01-01T00:00:00Z"
        if fmt == "date":
            return "1970-01-01"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        return ""
    if tp == "integer":
        return 0
    if tp == "number":
        return 0.0
    if tp == "boolean":
        return False
    if tp == "array":
        item = _sample_for_schema(schema.get("items", {}), root, depth + 1)
        return [item] if item is not None else []
    if tp == "object" or "properties" in schema:
        out: dict = {}
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        for name, sub in props.items():
            # Well-known field names get substituted with a Bruno env var
            # template so "run all" works end-to-end. This takes priority
            # over schema-derived sample values.
            if name in _ENV_MAPPED_FIELDS:
                out[name] = _ENV_MAPPED_FIELDS[name]
                continue
            # Otherwise include required fields + any declared example/default
            if name in required or "example" in sub or "default" in sub:
                out[name] = _sample_for_schema(sub, root, depth + 1)
        # Always include at least the first prop so empty schemas aren't
        # blank objects that confuse the user.
        if not out and props:
            first = next(iter(props))
            if first in _ENV_MAPPED_FIELDS:
                out[first] = _ENV_MAPPED_FIELDS[first]
            else:
                out[first] = _sample_for_schema(props[first], root, depth + 1)
        return out

    return None


# ── .bru rendering ──────────────────────────────────────────────────


_BRU_METHOD_KEYWORDS = {"get", "post", "put", "patch", "delete"}


def _render_url(path: str) -> str:
    # "/api/apps/{app_id}/sessions/{session_id}/..."
    # → "{{base_url}}/api/apps/{{app_id}}/sessions/{{session_id}}/..."
    #
    # Note: ``/auth/sessions/{session_id}`` and
    # ``/api/apps/{app_id}/sessions/{session_id}`` point at the SAME
    # UserSession row (primary key is (app_id, session_id)). Both use
    # the same ``{{session_id}}`` — no distinction needed.
    def repl(m: re.Match) -> str:
        name = m.group(1)
        return _ENV_MAPPED_PARAMS.get(name, "{{" + name + "}}")

    rendered = re.sub(r"\{([^}]+)\}", repl, path)
    return "{{base_url}}" + rendered


def _render_query_params(params: list[dict]) -> str | None:
    query = [p for p in params if p.get("in") == "query"]
    if not query:
        return None
    lines = []
    for p in query:
        name = p.get("name", "?")
        default = ""
        schema = p.get("schema") or {}
        if "default" in schema:
            default = str(schema["default"])
        lines.append(f"  {name}: {default}")
    return "params:query {\n" + "\n".join(lines) + "\n}\n"


_MUSTACHE_PLACEHOLDER_RE = re.compile(r'"(\{\{[^}]+\}\})"')


def _unquote_mustache(text: str) -> str:
    """Strip the surrounding quotes around ``"{{var}}"`` so the body
    reads as a plain Bruno template reference. Bruno resolves
    ``{{access_token}}`` to the env var's value at send time — without
    unquoting, the body would contain a literal ``"{{access_token}}"``
    string which isn't what Bruno's `body:json` block expects.
    """
    return _MUSTACHE_PLACEHOLDER_RE.sub(lambda m: m.group(1), text)


def _render_body(op: dict, root: dict) -> tuple[str, str]:
    """Return (body_spec, body_block) for the .bru file.

    body_spec is one of: 'none', 'json'. body_block is the full
    ``body:json { ... }`` section or empty string.
    """
    rb = op.get("requestBody") or {}
    content = rb.get("content") or {}
    jc = content.get("application/json")
    if not jc:
        return "none", ""
    schema = jc.get("schema") or {}
    sample = _sample_for_schema(schema, root)
    if sample is None:
        sample = {}
    rendered = json.dumps(sample, indent=2)
    # Keep the placeholders quoted — Bruno's body:json is valid JSON
    # with string-typed {{vars}} that resolve at send time. The quoted
    # form is what actually works (unquoting produces invalid JSON).
    # Indent 2 spaces inside the body:json block.
    indented = "\n".join("  " + line for line in rendered.splitlines())
    return "json", f"body:json {{\n{indented}\n}}\n"


def _capture_script_for(op_id: str) -> str:
    """Return a ``script:post-response`` block that saves response fields
    into Bruno env vars, or empty string if this op is not a capture op.

    Match with ``endswith`` so both the real operationId (``login``)
    and our fallback form (``post_login``) resolve to the same capture.
    """
    captures = None
    for key, caps in _OPERATION_CAPTURES.items():
        if op_id == key or op_id.endswith("_" + key):
            captures = caps
            break
    if not captures:
        return ""
    lines: list[str] = ["script:post-response {"]
    lines.append("  if (res.status >= 200 && res.status < 300 && res.body) {")
    for env_var, dot_path in captures:
        parts = dot_path.split(".")
        # Build a safe JS chain: res.body?.data?.session_id. Numeric
        # components become bracketed array indices: "0" → [0].
        chain = "res.body"
        for p in parts:
            if p.isdigit():
                chain += f"?.[{p}]"
            else:
                chain += f"?.{p}"
        lines.append(f'    if ({chain} != null) bru.setEnvVar("{env_var}", String({chain}));')
    lines.append("  }")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _render_bru(
    path: str,
    method: str,
    op: dict,
    seq: int,
    root: dict,
) -> str:
    method = method.lower()
    assert method in _BRU_METHOD_KEYWORDS

    # Prefer summary > operationId > "METHOD path"
    name = (op.get("summary") or op.get("operationId") or f"{method.upper()} {path}").strip()
    name = name.replace("\n", " ").strip()
    if not name:
        name = f"{method.upper()} {path}"

    body_spec, body_block = _render_body(op, root)

    lines: list[str] = []
    lines.append("meta {")
    lines.append(f"  name: {name}")
    lines.append("  type: http")
    lines.append(f"  seq: {seq}")
    lines.append("}")
    lines.append("")

    lines.append(f"{method} {{")
    lines.append(f"  url: {_render_url(path)}")
    lines.append(f"  body: {body_spec}")
    lines.append("  auth: bearer")
    lines.append("}")
    lines.append("")

    lines.append("auth:bearer {")
    lines.append("  token: {{access_token}}")
    lines.append("}")
    lines.append("")

    # Query params block, if any
    qp_block = _render_query_params(op.get("parameters") or [])
    if qp_block:
        lines.append(qp_block)

    if body_block:
        lines.append(body_block)

    # script:post-response — capture tokens / ids into env vars so the
    # "run all" flow chains login → deploy → session without manual edits.
    capture_block = _capture_script_for(op.get("operationId") or "")
    if capture_block:
        lines.append(capture_block)

    desc = (op.get("description") or "").strip()
    if desc:
        # Bruno's docs block accepts Markdown.
        clean = desc.replace("\\", "\\\\")
        lines.append("docs {")
        for d_line in clean.splitlines():
            lines.append("  " + d_line)
        lines.append("}")

    return "\n".join(lines) + "\n"


# ── Driver ──────────────────────────────────────────────────────────


def main() -> None:
    if not OPENAPI_PATH.exists():
        raise SystemExit(
            f"[export_bruno] {OPENAPI_PATH} not found — run `py -3.12 tools/export_openapi.py` first."
        )
    schema = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    paths = schema.get("paths", {})
    if not paths:
        raise SystemExit("[export_bruno] OpenAPI contains no paths.")

    if OUT_ROOT.exists():
        # Wipe the previous full collection — it's entirely regenerated.
        import shutil
        shutil.rmtree(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True)

    # README explaining run order + known stateful dependencies that
    # the first "Run all" can't satisfy without at least one message
    # being sent to materialise UserSession rows in DB.
    (OUT_ROOT / "README.md").write_text(
        "# Digitorn API — full collection\n\n"
        "260 requests auto-generated from `app.openapi()`. Regenerate with:\n"
        "```\npy -3.12 tools/export_openapi.py && py -3.12 tools/export_bruno.py\n```\n\n"
        "## Run order (lower seq = earlier)\n\n"
        "Folders are prioritised so tokens + app_id + session_id are captured before anything that depends on them:\n\n"
        "| seq | folder | notes |\n"
        "|-----|--------|-------|\n"
        "| 10  | auth   | login first → captures access_token, refresh_token, user_id |\n"
        "| 20  | user   |       |\n"
        "| 30  | discovery | read-only |\n"
        "| 40  | modules   | read-only |\n"
        "| 50  | credentials | configure API keys |\n"
        "| 60  | mcp   |       |\n"
        "| 70  | packages |    |\n"
        "| 80  | apps  | deploy → create_session → send_message (captures app_id, session_id) |\n"
        "| 90  | builder |     |\n"
        "| 100+ | oauth, config, ui, transcribe, requires, untagged | |\n"
        "| 900 | security | grants/revokes — near the end |\n"
        "| 910 | admin  | destructive — run last |\n\n"
        "## State-driven 404s on first Run all\n\n"
        "`auth/session_history`, `auth/fork_session`, `auth/delete_session` all read from the "
        "`UserSession` table which is **populated lazily** (persistence.py:57): "
        "the row only gets written when a conversation session persists its first message. "
        "On a clean daemon:\n\n"
        "  1. Auth runs at seq 10 — UserSession table empty → **404**\n"
        "  2. Apps runs at seq 80 — session_send_message populates a row\n"
        "  3. Second Run all — the 404s disappear\n\n"
        "Same logic applies to routes that need a `{{request_id}}` (approval resolve), "
        "`{{credential_id}}`, `{{draft_id}}`, `{{task_id}}`, `{{mcp_server_id}}` — those IDs "
        "don't exist in a fresh database. Create the resource manually with its POST route first, "
        "then the GET/DELETE/PUT variants succeed.\n\n"
        "## Variables filled automatically\n\n"
        "| Variable | Captured by |\n"
        "|----------|-------------|\n"
        "| `access_token`, `refresh_token`, `user_id` | `auth/login` and `auth/register` |\n"
        "| `access_token`, `refresh_token` | `auth/refresh` |\n"
        "| `app_id` | `apps/deploy_app` |\n"
        "| `session_id` | `apps/create_session` |\n"
        "| `last_correlation_id` | `apps/session_send_message` |\n\n"
        "## Variables you fill in `environments/Local.bru`\n\n"
        "- `email`, `username`, `password` — your real credentials\n"
        "- `yaml_path` — absolute path to the YAML you want to deploy\n"
        "- `message` — text sent to the agent\n\n",
        encoding="utf-8",
    )

    # Root manifest so Bruno recognizes the folder as a collection.
    (OUT_ROOT / "bruno.json").write_text(
        json.dumps(
            {"version": "1", "name": "Digitorn API — full", "type": "collection", "ignore": ["node_modules", ".git"]},
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    # Shared environment — mirrors the curated collection's so both can
    # coexist without duplicating state.
    env_dir = OUT_ROOT / "environments"
    env_dir.mkdir()
    (env_dir / "Local.bru").write_text(
        "vars {\n"
        "  base_url: http://127.0.0.1:8000\n"
        "  email: admin@digitorn.local\n"
        "  password: changeme-12345\n"
        "  username: admin\n"
        "  display_name: Admin\n"
        "  access_token:\n"
        "  refresh_token:\n"
        "  user_id:\n"
        "  app_id: tutorial-hello-world\n"
        "  session_id:\n"
        "  provider_name: deepseek\n"
        "  agent_id: main\n"
        "  task_id:\n"
        "  credential_id:\n"
        "  draft_id:\n"
        "  request_id:\n"
        "  mcp_server_id:\n"
        "  image_id:\n"
        "  revision:\n"
        "  last_correlation_id:\n"
        "  message: Hello from Bruno\n"
        "  query: hello\n"
        "  draft_name: Bruno draft\n"
        "  yaml_path: C:/Users/ASUS/Documents/digitorn-bridge/knowledge_base/tutorials/01-hello-world.yaml\n"
        "}\n"
        "vars:secret [\n"
        "  password\n"
        "]\n",
        encoding="utf-8",
    )

    # Write one folder.bru per tag so Bruno remembers the folder name.
    written_tags: set[str] = set()

    # First, count total operations per tag to make seq numbering stable.
    operations: list[tuple[str, str, str, dict]] = []  # (tag, path, method, op)
    for path, methods in paths.items():
        for method, op in methods.items():
            if method.lower() not in _BRU_METHOD_KEYWORDS:
                continue
            tags = op.get("tags") or ["untagged"]
            tag = tags[0]
            operations.append((tag, path, method, op))

    def _folder_priority(tag: str) -> int:
        return _FOLDER_PRIORITY.get(tag.lower(), 1000)

    def _operation_priority(path: str, method: str) -> int:
        """Within a folder, route operations that CREATE resources (tokens,
        apps, sessions) to run before everything else so their outputs
        are captured in env vars for the rest of the folder.
        """
        p = path.lower()
        m = method.lower()
        # Canonical creation chain — these capture env vars the rest
        # of the collection depends on.
        if p == "/auth/login" and m == "post":
            return 1
        if p == "/auth/refresh" and m == "post":
            return 2
        if p == "/auth/me" and m == "get":
            return 3
        if p == "/auth/sessions" and m == "get":  # captures auth_session_id
            return 4
        if p == "/api/apps/deploy" and m == "post":
            return 1
        if p == "/api/apps" and m == "get":
            return 2
        if p.endswith("/sessions") and m == "post":  # create_session
            return 3
        if p.endswith("/messages") and m == "post":
            return 4
        # Read-only operations before mutating ones on the same folder.
        if m == "get":
            return 100
        if m == "post":
            return 200
        if m == "put" or m == "patch":
            return 300
        if m == "delete":
            return 400
        return 500

    # Sort by (folder priority, operation priority, path, method) so:
    #   - folders run in dependency order
    #   - within a folder, setup calls run first
    operations.sort(
        key=lambda t: (
            _folder_priority(t[0]),
            _operation_priority(t[1], t[2]),
            t[1],
            t[2],
        )
    )

    seq_per_tag: dict[str, int] = {}
    written_files = 0

    for tag, path, method, op in operations:
        folder_name = _tag_to_folder(tag)
        folder = OUT_ROOT / folder_name
        folder.mkdir(exist_ok=True)

        if folder_name not in written_tags:
            (folder / "folder.bru").write_text(
                f"meta {{\n  name: {folder_name}\n  seq: {_folder_priority(tag)}\n}}\n",
                encoding="utf-8",
            )
            written_tags.add(folder_name)

        seq_per_tag.setdefault(folder_name, 0)
        seq_per_tag[folder_name] += 1
        seq = seq_per_tag[folder_name]

        # Filename: "METHOD verb-style-path.bru" — collapse long paths.
        path_slug = _slug_filename(
            re.sub(r"\{([^}]+)\}", r":\1", path).replace("/", " ").strip()
        ) or "root"
        filename = f"{method.upper()} {path_slug}.bru"
        out = folder / filename
        # Guard against Windows path length
        if len(str(out)) > 240:
            filename = f"{method.upper()} {seq:03d}.bru"
            out = folder / filename

        out.write_text(_render_bru(path, method, op, seq, schema), encoding="utf-8")
        written_files += 1

    print(
        f"[export_bruno] wrote {written_files} request(s) across {len(written_tags)} folder(s)",
        file=sys.stderr,
    )
    print(f"[export_bruno] collection at: {OUT_ROOT.relative_to(REPO_ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
