"""Workspace layout manager.

Manages the .digitorn/ directory structure inside the user's workspace.
Handles both CLI mode (workspace = user's cwd) and web/daemon mode
(workspace = server-managed directory).

Structure:
    {workspace}/
      .digitorn/
        skills/                           Global skills (shared across apps)
        rules/                            Modular instruction files (*.md)
        apps/
          {app_id}/
            skills/                       App-specific skills
            rules/                        App-specific rules
            .digitorn.md                  App project memory
            sessions/
              {session_id}/
                checkpoints/              File checkpoints for this session

In BD (not filesystem):
    messages, memory_snapshot, user sessions, secrets, audit log
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


class WorkspaceLayout:
    """Resolves paths within the .digitorn/ workspace structure.

    Two layouts coexist:

    - **Shared** (default): the workspace is a directory that may host
      multiple apps and multiple sessions (CLI use, user-picked
      project folder). Per-app and per-session metadata is segregated
      under `.digitorn/apps/{app_id}/sessions/{sid}/`.

    - **Per-session** (`per_session=True`): the workspace is ALREADY
      scoped to a single (app, session) pair - typically the daemon
      auto-workspace `~/.digitorn/workspaces/{app_id}/{sid}/`. The
      `apps/{app_id}/` and `sessions/{sid}/` segments are dropped
      because the workspace path already provides that segregation;
      otherwise the layout repeats the app_id and session_id once
      uselessly inside `.digitorn/`. Skills, rules, memory and
      checkpoints all land directly under `.digitorn/` in this mode.

    - **Project-shared** (`per_session=True` +
      `external_session_dir`): the workspace is a
      `~/.digitorn/workdirs/{app_id}/{user_id}/{slug}/` dir shared
      by every session of the named project. Project-level artifacts
      (`skills/`, `rules/`, project memory) stay in
      `{workdir}/.digitorn/` so the user can edit them; per-session
      state (checkpoints, future per-session files) lives at
      `external_session_dir` instead - typically
      `~/.digitorn/workspaces/{app_id}/{session_id}/` - to prevent
      concurrent sessions from trampling each other's state and to
      keep the workdir clean of daemon internals the agent shouldn't
      mutate.
    """

    def __init__(
        self,
        workspace: str | Path,
        app_id: str,
        *,
        per_session: bool = False,
        external_session_dir: Path | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.app_id = app_id
        self._root = self.workspace / ".digitorn"
        self._per_session = per_session
        self._external_session_dir = (
            external_session_dir.resolve() if external_session_dir else None
        )
        # In per-session layout, app_dir collapses to _root so every
        # app-scoped path (skills, rules, memory) flattens too.
        self._app_dir = (
            self._root if per_session else self._root / "apps" / app_id
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def global_skills_dir(self) -> Path:
        return self._root / "skills"

    @property
    def app_dir(self) -> Path:
        return self._app_dir

    @property
    def app_skills_dir(self) -> Path:
        return self._app_dir / "skills"

    @property
    def global_rules_dir(self) -> Path:
        return self._root / "rules"

    @property
    def app_rules_dir(self) -> Path:
        return self._app_dir / "rules"

    @property
    def app_memory_file(self) -> Path:
        return self._app_dir / ".digitorn.md"

    def session_dir(self, session_id: str) -> Path:
        # Project-shared workdirs route per-session state to a daemon-
        # private location so multiple sessions on the same workdir
        # don't trample each other and so the workdir stays clean.
        if self._external_session_dir is not None:
            return self._external_session_dir
        # Per-session workspaces are already segregated by the session
        # id at the workspace root; collapse the nested segment.
        if self._per_session:
            return self._app_dir
        return self._app_dir / "sessions" / session_id

    def session_checkpoints_dir(self, session_id: str) -> Path:
        if self._external_session_dir is not None:
            return self._external_session_dir / "checkpoints"
        if self._per_session:
            return self._root / "checkpoints"
        return self.session_dir(session_id) / "checkpoints"

    def ensure_app_dirs(self) -> None:
        """Create the app directory structure if it doesn't exist."""
        self._root.mkdir(parents=True, exist_ok=True)
        self.global_skills_dir.mkdir(exist_ok=True)
        self._app_dir.mkdir(parents=True, exist_ok=True)
        self.app_skills_dir.mkdir(exist_ok=True)

    def ensure_session_dirs(self, session_id: str) -> None:
        """Create session directories."""
        self.ensure_app_dirs()
        self.session_dir(session_id).mkdir(parents=True, exist_ok=True)
        self.session_checkpoints_dir(session_id).mkdir(exist_ok=True)

    def load_project_memory(self) -> str | None:
        """Load the app's project memory file + modular rules.

        Sources (in order, first match wins for base memory):
          1. `.digitorn/apps/{app_id}/.digitorn.md`
          2. `{workspace}/.digitorn.md`
          3. `{workspace}/CLAUDE.md`

        Rules from `.digitorn/rules/` are always appended.
        """
        base: str | None = None

        path = self.app_memory_file
        if path.exists() and path.is_file():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    logger.debug("project_memory_loaded", path=str(path), chars=len(content))
                    base = content
            except OSError:
                pass

        if base is None:
            for name in [".digitorn.md", "CLAUDE.md"]:
                fallback = self.workspace / name
                if fallback.exists() and fallback.is_file():
                    try:
                        content = fallback.read_text(encoding="utf-8").strip()
                        if content:
                            logger.debug("project_memory_loaded", path=str(fallback), chars=len(content))
                            base = content
                            break
                    except OSError:
                        pass

        # Append modular rules
        rules = self.load_rules()
        if rules:
            base = f"{base}\n\n{rules}" if base else rules

        return base

    def load_skills(self) -> dict[str, str]:
        """Load all skills from global + app directories.

        Returns:
            Dict of command_name -> file content.
            Global skills are loaded first, app skills override.
        """
        skills: dict[str, str] = {}

        for skills_dir in [self.global_skills_dir, self.app_skills_dir]:
            if not skills_dir.exists():
                continue
            for path in sorted(skills_dir.glob("*.md")):
                name = path.stem
                try:
                    content = path.read_text(encoding="utf-8").strip()
                    if content:
                        skills[name] = content
                except OSError:
                    logger.warning("skill_load_failed", path=str(path))

        if skills:
            logger.debug("skills_loaded", count=len(skills), names=list(skills.keys()))

        return skills

    def load_rules(self, active_paths: list[str] | None = None) -> str | None:
        """Load modular rules from global + app rules directories.

        Rules are `.md` files in `.digitorn/rules/` (global) and
        `.digitorn/apps/{app_id}/rules/` (app-specific).

        Each file may have optional YAML frontmatter to scope it::

            ---
            paths: ["src/api/**"]
            ---
            All API routes must validate input with Pydantic.

        Args:
            active_paths: If provided, only include rules whose `paths:`
                frontmatter matches at least one active path (glob).
                Rules without `paths:` frontmatter are always included.

        Returns:
            Concatenated rules text, or None if no rules found.
        """
        import fnmatch
        import re

        fragments: list[str] = []

        for rules_dir in [self.global_rules_dir, self.app_rules_dir]:
            if not rules_dir.exists():
                continue
            for path in sorted(rules_dir.rglob("*.md")):
                try:
                    raw = path.read_text(encoding="utf-8").strip()
                    if not raw:
                        continue
                except OSError:
                    logger.warning("rule_load_failed", path=str(path))
                    continue

                # Parse optional frontmatter
                content = raw
                rule_paths: list[str] = []
                fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
                if fm_match:
                    content = raw[fm_match.end():].strip()
                    fm_text = fm_match.group(1)
                    # Simple paths: extraction (avoid yaml dependency)
                    pm = re.search(r'paths:\s*\[([^\]]*)\]', fm_text)
                    if pm:
                        rule_paths = [
                            p.strip().strip('"').strip("'")
                            for p in pm.group(1).split(",")
                            if p.strip()
                        ]

                if not content:
                    continue

                # Scope filtering
                if rule_paths and active_paths is not None:
                    matched = any(
                        fnmatch.fnmatch(ap, rp)
                        for ap in active_paths
                        for rp in rule_paths
                    )
                    if not matched:
                        continue

                rel = path.relative_to(rules_dir)
                fragments.append(f"# Rule: {rel}\n\n{content}")

        if not fragments:
            return None

        result = "\n\n".join(fragments)
        logger.debug("rules_loaded", count=len(fragments), chars=len(result))
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        """List session directories for this app."""
        sessions_dir = self._app_dir / "sessions"
        if not sessions_dir.exists():
            return []
        result = []
        for d in sorted(sessions_dir.iterdir()):
            if d.is_dir():
                result.append({
                    "session_id": d.name,
                    "path": str(d),
                    "has_checkpoints": (d / "checkpoints").exists(),
                })
        return result

    def cleanup_session(self, session_id: str) -> bool:
        """Remove a session's filesystem artifacts."""
        d = self.session_dir(session_id)
        if d.exists():
            import shutil
            shutil.rmtree(d)
            return True
        return False

    @staticmethod
    def resolve_workspace(
        yaml_workspace: str | None,
        source_path: Path | None,
        mode: str = "cli",
    ) -> Path:
        """Resolve the workspace path based on mode.

        CLI mode:     yaml_workspace > yaml parent > cwd
        Daemon mode:  yaml_workspace > server data dir / apps / {app_id}
        """
        if yaml_workspace:
            return Path(yaml_workspace).resolve()

        if mode == "cli":
            if source_path:
                return source_path.parent.resolve()
            return Path.cwd()
        else:
            from platformdirs import user_data_dir
            return Path(user_data_dir("digitorn")) / "workspaces"
