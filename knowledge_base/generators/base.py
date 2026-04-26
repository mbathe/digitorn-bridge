"""Base contract every KB doc generator implements.

Principles:
  - Generators derive content from CODE — no hand-written facts.
  - Output is deterministic: run twice, get the same bytes.
  - Two modes: ``write`` (update disk) and ``check`` (detect drift, exit 1
    on divergence). ``check`` is the CI gate.
  - A generator owns exactly one output directory. Files in that dir
    that aren't in the generator's current output are reported as
    PHANTOM — stale docs auto-detected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DriftReport:
    """What's out of sync between code and docs."""

    generator: str
    missing: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    phantom: list[Path] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not (self.missing or self.changed or self.phantom)

    def summary(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append(f"{len(self.missing)} missing")
        if self.changed:
            parts.append(f"{len(self.changed)} changed")
        if self.phantom:
            parts.append(f"{len(self.phantom)} phantom")
        return ", ".join(parts) if parts else "clean"

    def detail(self, repo_root: Path) -> list[str]:
        lines: list[str] = []
        for p in self.missing:
            lines.append(f"  MISSING: {p.relative_to(repo_root)}")
        for p in self.changed:
            lines.append(f"  CHANGED: {p.relative_to(repo_root)}")
        for p in self.phantom:
            lines.append(f"  PHANTOM: {p.relative_to(repo_root)} (no source in code)")
        return lines


class DocGenerator(ABC):
    """One generator = one documentation surface derived from code."""

    #: short identifier, used in CLI output (e.g. "modules", "schema")
    name: str = ""

    @property
    @abstractmethod
    def output_dir(self) -> Path:
        """Directory this generator owns — contents outside ``generate()`` are phantoms."""

    #: glob pattern for files under ``output_dir`` this generator owns.
    #: Defaults to ``*.md`` — subclasses can override if they produce other extensions.
    output_glob: str = "*.md"

    @abstractmethod
    def generate(self) -> dict[Path, str]:
        """Return a map of ``absolute_path -> content`` for every doc this generator emits."""

    # ── operations ────────────────────────────────────────────────

    def write(self) -> tuple[int, int]:
        """Write all docs to disk + delete phantoms. Returns (written, removed)."""
        docs = self.generate()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for path, content in docs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written += 1

        removed = 0
        for existing in self.output_dir.glob(self.output_glob):
            if existing not in docs:
                existing.unlink()
                removed += 1

        return written, removed

    def check(self) -> DriftReport:
        """Compare generated output against disk. Never writes."""
        docs = self.generate()
        report = DriftReport(generator=self.name)
        for path, content in docs.items():
            if not path.exists():
                report.missing.append(path)
            elif path.read_text(encoding="utf-8") != content:
                report.changed.append(path)

        if self.output_dir.exists():
            for existing in self.output_dir.glob(self.output_glob):
                if existing not in docs:
                    report.phantom.append(existing)

        return report
