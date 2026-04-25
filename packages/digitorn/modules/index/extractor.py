"""Index module — extractor system.

Extractors transform raw content into IndexEntry + Relation lists.
The module ships with a ``TextExtractor`` (fallback) and a ``PythonExtractor``
(understands classes, functions, imports).

Other modules register custom extractors at runtime via the service bus.
"""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Any, Protocol

from .types import IndexEntry, Relation, make_entry_id


class Extractor(Protocol):
    """Interface for content extractors.

    Any module can provide an extractor by implementing this protocol
    and registering it via ``index.register_extractor``.
    """

    name: str

    def supports(self, path: str, metadata: dict[str, Any]) -> bool:
        """Return True if this extractor can handle the given content."""
        ...

    def extract(
        self,
        source_id: str,
        path: str,
        content: str,
        metadata: dict[str, Any],
    ) -> tuple[list[IndexEntry], list[Relation]]:
        """Parse content and return entries + relations."""
        ...


class TextExtractor:
    """Simple line-based extractor — works on any text file.

    Produces a single ``file`` entry per file with line count and hash.
    """

    name = "text"

    def supports(self, path: str, metadata: dict[str, Any]) -> bool:
        return True

    def extract(
        self,
        source_id: str,
        path: str,
        content: str,
        metadata: dict[str, Any],
    ) -> tuple[list[IndexEntry], list[Relation]]:
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        entry = IndexEntry(
            entry_id=make_entry_id(source_id, path, path.rsplit("/", 1)[-1]),
            source_id=source_id,
            path=path,
            kind="file",
            name=path.rsplit("/", 1)[-1],
            signature=f"file ({line_count} lines)",
            summary="",
            line_start=1,
            line_end=line_count,
            content_hash=content_hash,
            metadata={"language": _guess_language(path), **metadata},
        )
        return [entry], []


class PythonExtractor:
    """AST-based Python extractor — understands classes, functions, imports.

    Produces entries for each top-level and nested class/function, plus
    import relations.
    """

    name = "python"

    def supports(self, path: str, metadata: dict[str, Any]) -> bool:
        return path.endswith(".py")

    def extract(
        self,
        source_id: str,
        path: str,
        content: str,
        metadata: dict[str, Any],
    ) -> tuple[list[IndexEntry], list[Relation]]:
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        entries: list[IndexEntry] = []
        relations: list[Relation] = []

        line_count = content.count("\n") + 1
        file_id = make_entry_id(source_id, path, path.rsplit("/", 1)[-1])
        file_entry = IndexEntry(
            entry_id=file_id,
            source_id=source_id,
            path=path,
            kind="file",
            name=path.rsplit("/", 1)[-1],
            signature=f"python file ({line_count} lines)",
            summary=_extract_module_docstring(content),
            line_start=1,
            line_end=line_count,
            content_hash=content_hash,
            metadata={"language": "python", **metadata},
        )
        entries.append(file_entry)

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return entries, relations

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entry = _function_entry(source_id, path, node, content_hash)
                entries.append(entry)
                relations.append(Relation(
                    from_id=file_id,
                    to_id=entry.entry_id,
                    kind="contains",
                ))

            elif isinstance(node, ast.ClassDef):
                entry = _class_entry(source_id, path, node, content_hash)
                entries.append(entry)
                relations.append(Relation(
                    from_id=file_id,
                    to_id=entry.entry_id,
                    kind="contains",
                ))
                for base in node.bases:
                    base_name = _node_name(base)
                    if base_name:
                        relations.append(Relation(
                            from_id=entry.entry_id,
                            to_id=make_entry_id(source_id, "", base_name),
                            kind="inherits",
                            metadata={"base": base_name},
                        ))

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for imp_entry, imp_rel in _import_entries(source_id, path, node, file_id):
                    entries.append(imp_entry)
                    relations.append(imp_rel)

        return entries, relations


def _function_entry(
    source_id: str, path: str, node: ast.FunctionDef | ast.AsyncFunctionDef,
    content_hash: str,
) -> IndexEntry:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    args = _format_args(node.args)
    returns = ""
    if node.returns:
        try:
            returns = f" -> {ast.unparse(node.returns)}"
        except Exception:
            pass  # Non-critical: return type annotation unparsing is best-effort
    signature = f"{prefix}def {node.name}({args}){returns}"
    docstring = ast.get_docstring(node) or ""
    summary = docstring.split("\n")[0] if docstring else ""

    return IndexEntry(
        entry_id=make_entry_id(source_id, path, node.name),
        source_id=source_id,
        path=path,
        kind="function",
        name=node.name,
        signature=signature,
        summary=summary,
        line_start=node.lineno,
        line_end=node.end_lineno,
        content_hash=content_hash,
    )


def _class_entry(
    source_id: str, path: str, node: ast.ClassDef, content_hash: str,
) -> IndexEntry:
    bases = ", ".join(_node_name(b) or "?" for b in node.bases)
    signature = f"class {node.name}({bases})" if bases else f"class {node.name}"
    docstring = ast.get_docstring(node) or ""
    summary = docstring.split("\n")[0] if docstring else ""

    return IndexEntry(
        entry_id=make_entry_id(source_id, path, node.name),
        source_id=source_id,
        path=path,
        kind="class",
        name=node.name,
        signature=signature,
        summary=summary,
        line_start=node.lineno,
        line_end=node.end_lineno,
        content_hash=content_hash,
    )


def _import_entries(
    source_id: str, path: str, node: ast.Import | ast.ImportFrom,
    file_id: str,
) -> list[tuple[IndexEntry, Relation]]:
    results = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            entry = IndexEntry(
                entry_id=make_entry_id(source_id, path, f"import:{name}"),
                source_id=source_id,
                path=path,
                kind="import",
                name=name,
                signature=f"import {name}",
                line_start=node.lineno,
                line_end=node.lineno,
                content_hash="",
            )
            rel = Relation(from_id=file_id, to_id=entry.entry_id, kind="imports")
            results.append((entry, rel))
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            name = f"{module}.{alias.name}" if module else alias.name
            entry = IndexEntry(
                entry_id=make_entry_id(source_id, path, f"import:{name}"),
                source_id=source_id,
                path=path,
                kind="import",
                name=name,
                signature=f"from {module} import {alias.name}",
                line_start=node.lineno,
                line_end=node.lineno,
                content_hash="",
            )
            rel = Relation(from_id=file_id, to_id=entry.entry_id, kind="imports")
            results.append((entry, rel))
    return results


def _format_args(args: ast.arguments) -> str:
    """Format function arguments as a compact string."""
    parts = []
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        name = arg.arg
        annotation = ""
        if arg.annotation:
            try:
                annotation = f": {ast.unparse(arg.annotation)}"
            except Exception:
                pass  # Non-critical: type annotation unparsing is best-effort
        default = ""
        di = i - defaults_offset
        if di >= 0 and di < len(args.defaults):
            try:
                default = f"={ast.unparse(args.defaults[di])}"
            except Exception:
                default = "=..."
        parts.append(f"{name}{annotation}{default}")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for arg in args.kwonlyargs:
        parts.append(arg.arg)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _node_name(node: ast.expr) -> str | None:
    """Get the name from a Name or Attribute node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _node_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _extract_module_docstring(content: str) -> str:
    """Extract the first line of a module docstring."""
    try:
        tree = ast.parse(content)
        docstring = ast.get_docstring(tree)
        return docstring.split("\n")[0] if docstring else ""
    except SyntaxError:
        return ""


def _guess_language(path: str) -> str:
    """Guess programming language from file extension."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "jsx", ".tsx": "tsx", ".rs": "rust", ".go": "go",
        ".java": "java", ".rb": "ruby", ".php": "php", ".c": "c",
        ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".cs": "csharp",
        ".sql": "sql", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
        ".json": "json", ".toml": "toml", ".xml": "xml", ".html": "html",
        ".css": "css", ".md": "markdown", ".txt": "text",
    }
    for ext, lang in ext_map.items():
        if path.endswith(ext):
            return lang
    return "unknown"


class ExtractorRegistry:
    """Manages extractors — builtin + custom from other modules."""

    def __init__(self) -> None:
        self._extractors: dict[str, Extractor] = {}
        self._remote: dict[str, dict[str, str]] = {}
        self.register(PythonExtractor())
        self.register(TextExtractor())

    def register(self, extractor: Extractor) -> None:
        self._extractors[extractor.name] = extractor

    def register_remote(
        self, name: str, module_id: str, action: str,
    ) -> None:
        """Register a remote extractor handled by another module."""
        self._remote[name] = {"module_id": module_id, "action": action}

    def get(self, name: str) -> Extractor | None:
        return self._extractors.get(name)

    def get_remote(self, name: str) -> dict[str, str] | None:
        return self._remote.get(name)

    def resolve(self, name: str, path: str, metadata: dict) -> Extractor | None:
        """Find the best extractor for this content."""
        if name in self._extractors:
            return self._extractors[name]
        for ext in self._extractors.values():
            if ext.name != "text" and ext.supports(path, metadata):
                return ext
        return self._extractors.get("text")

    def list_extractors(self) -> list[str]:
        return list(self._extractors.keys()) + list(self._remote.keys())
