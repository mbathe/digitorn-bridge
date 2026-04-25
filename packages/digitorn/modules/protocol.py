"""Module layer — IModule Protocol (the public contract for all modules).

Any class that satisfies this interface is a valid Digitorn module — whether
it subclasses ``BaseModule`` or is a completely independent implementation
(e.g. an FFI bridge, a subprocess proxy, or a mock used in tests).

``@runtime_checkable`` enables ``isinstance(obj, IModule)`` checks in the
registry without forcing an inheritance chain, and also allows
``issubclass(BaseModule, IModule)`` since ``BaseModule`` explicitly inherits
from it.

Circular-import note
--------------------
``protocol.py`` *defines* the contract; ``base.py`` both imports and
*implements* it.  To avoid a circular dependency the heavy type imports
(``ExecutionContext``, ``Platform``, ``ModuleManifest``) are guarded by
``TYPE_CHECKING`` — they are resolved by static analysers and IDEs but are
**never executed at runtime**, which is fine because ``@runtime_checkable``
only checks for the *presence* of method names, not their signatures.

Usage::

    from digitorn.module.protocol import IModule

    assert isinstance(my_module, IModule)

    assert issubclass(MyConcreteModule, IModule)

    def register(module: IModule) -> None: ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from digitorn.modules.base import ExecutionContext, Platform
    from digitorn.modules.manifest import ModuleManifest


@runtime_checkable
class IModule(Protocol):
    """Minimum contract every Digitorn module must satisfy.

    ``BaseModule`` explicitly inherits from this Protocol, so all concrete
    modules automatically satisfy it.

    Implement this Protocol **directly** (without subclassing ``BaseModule``)
    only when you need full control over dispatch — e.g. subprocess proxies,
    language bridges, or mock objects in tests.

    For normal modules, subclass :class:`~digitorn.module.base.BaseModule`
    which provides the standard implementation of all these members.
    """


    MODULE_ID: str
    """Unique snake_case identifier (e.g. ``"filesystem"``)."""

    VERSION: str
    """Semantic version string (``"MAJOR.MINOR.PATCH"``, PEP-440)."""

    SUPPORTED_PLATFORMS: list["Platform"]
    """Platforms this module runs on. ``[Platform.ALL]`` for cross-platform."""


    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        context: "ExecutionContext | None" = None,
    ) -> Any:
        """Dispatch *action* with *params* and optional *context*.

        Concurrency contract (critical):
        - This method **must** be ``async``.  Callers dispatch multiple
          ``execute()`` calls concurrently via ``asyncio.gather()`` or
          :class:`~digitorn.module.executor.ModuleExecutor`.
        - I/O-bound implementations: use ``async def`` and ``await`` properly.
        - Blocking/CPU-bound implementations: declare ``execution_mode='threaded'``
          on each ``@action`` — ``BaseModule`` will wrap the handler in
          ``asyncio.to_thread()`` automatically.

        Implementations must:
        - Return any JSON-serialisable value on success.
        - Raise ``ActionExecutionError`` (or a subclass) on failure.
        - Never raise bare ``Exception``.
        """
        ...


    def get_manifest(self) -> "ModuleManifest":
        """Return the full capability manifest for this module.

        The manifest is the machine-readable contract consumed by:
        - The ``/modules`` REST API
        - The LangChain tool generator
        - The IML validator
        - The dashboard UI

        Preferred implementation (DRY)::

            def get_manifest(self) -> ModuleManifest:
                return ModuleManifest.from_module(self).model_copy(update={
                    "description": "...",
                    "author": "...",
                })
        """
        ...


    async def on_start(self) -> None:
        """Called when the module transitions to ACTIVE.

        Override to initialise connections, load models, warm caches.
        Default: no-op.
        """
        ...

    async def on_stop(self) -> None:
        """Called when the module is being disabled or the daemon shuts down.

        Override to close connections, persist state, release resources.
        Default: no-op.
        """
        ...
