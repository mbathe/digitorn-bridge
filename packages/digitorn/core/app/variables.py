"""Template variable resolution for app YAML.

Resolves ``{{variable}}`` patterns in strings, dicts, and lists.
No external template engine - just regex + recursive substitution.

Supported namespaces:

**Compile-time** (resolved by the compiler before bootstrap):

    - ``{{env.VAR_NAME}}``        - reads ``os.environ[VAR_NAME]``
    - ``{{secret.VAR_NAME}}``     - reads from DB secret store, falls back to env
    - ``{{name}}``                - reads from the ``variables:`` section of the YAML
    - ``{{sys.VAR}}``             - system variables (see below)
    - ``{{app.FIELD}}``           - app metadata (id, name, version, author)

**Runtime** (preserved at compile time, resolved by modules at execution):

    - ``{{event.payload.field}}`` - inbound event data (channels module)
    - ``{{caller.name}}``         - prepare step results (channels module)
    - Any ``{{dotpath.expr}}``    - passed through to modules for runtime resolution

**System variables** (``{{sys.*}}``, resolved at compile time):

    - ``{{sys.timestamp}}``       - ISO 8601 UTC compilation timestamp
    - ``{{sys.date}}``            - YYYY-MM-DD date
    - ``{{sys.time}}``            - HH:MM:SS time
    - ``{{sys.hostname}}``        - machine hostname
    - ``{{sys.platform}}``        - OS platform (linux, darwin, win32)
    - ``{{sys.python_version}}``  - Python version string
    - ``{{sys.cwd}}``             - current working directory
    - ``{{sys.user}}``            - current OS username
    - ``{{sys.pid}}``             - current process ID
    - ``{{sys.digitorn_version}}``- Digitorn version

**App variables** (``{{app.*}}``, resolved at compile time from the YAML):

    - ``{{app.id}}``              - app_id from the app: block
    - ``{{app.name}}``            - app name
    - ``{{app.version}}``         - app version
    - ``{{app.author}}``          - app author
    - ``{{app.description}}``     - app description

Variables can reference other variables (max depth = 10, cycle-detected).
"""

from __future__ import annotations

import logging
import os
import platform as _platform
import re
import shutil
import socket
import tempfile
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{\{\s*(.+?)\s*\}\}")

_MAX_DEPTH = 10

# Bundle dir is threaded through ``resolve_variables`` via a context
# variable so every recursive call (and the file-based namespaces
# like ``prompt.X``, ``skill.X``, ``asset.X``) has access without
# changing every call signature.
_BUNDLE_CTX: ContextVar[dict[str, Any] | None] = ContextVar(
    "_variables_bundle_ctx", default=None,
)


# File extensions the text-inlining namespaces look for, in order.
_TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".prompt", "")

# Common image/asset extensions for the ``asset.X`` fuzzy lookup
# when the caller writes ``{{asset.logo}}`` without an extension.
_ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico",
    ".pdf", ".json", ".yaml", ".yml", ".csv", ".txt", "",
)


# Matches markdown image references: ![alt](path) and [title](path)
# as well as HTML-ish <img src="path">. The path group is captured
# so we can rewrite it.
_MD_IMAGE_PATTERN = re.compile(
    r"""(
        !\[[^\]]*\]\(       # ![alt](
        ([^)\s]+)           #   path (no spaces, no closing paren)
        (?:\s+\"[^\"]*\")?  # optional "title"
        \)
    |
        <img\s+[^>]*src=\"([^\"]+)\"[^>]*>  # <img src="...">
    )""",
    re.VERBOSE,
)


def resolve_variables(
    data: Any,
    variables: dict[str, str],
    *,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    bundle_dir: Path | str | None = None,
    app_id: str | None = None,
    _depth: int = 0,
) -> Any:
    """Recursively resolve ``{{...}}`` templates in *data*.

    Supports 3 filesystem-backed namespaces when ``bundle_dir`` is
    passed by the caller (typically the compiler):

    - ``{{prompt.X}}``    → inlines the content of ``prompts/X.md``
      (tries ``.md``, ``.markdown``, ``.txt``, ``.prompt``, bare name)
    - ``{{skill.X}}``     → inlines the content of ``skills/X.md``
    - ``{{behavior.X}}``  → parses ``behavior/X.yaml`` and returns
      the profile dict as a JSON string (for custom behavior profiles)
    - ``{{asset.X}}``     → returns the URL
      ``/api/apps/{app_id}/assets/assets/X`` that the Flutter client
      uses to fetch the file. When ``X`` has no extension, the
      compiler fuzzy-matches against common image/asset extensions.

    Args:
        data:       Any JSON-compatible value (str, dict, list, int, ...).
        variables:  The ``variables:`` section from the app YAML.
        env:        Environment variables (defaults to ``os.environ``).
        secrets:    Pre-loaded app secrets from DB.
        bundle_dir: Directory containing the app bundle (``app.yaml``
                    + ``prompts/`` + ``skills/`` + ``assets/``).
                    Required for the filesystem namespaces.
        app_id:     The app's id, used to build asset URLs.

    Returns:
        A new object with all ``{{...}}`` patterns replaced.
    """
    # Set the bundle context once at the top level so recursive
    # calls don't have to thread it. Only overrides when caller
    # passes an explicit bundle_dir - callers that rely on a
    # pre-set context (via ``bundle_context(...)``) keep their
    # values intact.
    token = None
    if _depth == 0 and (bundle_dir is not None or app_id is not None):
        existing = _BUNDLE_CTX.get() or {}
        token = _BUNDLE_CTX.set({
            "bundle_dir": (
                Path(bundle_dir).resolve() if bundle_dir
                else existing.get("bundle_dir")
            ),
            "app_id": app_id or existing.get("app_id") or "",
        })
    try:
        return _resolve_impl(
            data, variables, env=env, secrets=secrets, _depth=_depth,
        )
    finally:
        if token is not None:
            _BUNDLE_CTX.reset(token)


def collected_prompt_metadata() -> dict[str, dict[str, Any]]:
    """Return the frontmatter metadata collected during the current
    compile pass. Keyed by ``"<namespace>.<name>"`` (e.g.
    ``"prompt.system_main"``, ``"skill.commit"``).

    Used by the compiler's post-variable-resolution validation
    step. Returns an empty dict when there's no active bundle ctx
    or no frontmatter was encountered.
    """
    ctx = _BUNDLE_CTX.get()
    if ctx is None:
        return {}
    return dict(ctx.get("prompt_metadata") or {})


class bundle_context:
    """Context manager that pre-seeds the bundle ctx for a whole
    compile pass.

    Usage::

        with bundle_context(bundle_dir=yaml_path.parent, app_id="myapp"):
            for module_id, block in modules.items():
                resolved = resolve_variables(block.config, variables)

    Lets the compiler set the filesystem context once instead of
    passing ``bundle_dir=`` + ``app_id=`` at every call site.

    ``locale`` picks the locale-suffixed prompt files
    (``system.fr.md`` before ``system.md``). Defaults to ``"en"``.
    """

    def __init__(
        self,
        *,
        bundle_dir: Path | str | None = None,
        app_id: str | None = None,
        locale: str = "en",
    ) -> None:
        self._bundle_dir = Path(bundle_dir).resolve() if bundle_dir else None
        self._app_id = app_id or ""
        self._locale = locale or "en"
        self._token = None

    def __enter__(self) -> "bundle_context":
        self._token = _BUNDLE_CTX.set({
            "bundle_dir": self._bundle_dir,
            "app_id": self._app_id,
            "locale": self._locale,
        })
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _BUNDLE_CTX.reset(self._token)
            self._token = None


def list_available_locales(bundle_dir: Path, subdir: str = "prompts") -> list[str]:
    """Scan a bundle's prompts directory and return the set of
    locales declared via filename suffixes (e.g. ``system.fr.md``
    → ``fr``). Useful to decide whether to re-compile per locale.
    """
    base = (Path(bundle_dir) / subdir).resolve()
    if not base.is_dir():
        return []
    locales: set[str] = set()
    for entry in base.iterdir():
        if not entry.is_file():
            continue
        # foo.fr.md  →  parts = ("foo", "fr", "md")
        parts = entry.name.split(".")
        if len(parts) >= 3:
            candidate = parts[-2]
            if len(candidate) in (2, 5) and candidate.replace("-", "").isalpha():
                # looks like a locale code (en, fr, pt-BR, zh-CN)
                locales.add(candidate)
    return sorted(locales)


def _resolve_impl(
    data: Any,
    variables: dict[str, str],
    *,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    _depth: int = 0,
) -> Any:
    if _depth > _MAX_DEPTH:
        raise ValueError(
            f"Variable resolution exceeded max depth ({_MAX_DEPTH}). "
            "Possible circular reference."
        )

    if env is None:
        env = dict(os.environ)

    if isinstance(data, str):
        return _resolve_string(data, variables, env, secrets, _depth)
    if isinstance(data, dict):
        return {
            _resolve_string(str(k), variables, env, secrets, _depth)
            if isinstance(k, str)
            else k: _resolve_impl(
                v, variables, env=env, secrets=secrets, _depth=_depth
            )
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [
            _resolve_impl(item, variables, env=env, secrets=secrets, _depth=_depth)
            for item in data
        ]
    return data


def _resolve_string(
    value: str,
    variables: dict[str, str],
    env: dict[str, str],
    secrets: dict[str, str] | None,
    depth: int,
) -> str:
    """Resolve all ``{{...}}`` in a single string value."""
    if depth > _MAX_DEPTH:
        raise ValueError(
            f"Variable resolution exceeded max depth ({_MAX_DEPTH}). "
            "Possible circular reference."
        )

    def _replacer(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if "|" in expr:
            return match.group(0)
        resolved = _lookup(expr, variables, env, secrets)
        # If _lookup returned the original template unchanged (runtime
        # passthrough for dotpath expressions like event.*, caller.*),
        # don't recurse - it would loop forever.
        if resolved == match.group(0):
            return resolved
        if "{{" in resolved:
            resolved = _resolve_string(resolved, variables, env, secrets, depth + 1)
        return resolved

    return _VAR_PATTERN.sub(_replacer, value)


def _lookup(
    expr: str,
    variables: dict[str, str],
    env: dict[str, str],
    secrets: dict[str, str] | None,
) -> str:
    """Resolve a single expression (without braces).

    Supports:
        ``env.VAR``       → os.environ
        ``secret.VAR``    → DB secrets first, then os.environ fallback
        ``name``          → variables dict
        ``name ?? fallback`` → try name, else fallback expression
    """
    if "??" in expr:
        parts = expr.split("??", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        try:
            result = _lookup(left, variables, env, secrets)
        except ValueError:
            return _lookup(right, variables, env, secrets)
        # ``env.X`` / ``secret.X`` return a lenient passthrough on miss.
        # For ``??`` we want strict semantics - fall through if the left
        # side didn't actually resolve.
        if result == "{{" + left + "}}":
            return _lookup(right, variables, env, secrets)
        return result

    if (expr.startswith("'") and expr.endswith("'")) or (
        expr.startswith('"') and expr.endswith('"')
    ):
        return expr[1:-1]

    # ── Filesystem-backed namespaces: prompt / skill / asset ──
    # These require a bundle_dir set in the resolver context.
    # When the context is empty (e.g. tests, pre-existing callers
    # that don't pass bundle_dir), the template is passed through
    # unresolved so the caller can see it's a template.
    if expr.startswith("prompt."):
        return _resolve_prompt(expr[7:])
    if expr.startswith("skill."):
        return _resolve_skill(expr[6:])
    if expr.startswith("asset."):
        return _resolve_asset(expr[6:])
    if expr.startswith("behavior."):
        return _resolve_behavior(expr[9:])
    if expr.startswith("asset_b64."):
        return _resolve_asset_b64(expr[10:])
    if expr.startswith("include:"):
        return _resolve_include(expr[8:])

    if expr.startswith("env."):
        key = expr[4:]
        value = env.get(key)
        if value is None and secrets:
            value = secrets.get(key)
        if value is not None:
            return value
        # Lenient passthrough - symmetric with secret.X. Credentials are
        # resolved by the CredentialStore at runtime (per_user scopes are
        # runtime-only, and globals are validated at deploy time, not
        # here). If nothing resolves the template at runtime, the user
        # message will contain a literal ``{{env.X}}`` - visible and
        # debuggable rather than silently broken at compile.
        return "{{env." + key + "}}"

    if expr.startswith("secret."):
        key = expr[7:]
        if secrets and key in secrets:
            return secrets[key]
        value = env.get(key)
        if value is not None:
            return value
        # Runtime passthrough - the compiler doesn't know about the
        # running user, so a missing secret at compile time might
        # still be resolvable at runtime via the CredentialStore
        # (per_user / per_app_per_user scopes are runtime-only). We
        # emit the original template back so the runtime resolver
        # ``digitorn.core.credentials.runtime_resolver`` can find it
        # and substitute with the correct user context.
        #
        # If nothing resolves the template at runtime either, the
        # user message will contain a literal ``{{secret.X}}``
        # string - visible, debuggable, not silently broken.
        return "{{secret." + key + "}}"

    # ── System variables (computed at compile time) ──
    if expr.startswith("sys."):
        key = expr[4:]
        sys_value = _get_sys_variable(key)
        if sys_value is not None:
            return sys_value
        raise ValueError(
            f"Unknown system variable '{key}' (referenced as '{{{{sys.{key}}}}}'). "
            f"Available: {', '.join(sorted(_SYS_VARIABLES))}"
        )

    # ── App variables (from the app: block, injected by the compiler) ──
    if expr.startswith("app."):
        key = expr[4:]
        app_value = variables.get(f"_app_{key}")
        if app_value is not None:
            return app_value
        raise ValueError(
            f"Unknown app variable '{key}' (referenced as '{{{{app.{key}}}}}'). "
            f"Available: id, name, version, author, description"
        )

    value = variables.get(expr)
    if value is None:
        # Dotpath expressions (e.g. event.payload.field, caller.name) are
        # runtime templates resolved by modules at execution time, not by
        # the compiler.  Compile-time variables never contain dots - they
        # are plain names in the ``variables:`` section. So any expression
        # with a dot that isn't env.*/secret.*/sys.*/app.* is a runtime
        # passthrough.
        if "." in expr:
            return "{{" + expr + "}}"
        raise ValueError(
            f"Variable '{expr}' not defined in the variables section"
        )
    return value


def collect_unresolved(data: Any) -> list[str]:
    """Return all ``{{...}}`` expressions still present in *data*.

    Useful for dry-run validation without raising errors.
    """
    found: list[str] = []

    if isinstance(data, str):
        found.extend(m.group(1).strip() for m in _VAR_PATTERN.finditer(data))
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str):
                found.extend(collect_unresolved(k))
            found.extend(collect_unresolved(v))
    elif isinstance(data, list):
        for item in data:
            found.extend(collect_unresolved(item))

    return found


# ── System variables ─────────────────────────────────────────────────


def _get_sys_variable(key: str) -> str | None:
    """Resolve a ``sys.*`` variable at compile time."""
    if key in _SYS_VARIABLES:
        return _SYS_VARIABLES[key]()
    return None


def _digitorn_version() -> str:
    try:
        from digitorn import __version__
        return __version__
    except (ImportError, AttributeError):
        return "dev"


def _detect_system_shell() -> str:
    """Best-effort detection of the primary interactive shell on this host."""
    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC")
        if comspec:
            return comspec
        for candidate in ("powershell.exe", "pwsh.exe", "cmd.exe"):
            found = shutil.which(candidate)
            if found:
                return found
        return "cmd.exe"

    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/sh"


def _detect_shell_family() -> str:
    shell = os.path.basename(_detect_system_shell()).lower()
    if shell in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
        return "powershell"
    if shell in {"cmd.exe", "cmd"}:
        return "cmd"
    if "bash" in shell:
        return "bash"
    if shell in {"sh", "dash", "zsh", "fish"}:
        return shell
    return "unknown"


# Each entry is a callable returning the current value (evaluated at
# compile time - values are snapshot when the YAML is compiled).
_SYS_VARIABLES: dict[str, Any] = {
    "timestamp":        lambda: datetime.now(timezone.utc).isoformat(),
    "date":             lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "time":             lambda: datetime.now(timezone.utc).strftime("%H:%M:%S"),
    "hostname":         lambda: socket.gethostname(),
    "platform":         lambda: sys.platform,
    "os":               lambda: _platform.system(),
    "arch":             lambda: _platform.machine(),
    "python_version":   lambda: _platform.python_version(),
    "cwd":              lambda: os.getcwd(),
    "user":             lambda: os.environ.get("USER", os.environ.get("USERNAME", "unknown")),
    "pid":              lambda: str(os.getpid()),
    "digitorn_version": _digitorn_version,
    "home":             lambda: str(os.path.expanduser("~")),
    "tmpdir":           lambda: tempfile.gettempdir(),
    "temp_dir":         lambda: tempfile.gettempdir(),
    "locale":           lambda: os.environ.get("LANG", os.environ.get("LC_ALL", "C")),
    "shell":            _detect_system_shell,
    "shell_family":     _detect_shell_family,
    "path_sep":         lambda: os.sep,
    "is_windows":       lambda: "true" if sys.platform == "win32" else "false",
    "is_linux":         lambda: "true" if sys.platform.startswith("linux") else "false",
    "is_macos":         lambda: "true" if sys.platform == "darwin" else "false",
}


def _resolve_prompt(key: str) -> str:
    """Inline the content of ``prompts/<key>`` as a string.

    Tries the extensions listed in ``_TEXT_EXTENSIONS`` in order -
    the first match wins. Raises ``ValueError`` when no prompt
    file is found so the compiler surfaces the bad reference.

    Returns the raw file content with trailing whitespace stripped.
    Markdown code fences and frontmatter pass through unchanged -
    the compiler doesn't interpret prompt files.
    """
    ctx = _BUNDLE_CTX.get()
    if ctx is None or ctx.get("bundle_dir") is None:
        # No bundle context - passthrough so the caller sees the
        # template. Useful for tests and legacy code paths.
        return "{{prompt." + key + "}}"
    return _read_text_file(
        ctx["bundle_dir"], "prompts", key,
        namespace_label="prompt",
    )


def _resolve_skill(key: str) -> str:
    """Inline the content of ``skills/<key>``. Same resolution
    order as ``_resolve_prompt`` - see above."""
    ctx = _BUNDLE_CTX.get()
    if ctx is None or ctx.get("bundle_dir") is None:
        return "{{skill." + key + "}}"
    return _read_text_file(
        ctx["bundle_dir"], "skills", key,
        namespace_label="skill",
    )


def _resolve_behavior(key: str) -> str:
    """Inline the content of ``behavior/<key>.yaml`` as a JSON string.

    Used in the ``behavior.profile`` field to reference a custom profile
    defined in the bundle's ``behavior/`` directory::

        behavior:
          profile: "{{behavior.strict_dev}}"

    The YAML file is parsed and returned as a JSON string so the
    variable resolver can inject a structured dict into the profile
    field. The compiler or engine then parses it back.

    File format (``behavior/strict_dev.yaml``)::

        # Custom behavior profile
        name: strict_dev
        description: "Ultra-strict dev rules"
        extends: dev

        rules:
          read_before_edit: true
          test_after_changes: true
          max_blind_reads: 1

        prompt: |
          Additional behavioral instructions...

        custom:
          - id: protect_migrations
            rule: "Never modify migration files without asking"
            trigger: edit
            action: block
    """
    import json
    import yaml as _yaml

    ctx = _BUNDLE_CTX.get()
    if ctx is None or ctx.get("bundle_dir") is None:
        return "{{behavior." + key + "}}"

    bundle_dir = ctx["bundle_dir"]
    base = (bundle_dir / "behavior").resolve()
    if not base.is_dir():
        raise ValueError(
            f"behavior namespace: 'behavior/' dir not found under "
            f"'{bundle_dir}'. Create it and add a YAML file for '{key}'."
        )

    # Try .yaml, .yml, bare name
    candidates = [
        base / f"{key}.yaml",
        base / f"{key}.yml",
        base / key,
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(base)
        except (ValueError, OSError):
            continue
        if resolved.is_file():
            try:
                raw = resolved.read_text(encoding="utf-8")
                data = _yaml.safe_load(raw)
            except (_yaml.YAMLError, OSError) as exc:
                raise ValueError(
                    f"behavior '{key}': failed to load {resolved}: {exc}"
                )
            if not isinstance(data, dict):
                raise ValueError(
                    f"behavior '{key}': expected a YAML mapping, "
                    f"got {type(data).__name__}"
                )
            return json.dumps(data)

    available = _sample_dir(base, limit=10)
    raise ValueError(
        f"behavior '{key}' not found under '{base}'. "
        f"Available: {available}"
    )


def _resolve_asset(key: str) -> str:
    """Return the URL a Flutter client uses to fetch an asset.

    ``{{asset.logo.svg}}``   → ``/api/apps/<app_id>/assets/assets/logo.svg``
    ``{{asset.logo}}``       → tries ``assets/logo`` + common image
                               extensions, returns the URL of the first match
    ``{{asset.sub/dir/x}}``  → ``/api/apps/<app_id>/assets/assets/sub/dir/x``

    When ``bundle_dir`` is set, the compiler verifies the file
    exists and raises ``ValueError`` if not - catches typos at
    compile time instead of at runtime. When bundle_dir is absent,
    the URL is returned unverified (useful for tests / non-compile
    callers).
    """
    ctx = _BUNDLE_CTX.get()
    if ctx is None:
        return "{{asset." + key + "}}"
    bundle_dir = ctx.get("bundle_dir")
    app_id = ctx.get("app_id") or ""

    rel_path: str | None = None
    if bundle_dir is not None:
        rel_path = _find_asset(bundle_dir, key)
        if rel_path is None:
            raise ValueError(
                f"Asset '{key}' not found. Looked under "
                f"'{bundle_dir}/assets/' - available files: "
                f"{_sample_assets(bundle_dir)}"
            )
    else:
        # No bundle dir - trust the caller's key as-is.
        rel_path = f"assets/{key}"

    # Build the client URL. ``app_id`` may be empty when the
    # compiler calls this before the app block is parsed - fall
    # back to a placeholder the client can substitute at runtime.
    if app_id:
        return f"/api/apps/{app_id}/{rel_path}"
    return f"/api/apps/{{app_id}}/{rel_path}"


def _resolve_asset_b64(key: str) -> str:
    """Return a ``data:<mime>;base64,<payload>`` URI for an asset.

    Used to inline small icons directly in HTML/SVG or in LLM
    prompts without a separate HTTP round-trip. Size-capped at
    64 kB by default - larger assets raise ``ValueError`` with
    a hint to use ``{{asset.X}}`` (URL) instead. The cap protects
    against accidentally inlining a 5 MB PDF.

    Override the cap via ``DIGITORN_ASSET_B64_MAX_BYTES`` env var.
    """
    import base64
    import mimetypes

    ctx = _BUNDLE_CTX.get()
    if ctx is None or ctx.get("bundle_dir") is None:
        return "{{asset_b64." + key + "}}"

    bundle_dir = ctx["bundle_dir"]
    rel_path = _find_asset(bundle_dir, key)
    if rel_path is None:
        raise ValueError(
            f"Asset '{key}' not found for asset_b64 under "
            f"'{bundle_dir}/assets/'. Available: "
            f"{_sample_assets(bundle_dir)}"
        )

    target = (bundle_dir / rel_path).resolve()
    try:
        target.relative_to(bundle_dir)
    except ValueError:
        raise ValueError(f"asset_b64 path escapes bundle: {key}")

    max_bytes = _asset_b64_cap()
    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"Asset '{key}' is {size} bytes, exceeds the "
            f"{max_bytes}-byte cap for asset_b64. Use "
            f"{{{{asset.{key}}}}} (URL) instead for large files, or "
            f"bump DIGITORN_ASSET_B64_MAX_BYTES."
        )

    data = target.read_bytes()
    mime, _ = mimetypes.guess_type(target.name)
    if not mime:
        mime = "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _asset_b64_cap() -> int:
    """Return the max byte size for asset_b64. 64 kB default."""
    raw = os.environ.get("DIGITORN_ASSET_B64_MAX_BYTES", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 64 * 1024


def _resolve_include(key: str) -> Any:
    """Inline a YAML fragment file into the parent structure.

    ``{{include:fragments/main_brain.yaml}}`` reads the YAML file
    at ``<bundle_dir>/fragments/main_brain.yaml``, parses it, and
    returns the parsed Python object. This lets authors factor
    shared blocks between agents:

    .. code-block:: yaml

        agents:
          - id: main
            brain: "{{include:fragments/main_brain.yaml}}"
          - id: backup
            brain: "{{include:fragments/main_brain.yaml}}"

    Guards:
    - Path traversal check (must stay under bundle_dir)
    - Recursion depth (via the existing _MAX_DEPTH in resolve_variables)
    - File must exist and parse as YAML
    """
    import yaml as _yaml

    ctx = _BUNDLE_CTX.get()
    if ctx is None or ctx.get("bundle_dir") is None:
        return "{{include:" + key + "}}"

    bundle_dir = ctx["bundle_dir"]
    target = (bundle_dir / key).resolve()
    try:
        target.relative_to(bundle_dir)
    except ValueError:
        raise ValueError(f"include path escapes bundle: {key}")
    if not target.is_file():
        raise ValueError(
            f"include '{key}' not found under '{bundle_dir}'"
        )
    try:
        with open(target, "r", encoding="utf-8") as fh:
            loaded = _yaml.safe_load(fh)
    except _yaml.YAMLError as exc:
        raise ValueError(f"include '{key}' is not valid YAML: {exc}")

    # The parent expects a string in the template slot. When the
    # loaded value is a dict/list, yaml.safe_dump it so substitution
    # remains valid. Callers that want structured injection (rare)
    # can parse the resulting YAML string themselves.
    #
    # Actually this is the subtle part: the template is inside a
    # string position. If the user wrote ``brain: "{{include:...}}"``
    # and the file contains a dict, yaml post-parse will see a
    # string, not a dict. The cleanest fix is for ``include`` to
    # be processed BEFORE variable string resolution - a two-pass
    # compile. For v1 we support only scalar includes (strings);
    # for structured YAML includes the recommended form is to put
    # the template as a bare value (no quotes), which makes the
    # parent dict a {key: "{{include:...}}"} and we return the
    # loaded structure directly. The caller sees a string → dict
    # replacement via the resolver's recursion.
    if isinstance(loaded, (dict, list)):
        import json
        return json.dumps(loaded)  # JSON is valid YAML, parses back
    if loaded is None:
        return ""
    return str(loaded)


def _read_text_file(
    bundle_dir: Path, subdir: str, key: str, *, namespace_label: str,
) -> str:
    """Resolve ``<bundle_dir>/<subdir>/<key>`` trying several
    extensions. Guards against path traversal outside the subdir.
    """
    base = (bundle_dir / subdir).resolve()
    if not base.is_dir():
        raise ValueError(
            f"{namespace_label} namespace: '{subdir}/' dir not "
            f"found under '{bundle_dir}'. Create it and add a "
            f"file for '{key}'."
        )

    # Resolve against the compile-time locale if set. Locale-
    # suffixed variants (``X.fr.md``) win over the default
    # (``X.md``) when a match exists. Falls through to the
    # default when the locale-specific file is missing.
    ctx = _BUNDLE_CTX.get() or {}
    locale = ctx.get("locale") or ""

    candidates: list[Path] = []
    # Locale-suffixed candidates first
    if locale and "." not in Path(key).name:
        for ext in _TEXT_EXTENSIONS:
            candidates.append(base / f"{key}.{locale}{ext}")
    # If the key already has an extension, try it verbatim
    if "." in Path(key).name:
        candidates.append(base / key)
    # Default (unsuffixed) candidates
    for ext in _TEXT_EXTENSIONS:
        candidates.append(base / f"{key}{ext}")

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(base)
        except (ValueError, OSError):
            continue
        if resolved.is_file():
            try:
                raw = resolved.read_text(encoding="utf-8").rstrip()
            except OSError as exc:
                raise ValueError(
                    f"{namespace_label} '{key}': failed to read "
                    f"{resolved}: {exc}"
                )
            # Strip YAML frontmatter (--- ... --- at file head).
            # The frontmatter block is optional metadata for the
            # prompt / skill; the compiler records it for later
            # validation but returns only the body here.
            body, frontmatter = _split_frontmatter(raw)
            _record_prompt_metadata(
                namespace_label, key, frontmatter,
            )
            # Rewrite markdown image references to asset URLs the
            # Flutter client can fetch. Handles both ![alt](path)
            # and <img src="path">.
            return _rewrite_markdown_assets(
                body, bundle_dir, resolved.parent,
            )

    available = _sample_dir(base, limit=10)
    raise ValueError(
        f"{namespace_label} '{key}' not found under '{base}'. "
        f"Available: {available}"
    )


_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL,
)


def _split_frontmatter(text: str) -> tuple[str, dict[str, Any]]:
    """Strip a YAML frontmatter block from the head of a prompt.

    Frontmatter format (standard markdown convention)::

        ---
        version: 2
        max_tokens_estimate: 1200
        min_model: claude-sonnet-4-5
        variables_required: [user_name, company]
        description: "Main system prompt for the assistant"
        ---

        You are ...

    Returns ``(body, frontmatter_dict)``. When no frontmatter is
    present, returns ``(text, {})`` - the vast majority of prompt
    files don't have frontmatter.
    """
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return text, {}
    fm_raw = match.group(1)
    body = text[match.end():]
    try:
        import yaml as _yaml
        fm = _yaml.safe_load(fm_raw) or {}
        if not isinstance(fm, dict):
            return body, {}
    except Exception:
        return body, {}
    return body, fm


def _record_prompt_metadata(
    namespace_label: str, key: str, frontmatter: dict[str, Any],
) -> None:
    """Store parsed frontmatter on the bundle context so the compiler
    can run validation after all prompts/skills have been loaded.

    Errors on frontmatter fields (missing required vars, unknown
    model, etc.) are collected in a shared list on the context and
    surfaced by the compiler as a single batch.
    """
    if not frontmatter:
        return
    ctx = _BUNDLE_CTX.get()
    if ctx is None:
        return
    metadata = ctx.setdefault("prompt_metadata", {})
    metadata[f"{namespace_label}.{key}"] = dict(frontmatter)


def _rewrite_markdown_assets(
    text: str, bundle_dir: Path, prompt_dir: Path,
) -> str:
    """Rewrite ``![alt](path)`` and ``<img src="path">`` references
    in a prompt's markdown body to asset URLs under ``/api/apps/...``.

    The rewriter only touches paths that:
    - Are relative (no scheme, no leading ``/``)
    - Resolve to a real file under the bundle dir
    - Stay inside the bundle dir (no ``../..`` escape)

    Absolute URLs (``http://``, ``https://``, ``data:``) and
    references that don't match a real file pass through
    unchanged, letting authors link to external images.
    """
    ctx = _BUNDLE_CTX.get()
    app_id = (ctx or {}).get("app_id", "") if ctx else ""
    app_id_seg = app_id or "{app_id}"

    def _replace(match: re.Match[str]) -> str:
        whole = match.group(0)
        # Group 2 = markdown path, group 3 = html src
        path = match.group(2) or match.group(3) or ""
        if not path:
            return whole
        # Skip external URLs
        if path.startswith((
            "http://", "https://", "data:", "mailto:", "/api/apps/",
        )):
            return whole
        # Resolve relative to the prompt file's directory first,
        # then to the bundle dir as fallback
        for base in (prompt_dir, bundle_dir):
            candidate = (base / path).resolve()
            try:
                rel = candidate.relative_to(bundle_dir)
            except ValueError:
                continue
            if candidate.is_file():
                new_url = f"/api/apps/{app_id_seg}/assets/{rel.as_posix()}"
                return whole.replace(path, new_url)
        return whole

    return _MD_IMAGE_PATTERN.sub(_replace, text)


def _find_asset(bundle_dir: Path, key: str) -> str | None:
    """Return the relative path under the bundle dir for an asset
    key, trying common extensions. None if nothing matches.

    Returns paths like ``assets/logo.png`` suitable for concatenation
    into the asset-serving URL.
    """
    base = (bundle_dir / "assets").resolve()
    if not base.is_dir():
        return None
    # Exact name first
    candidates: list[Path] = [base / key]
    # Fuzzy on extension when none provided
    if "." not in Path(key).name:
        for ext in _ASSET_EXTENSIONS:
            if ext:
                candidates.append(base / f"{key}{ext}")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(base)
        except (ValueError, OSError):
            continue
        if resolved.is_file():
            rel = resolved.relative_to(bundle_dir).as_posix()
            return rel
    return None


def _sample_dir(path: Path, *, limit: int = 10) -> list[str]:
    try:
        return sorted(
            p.name for p in path.iterdir() if p.is_file()
        )[:limit]
    except OSError:
        return []


def _sample_assets(bundle_dir: Path, *, limit: int = 10) -> list[str]:
    return _sample_dir(bundle_dir / "assets", limit=limit)


def inject_app_variables(
    variables: dict[str, str],
    app_meta: Any,
) -> dict[str, str]:
    """Inject ``app.*`` variables from the AppMeta into the variables dict.

    Called by the compiler before variable resolution so that
    ``{{app.id}}``, ``{{app.name}}``, etc. are available in all
    config blocks, setup params, and constraints.

    Args:
        variables: The user's ``variables:`` dict (mutated in place).
        app_meta: The ``AppMeta`` object from the parsed YAML.

    Returns:
        The enriched variables dict (same reference as input).
    """
    variables["_app_id"] = getattr(app_meta, "app_id", "")
    variables["_app_name"] = getattr(app_meta, "name", "")
    variables["_app_version"] = getattr(app_meta, "version", "1.0")
    variables["_app_author"] = getattr(app_meta, "author", "")
    variables["_app_description"] = getattr(app_meta, "description", "")
    return variables
