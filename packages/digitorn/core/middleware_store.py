"""Middleware Store — install, discover, and manage middleware packages.

Middlewares follow the same pattern as modules:
- Discovered via ``digitorn-middleware.toml`` descriptor files
- Stored in known directories (builtin + user)
- Registered in the database for the GUI to list available middlewares
- Referenced by name in YAML (never by file path)

Lifecycle:  create/install → register in DB → use in app YAML

Directory structure::

    ~/.local/share/digitorn/middleware/
    └── french_only/
        ├── digitorn-middleware.toml
        └── middleware.py
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digitorn.core.paths import all_middleware_dirs, builtin_middleware_dir, user_middleware_dir

logger = logging.getLogger(__name__)


@dataclass
class MiddlewareDescriptor:
    """Parsed from ``digitorn-middleware.toml``."""

    middleware_id: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    level: str = "app"
    class_path: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    enabled: bool = True

    source_dir: Path | None = None
    source: str = "builtin"


class MiddlewareRegistry:
    """Singleton registry for all discovered middleware packages.

    Discovers middlewares from disk (TOML descriptors), loads their
    classes on demand, and provides lookup by name.
    """

    def __init__(self) -> None:
        self._descriptors: dict[str, MiddlewareDescriptor] = {}
        self._classes: dict[str, type] = {}

    def discover(self) -> int:
        """Scan all middleware directories and register descriptors.

        Returns the number of middlewares discovered.
        """
        self._descriptors.clear()
        self._classes.clear()

        for mw_dir in all_middleware_dirs():
            if not mw_dir.exists():
                continue
            source = "builtin" if mw_dir == builtin_middleware_dir() else "user"
            for child in sorted(mw_dir.iterdir()):
                if not child.is_dir():
                    continue
                toml_path = child / "digitorn-middleware.toml"
                if not toml_path.exists():
                    continue
                desc = _parse_toml(toml_path, source)
                if desc is not None:
                    self._descriptors[desc.middleware_id] = desc

        logger.info(
            "middleware_registry discovered=%d ids=%s",
            len(self._descriptors),
            list(self._descriptors.keys()),
        )
        return len(self._descriptors)

    def get_descriptor(self, middleware_id: str) -> MiddlewareDescriptor | None:
        return self._descriptors.get(middleware_id)

    def list_all(self) -> list[MiddlewareDescriptor]:
        return list(self._descriptors.values())

    def list_by_level(self, level: str) -> list[MiddlewareDescriptor]:
        return [
            d for d in self._descriptors.values()
            if d.level == level or d.level == "all"
        ]

    def instantiate(
        self, middleware_id: str, config: dict[str, Any] | None = None,
    ) -> Any | None:
        """Load and instantiate a middleware by ID.

        Returns the middleware instance, or None if not found.
        """
        desc = self._descriptors.get(middleware_id)
        if desc is None:
            logger.warning("middleware_not_found: %s", middleware_id)
            return None

        if not desc.enabled:
            logger.warning("middleware_disabled: %s", middleware_id)
            return None

        cls = self._resolve_class(desc)
        if cls is None:
            return None

        try:
            instance = cls(**(config or {}))
            logger.debug("middleware_instantiated: %s", middleware_id)
            return instance
        except Exception as exc:
            logger.warning(
                "middleware_instantiate_error id=%s: %s", middleware_id, exc,
            )
            return None

    def _resolve_class(self, desc: MiddlewareDescriptor) -> type | None:
        """Resolve the middleware class from the descriptor."""
        if desc.middleware_id in self._classes:
            return self._classes[desc.middleware_id]

        if not desc.class_path or desc.source_dir is None:
            logger.warning(
                "middleware_no_class_path: %s", desc.middleware_id,
            )
            return None

        if ":" not in desc.class_path:
            logger.warning(
                "middleware_invalid_class_path: %s (expected file:Class)",
                desc.class_path,
            )
            return None

        file_part, class_name = desc.class_path.rsplit(":", 1)
        if not file_part.endswith(".py"):
            file_part += ".py"

        py_path = (desc.source_dir / file_part).resolve()
        try:
            py_path.relative_to(desc.source_dir.resolve())
        except ValueError:
            logger.warning(
                "middleware_path_traversal_blocked: %s resolves outside source_dir (%s)",
                desc.middleware_id, py_path,
            )
            return None

        if not py_path.exists():
            logger.warning(
                "middleware_file_not_found: %s (%s)", desc.middleware_id, py_path,
            )
            return None

        try:
            spec = importlib.util.spec_from_file_location(
                f"digitorn_middleware_{desc.middleware_id}", str(py_path),
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load from {py_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls = getattr(module, class_name, None)
            if cls is None:
                logger.warning(
                    "middleware_class_not_found: %s in %s",
                    class_name, py_path,
                )
                return None
            self._classes[desc.middleware_id] = cls
            return cls
        except Exception as exc:
            logger.warning(
                "middleware_load_error id=%s: %s", desc.middleware_id, exc,
            )
            return None


_registry = MiddlewareRegistry()


def get_middleware_registry() -> MiddlewareRegistry:
    return _registry


def _parse_toml(path: Path, source: str) -> MiddlewareDescriptor | None:
    """Parse a ``digitorn-middleware.toml`` file."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        logger.warning("middleware_toml_parse_error: %s: %s", path, exc)
        return None

    mw = data.get("middleware", {})
    middleware_id = mw.get("middleware_id", "")
    if not middleware_id:
        logger.warning("middleware_toml_no_id: %s", path)
        return None

    return MiddlewareDescriptor(
        middleware_id=middleware_id,
        version=mw.get("version", "1.0.0"),
        description=mw.get("description", ""),
        author=mw.get("author", ""),
        level=mw.get("level", "app"),
        class_path=mw.get("class_path", ""),
        config_schema=mw.get("config_schema", {}),
        tags=mw.get("tags", []),
        enabled=mw.get("enabled", True),
        source_dir=path.parent,
        source=source,
    )


def install_middleware(source_path: str | Path) -> MiddlewareDescriptor:
    """Install a middleware from a local directory.

    Copies the middleware directory to the user middleware dir.
    Validates the TOML descriptor before installing.

    Raises ValueError if validation fails.
    """
    src = Path(source_path).resolve()
    if not src.is_dir():
        raise ValueError(f"Source must be a directory: {src}")

    toml_path = src / "digitorn-middleware.toml"
    if not toml_path.exists():
        raise ValueError(f"No digitorn-middleware.toml found in {src}")

    desc = _parse_toml(toml_path, "user")
    if desc is None:
        raise ValueError(f"Invalid TOML in {toml_path}")

    if not desc.class_path:
        raise ValueError(f"middleware.class_path is required in {toml_path}")

    if ":" not in desc.class_path:
        raise ValueError(
            f"class_path must be 'file:ClassName', got '{desc.class_path}'",
        )
    file_part = desc.class_path.rsplit(":", 1)[0]
    if not file_part.endswith(".py"):
        file_part += ".py"
    if not (src / file_part).exists():
        raise ValueError(f"Class file not found: {src / file_part}")

    dest = user_middleware_dir() / desc.middleware_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    desc.source_dir = dest
    desc.source = "user"

    logger.info(
        "middleware_installed id=%s dest=%s", desc.middleware_id, dest,
    )
    return desc


def uninstall_middleware(middleware_id: str) -> bool:
    """Uninstall a user-installed middleware.

    Returns True if removed, False if not found.
    Raises ValueError if trying to uninstall a builtin.
    """
    dest = user_middleware_dir() / middleware_id
    if not dest.exists():
        return False

    builtin = builtin_middleware_dir() / middleware_id
    if builtin.exists() and dest == builtin:
        raise ValueError(f"Cannot uninstall builtin middleware: {middleware_id}")

    shutil.rmtree(dest)
    logger.info("middleware_uninstalled id=%s", middleware_id)
    return True


def create_middleware_scaffold(
    middleware_id: str,
    level: str = "app",
    output_dir: str | Path | None = None,
) -> Path:
    """Generate a middleware project scaffold.

    Creates a ready-to-use middleware directory with TOML + Python file.

    Returns the path to the created directory.
    """
    if output_dir is None:
        output_dir = Path.cwd()
    dest = Path(output_dir) / middleware_id
    dest.mkdir(parents=True, exist_ok=True)

    toml_content = f'''[middleware]
middleware_id = "{middleware_id}"
version = "1.0.0"
description = "Custom {level}-level middleware"
author = ""
level = "{level}"
class_path = "middleware:{_to_class_name(middleware_id)}"
tags = ["custom"]
enabled = true

[middleware.config_schema]
'''
    (dest / "digitorn-middleware.toml").write_text(toml_content)

    if level == "app":
        py_content = f'''"""Custom app-level middleware: {middleware_id}

App-level middlewares intercept before/after each LLM call.
They can modify the system prompt, user messages, or short-circuit
the agent loop entirely.
"""


class {_to_class_name(middleware_id)}:
    """TODO: Add your middleware description here."""

    def __init__(self, **kwargs):
        self.config = kwargs

    async def before(self, ctx):
        """Called before each LLM call.

        Args:
            ctx: AppMiddlewareContext with:
                - ctx.system_prompt (str, mutable)
                - ctx.messages (list[dict], mutable)
                - ctx.agent_id (str)
                - ctx.turn (int)
                - ctx.metadata (dict, scratch space)

        Returns:
            None to continue normally, or a string to short-circuit
            (the string becomes the agent's response, no LLM call).
        """


        return None

    async def after(self, ctx, response, tool_calls):
        """Called after the LLM response.

        Args:
            ctx: Same AppMiddlewareContext as before()
            response: The LLM's text response (str)
            tool_calls: List of tool calls from the LLM

        Returns:
            The (possibly modified) response string.
        """
        return response
'''
    elif level == "module":
        py_content = f'''"""Custom module-level middleware: {middleware_id}

Module-level middlewares wrap individual module action executions
(filesystem.read, database.query, etc.).
"""


class {_to_class_name(middleware_id)}:
    """TODO: Add your middleware description here."""

    def __init__(self, **kwargs):
        self.config = kwargs

    async def __call__(self, ctx, next_):
        """Wrap a module action execution.

        Args:
            ctx: ModuleCallContext with:
                - ctx.module_id (str)
                - ctx.action (str)
                - ctx.params (dict, mutable)
                - ctx.attempt (int, set by retry middleware)
                - ctx.metadata (dict, scratch space)
            next_: async callable — invoke to run the next middleware
                   or the actual module handler.

        Returns:
            The action result (pass through from next_()).
        """

        result = await next_(ctx)

        return result
'''
    else:
        py_content = f'''"""Custom MCP-level middleware: {middleware_id}

MCP-level middlewares wrap raw MCP tool calls to external servers.
"""


class {_to_class_name(middleware_id)}:
    """TODO: Add your middleware description here."""

    def __init__(self, **kwargs):
        self.config = kwargs

    async def __call__(self, ctx, next_):
        """Wrap an MCP tool call.

        Args:
            ctx: MCPCallContext with:
                - ctx.server_id (str)
                - ctx.tool_name (str)
                - ctx.params (dict, mutable)
                - ctx.attempt (int)
                - ctx.metadata (dict, scratch space)
            next_: async callable — invoke to call the MCP server.

        Returns:
            The raw MCP result.
        """
        result = await next_(ctx)
        return result
'''

    (dest / "middleware.py").write_text(py_content)

    logger.info("middleware_scaffold_created id=%s dest=%s", middleware_id, dest)
    return dest


def _to_class_name(middleware_id: str) -> str:
    """Convert middleware_id to PascalCase class name."""
    return "".join(
        part.capitalize() for part in middleware_id.replace("-", "_").split("_")
    ) + "Middleware"


def _build_db_model():
    """Build the SQLAlchemy model (deferred to avoid import cycles)."""
    from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
    from sqlalchemy.orm import Mapped, mapped_column
    from digitorn.core.database import Base

    class InstalledMiddleware(Base):
        """Registry of installed middleware packages.

        Mirrors the TOML descriptor in the database so the GUI can
        list available middlewares without scanning the filesystem.
        """

        __tablename__ = "installed_middleware"

        id: Mapped[str] = mapped_column(String(64), primary_key=True)
        middleware_id: Mapped[str] = mapped_column(
            String(255), unique=True, nullable=False, index=True,
        )
        version: Mapped[str] = mapped_column(String(64), nullable=False, default="1.0.0")
        description: Mapped[str | None] = mapped_column(Text, nullable=True)
        author: Mapped[str] = mapped_column(String(255), nullable=False, default="")
        level: Mapped[str] = mapped_column(String(32), nullable=False, default="app")
        config_schema: Mapped[dict] = mapped_column(JSON, default=dict)
        tags: Mapped[list] = mapped_column(JSON, default=list)
        source: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
        enabled: Mapped[bool] = mapped_column(Boolean, default=True)
        installed_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        )

    return InstalledMiddleware


InstalledMiddleware = None


def get_installed_middleware_model():
    """Get or create the InstalledMiddleware model."""
    global InstalledMiddleware
    if InstalledMiddleware is None:
        InstalledMiddleware = _build_db_model()
    return InstalledMiddleware


async def sync_middleware_to_db(session: Any) -> int:
    """Sync discovered middlewares to the database.

    Upserts all discovered descriptors so the GUI can query them.
    Returns the number of synced records.
    """
    import uuid
    from sqlalchemy import select

    Model = get_installed_middleware_model()
    registry = get_middleware_registry()

    count = 0
    for desc in registry.list_all():
        stmt = select(Model).where(Model.middleware_id == desc.middleware_id)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.version = desc.version
            existing.description = desc.description
            existing.author = desc.author
            existing.level = desc.level
            existing.config_schema = desc.config_schema
            existing.tags = desc.tags
            existing.source = desc.source
            existing.enabled = desc.enabled
        else:
            session.add(Model(
                id=uuid.uuid4().hex,
                middleware_id=desc.middleware_id,
                version=desc.version,
                description=desc.description,
                author=desc.author,
                level=desc.level,
                config_schema=desc.config_schema,
                tags=desc.tags,
                source=desc.source,
                enabled=desc.enabled,
            ))
        count += 1

    await session.flush()
    return count
