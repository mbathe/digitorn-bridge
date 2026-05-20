"""Template variable resolution for app YAML."""

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

_BUNDLE_CTX: ContextVar[dict[str, Any] | None] = ContextVar(
    "_variables_bundle_ctx", default=None,
)


# File extensions the text-inlining namespaces look for, in order.
_TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".prompt", "")

# Common image/asset extensions for the `asset.X` fuzzy lookup
# when the caller writes `{{asset.logo}}` without an extension.
_ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico",
    ".pdf", ".json", ".yaml", ".yml", ".csv", ".txt", "",
)


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
    """Recursively resolve `{{...}}` templates in *data*."""
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
    """Return the frontmatter metadata collected during the current"""
    ctx = _BUNDLE_CTX.get()
    if ctx is None:
        return {}
    return dict(ctx.get("prompt_metadata") or {})


class bundle_context:
    """Context manager that pre-seeds the bundle ctx for a whole"""

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
    """Scan a bundle's prompts directory and return the set of"""
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
    """Resolve a single expression (without braces)."""
    if "??" in expr:
        parts = expr.split("??", 1)
        left = parts[0].strip()
        right = parts[1].strip()
        try:
            result = _lookup(left, variables, env, secrets)
        except ValueError:
            return _lookup(right, variables, env, secrets)
        if result == "{{" + left + "}}":
            return _lookup(right, variables, env, secrets)
        return result

    if (expr.startswith("'") and expr.endswith("'")) or (
        expr.startswith('"') and expr.endswith('"')
    ):
        return expr[1:-1]

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
        if key == "PWD":
            return os.getcwd()
        return "{{env." + key + "}}"

    if expr.startswith("secret."):
        key = expr[7:]
        if secrets and key in secrets:
            return secrets[key]
        value = env.get(key)
        if value is not None:
            return value
        return "{{secret." + key + "}}"

    if expr.startswith("sys."):
        key = expr[4:]
        sys_value = _get_sys_variable(key)
        if sys_value is not None:
            return sys_value
        raise ValueError(
            f"Unknown system variable '{key}' (referenced as '{{{{sys.{key}}}}}'). "
            f"Available: {', '.join(sorted(_SYS_VARIABLES))}"
        )

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
        if "." in expr:
            return "{{" + expr + "}}"
        raise ValueError(
            f"Variable '{expr}' not defined in the variables section"
        )
    return value


def collect_unresolved(data: Any) -> list[str]:
    """Return all `{{...}}` expressions still present in *data*."""
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


def _get_sys_variable(key: str) -> str | None:
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
    """Inline the content of `prompts/<key>` as a string."""
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
    """Inline the content of `skills/<key>`. Same resolution"""
    ctx = _BUNDLE_CTX.get()
    if ctx is None or ctx.get("bundle_dir") is None:
        return "{{skill." + key + "}}"
    return _read_text_file(
        ctx["bundle_dir"], "skills", key,
        namespace_label="skill",
    )


def _resolve_behavior(key: str) -> str:
    """Inline the content of `behavior/<key>.yaml` as a JSON string."""
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
                from digitorn.core.app.yaml_loader import safe_load_strict
                raw = resolved.read_text(encoding="utf-8")
                data = safe_load_strict(raw)
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
    """Return the URL a client uses to fetch an asset."""
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

    if app_id:
        return f"/api/apps/{app_id}/{rel_path}"
    return f"/api/apps/{{app_id}}/{rel_path}"


def _resolve_asset_b64(key: str) -> str:
    """Return a `data:<mime>;base64,<payload>` URI for an asset."""
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
    raw = os.environ.get("DIGITORN_ASSET_B64_MAX_BYTES", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 64 * 1024


def _resolve_include(key: str) -> Any:
    """Inline a YAML fragment file into the parent structure."""
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
        from digitorn.core.app.yaml_loader import safe_load_strict
        with open(target, "r", encoding="utf-8") as fh:
            loaded = safe_load_strict(fh.read())
    except _yaml.YAMLError as exc:
        raise ValueError(f"include '{key}' is not valid YAML: {exc}")

    if isinstance(loaded, (dict, list)):
        import json
        return json.dumps(loaded)  # JSON is valid YAML, parses back
    if loaded is None:
        return ""
    return str(loaded)


def _read_text_file(
    bundle_dir: Path, subdir: str, key: str, *, namespace_label: str,
) -> str:
    """Resolve `<bundle_dir>/<subdir>/<key>` trying several"""
    base = (bundle_dir / subdir).resolve()
    if not base.is_dir():
        raise ValueError(
            f"{namespace_label} namespace: '{subdir}/' dir not "
            f"found under '{bundle_dir}'. Create it and add a "
            f"file for '{key}'."
        )

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
            body, frontmatter = _split_frontmatter(raw)
            _record_prompt_metadata(
                namespace_label, key, frontmatter,
            )
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
    """Strip a YAML frontmatter block from the head of a prompt."""
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return text, {}
    fm_raw = match.group(1)
    body = text[match.end():]
    try:
        from digitorn.core.app.yaml_loader import safe_load_strict
        fm = safe_load_strict(fm_raw) or {}
        if not isinstance(fm, dict):
            return body, {}
    except Exception:
        return body, {}
    return body, fm


def _record_prompt_metadata(
    namespace_label: str, key: str, frontmatter: dict[str, Any],
) -> None:
    """Store parsed frontmatter on the bundle context so the compiler"""
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
    """Rewrite `![alt](path)` and `<img src="path">` references"""
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
    """Return the relative path under the bundle dir for an asset"""
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
    """Inject `app.*` variables from the AppMeta into the variables dict."""
    variables["_app_id"] = getattr(app_meta, "app_id", "")
    variables["_app_name"] = getattr(app_meta, "name", "")
    variables["_app_version"] = getattr(app_meta, "version", "1.0")
    variables["_app_author"] = getattr(app_meta, "author", "")
    variables["_app_description"] = getattr(app_meta, "description", "")
    return variables
