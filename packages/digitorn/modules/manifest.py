"""Module layer - Capability Manifest.

A ModuleManifest is the machine-readable contract between a module and the
rest of the system (LangChain SDK generator, /modules API, IML validator).

It describes:
  - Module identity and version
  - Supported platforms
  - Available actions with their param schemas
  - Required permission level
  - Usage examples for LLM few-shot prompting
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParamSpec:
    """Description of a single action parameter."""

    name: str
    type: str  # JSON Schema type: string, integer, number, boolean, object, array
    description: str
    required: bool = True
    default: Any = None
    enum: list[Any] | None = None
    example: Any = None


@dataclass
class ActionSpec:
    """Description of a single action exposed by a module."""

    name: str
    description: str
    params: list[ParamSpec] = field(default_factory=list)
    returns: str = "object"
    returns_description: str = ""
    examples: list[dict[str, Any]] = field(default_factory=list)
    permission_required: str = "local_worker"
    platforms: list[str] = field(default_factory=lambda: ["all"])
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    risk_level: str = ""
    irreversible: bool = False
    require_approval: bool = False
    data_classification: str = ""
    streams_progress: bool = False
    input_schema: dict[str, Any] | None = field(
        default=None,
        metadata={
            "description": (
                "Full JSONSchema for the action parameters, auto-generated "
                "from the Pydantic params_model.  When set, to_json_schema() "
                "returns this instead of the hand-built ParamSpec list."
            )
        },
    )
    output_schema: dict[str, Any] | None = field(
        default=None,
        metadata={"description": "Full JSONSchema for the return value. None means untyped."},
    )
    tool_prompt: str = field(
        default="",
        metadata={
            "description": (
                "Detailed usage instructions for the LLM agent. "
                "Injected into the system prompt (not the tool schema). "
                "Include: when to use, how to use, examples, common mistakes."
            )
        },
    )
    side_effects: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "Declared side effects: 'filesystem_write', 'filesystem_delete', "
                "'network_request', 'state_mutation', 'process_spawn', "
                "'screen_interaction', 'notification', 'external_api'."
            )
        },
    )
    execution_mode: str = field(
        default="async",
        metadata={"description": "Execution mode: 'sync', 'async', 'background', 'scheduled'."},
    )
    internal: bool = field(
        default=False,
        metadata={
            "description": (
                "When True, this action is hidden from LLM agents - it does NOT "
                "appear in the tool list, the discovery search index, or any "
                "agent-facing surface. It remains fully callable via "
                "BaseModule.execute() / bus.call() so other modules can use it "
                "as an internal API. Use this for infrastructure actions that "
                "are subsumed by a higher-level wrapper (e.g. database.fetch_results "
                "is internal because the LLM should call database.sql instead, "
                "but the RAG module still needs to call fetch_results directly)."
            )
        },
    )
    cli_label: str = ""
    cli_param: str = ""
    display_verb: str = field(
        default="",
        metadata={"description": "Short verb shown as the tool bubble label ('Write', 'Create task'). Never contains the technical tool name."},
    )
    display_detail_param: str = field(
        default="",
        metadata={"description": "Name of the param field used as the secondary detail line. Defaults to cli_param when empty."},
    )
    display_icon: str = field(
        default="",
        metadata={"description": "Semantic icon id - 'file', 'folder', 'checklist', 'memory', 'terminal', 'search', 'agent', 'web', 'database', 'git'. The client maps this to a concrete icon."},
    )
    display_channel: str = field(
        default="",
        metadata={"description": "Routing channel - 'chat' (default), 'tasks', 'memory', 'agents', 'workspace', 'terminal', 'diagnostics', 'none'. Non-'chat' channels are routed to the matching side panel and the tool call is NOT rendered in chat."},
    )
    display_hidden: bool = field(
        default=False,
        metadata={"description": "When True, the client does NOT render the tool call as a chat bubble even if display_channel is 'chat'. Use for plumbing tools kept in history but never shown to the user."},
    )
    display_category: str = field(
        default="",
        metadata={"description": "'action' (visible effect), 'plumbing' (internal scaffolding), 'memory', 'control_flow'."},
    )
    display_group: str = field(
        default="",
        metadata={"description": "Visual grouping id - adjacent tool calls sharing the same group are collapsed into a single card in the chat bubble."},
    )
    cli_show_output: bool = field(
        default=False,
        metadata={
            "description": (
                "When True, the CLI displays a preview of the action's output "
                "(stdout, result data) as sub-results under the tool call dot. "
                "Useful for shell commands, queries, fetches - any action where "
                "the user benefits from seeing a snippet of the result."
            )
        },
    )
    cli_output_lines: int = field(
        default=5,
        metadata={"description": "Max lines of output preview shown in CLI when cli_show_output is True."},
    )
    aliases: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "Search aliases - alternative names, verbs, and translations "
                "that should match this action in tool discovery.  Each module "
                "declares its own vocabulary so the context_builder can index "
                "them for keyword and semantic search.  "
                "Example: ['lire', 'afficher', 'cat', 'voir', 'contenu']"
            )
        },
    )
    capabilities: list["Capability"] = field(
        default_factory=list,
        metadata={"description": "Structured capabilities with scope and constraints."},
    )

    def to_json_schema(self) -> dict[str, Any]:
        """Generate a JSONSchema dict for the params of this action.

        When ``input_schema`` is set (auto-generated from a Pydantic
        ``params_model``), it is returned directly - richer and more
        accurate than hand-built ``ParamSpec`` lists.
        """
        if self.input_schema is not None:
            return self.input_schema

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.params:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum is not None:
                prop["enum"] = param.enum
            if param.example is not None:
                prop["examples"] = [param.example]
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def to_langchain_tool_schema(self) -> dict[str, Any]:
        """Generate a LangChain-compatible tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.to_json_schema(),
        }


@dataclass
class Capability:
    """Structured permission/capability with scope and constraints.

    Extends plain string permissions with rich metadata for fine-grained
    access control.  Plain strings are still supported - ``Capability``
    adds scope and constraints for modules that need them.

    Examples::

        Capability("filesystem.write", scope="sandbox_only")
        Capability("network.send", constraints={"allowed_hosts": ["api.example.com"]})
        Capability("database.write", scope="schema_only", constraints={"tables": ["users"]})
    """

    permission: str
    scope: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"permission": self.permission}
        if self.scope:
            d["scope"] = self.scope
        if self.constraints:
            d["constraints"] = self.constraints
        return d

    @classmethod
    def from_string(cls, permission: str) -> "Capability":
        """Create a Capability from a plain permission string."""
        return cls(permission=permission)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Capability":
        """Create a Capability from a dict."""
        return cls(
            permission=data["permission"],
            scope=data.get("scope", ""),
            constraints=data.get("constraints", {}),
        )


@dataclass
class ConstraintSpec:
    """Declaration of a constraint a module supports.

    Modules declare which constraints they understand so the runtime knows
    what to pass and the YAML compiler can validate app definitions.

    Scope:
      - ``"universal"`` - enforced by the runtime *before* calling the module
        (e.g. ``paths``, ``rate_limit_per_minute``).
      - ``"module"`` - enforced by the module itself (e.g. ``max_file_size``).

    Example YAML (app definition)::

        - module: filesystem
          constraints:
            paths: ["{{workspace}}"]
            max_file_size: "50MB"
    """

    name: str
    type: str
    description: str
    scope: str = "module"
    default: Any = None
    applies_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "scope": self.scope,
        }
        if self.default is not None:
            d["default"] = self.default
        if self.applies_to:
            d["applies_to"] = self.applies_to
        return d


@dataclass
class ServiceDescriptor:
    """Description of a service provided by a module.

    Used in the manifest to declare what services a module exposes
    on the ServiceBus for inter-module communication.
    """

    name: str
    methods: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ResourceLimits:
    """Resource budget for a module."""

    max_cpu_percent: float = 100.0
    max_memory_mb: int = 0
    max_execution_seconds: float = 0.0
    max_concurrent_actions: int = 0


@dataclass
class ModuleSignature:
    """Ed25519 cryptographic signature of a module package."""

    public_key_fingerprint: str
    signature_hex: str
    signed_hash: str
    signed_at: str = ""


@dataclass
class ModuleManifest:
    """Complete capability manifest for a module.

    The ``declared_permissions`` field is part of the plugin security model.
    Modules declare the OS-level or system capabilities they require, so the
    PermissionGuard can enforce them at the platform level before loading.

    Standard permission strings:
      - ``"filesystem_read"``   - read access to the filesystem
      - ``"filesystem_write"``  - write/delete access to the filesystem
      - ``"process_execute"``   - ability to spawn subprocesses
      - ``"process_kill"``      - ability to terminate processes
      - ``"network_access"``    - outbound HTTP/HTTPS connections
      - ``"screen_capture"``    - screenshot / display access
      - ``"gpio_access"``       - GPIO hardware access (IoT/Raspberry Pi)
      - ``"database_access"``   - database read/write
      - ``"display_automation"``- GUI automation (keyboard/mouse injection)
      - ``"browser_control"``   - headless browser automation

    Community modules may declare custom strings prefixed with their module_id,
    e.g. ``"mymodule:cloud_sync"``.
    """

    module_id: str
    version: str
    description: str
    author: str = ""
    homepage: str = ""
    platforms: list[str] = field(default_factory=lambda: ["all"])
    actions: list[ActionSpec] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    declared_permissions: list[str] = field(
        default_factory=list,
        metadata={
            "description": (
                "OS-level capabilities required by this module.  "
                "Declaring permissions enables the PermissionGuard to enforce "
                "them before loading and gives users visibility into what a "
                "module can do before installing it."
            )
        },
    )
    module_type: str = "user"
    provides_services: list[ServiceDescriptor] = field(default_factory=list)
    consumes_services: list[str] = field(default_factory=list)
    emits_events: list[str] = field(default_factory=list)
    subscribes_events: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    resource_limits: ResourceLimits | None = None
    sandbox_level: str = "none"
    license: str = ""
    optional_dependencies: list[str] = field(default_factory=list)
    module_dependencies: dict[str, str] = field(
        default_factory=dict,
        metadata={"description": "Module-to-module deps with PEP-440 version specifiers."},
    )
    signing: ModuleSignature | None = None
    declared_capabilities: list[Capability] = field(
        default_factory=list,
        metadata={
            "description": (
                "Structured capabilities with scope and constraints.  "
                "Extends declared_permissions with fine-grained control."
            )
        },
    )
    supported_constraints: list[ConstraintSpec] = field(
        default_factory=list,
        metadata={
            "description": (
                "Constraints this module supports.  Apps set values in YAML; "
                "the runtime resolves templates and passes them to the module."
            )
        },
    )


    @classmethod
    def from_module(cls, module: "Any") -> "ModuleManifest":
        """Build a ``ModuleManifest`` from a ``BaseModule`` instance.

        **DRY entry point** - instead of manually listing every action's
        ``ActionSpec`` in ``get_manifest()``, use this factory and only
        override metadata (description, author, tags) via ``model_copy()``:

        .. code-block:: python

            def get_manifest(self) -> ModuleManifest:
                return ModuleManifest.from_module(self).model_copy(update={
                    "description": "File and directory operations",
                    "author": "LLMOS Core Team",
                    "tags": ["core", "io"],
                })

        Priority order for action discovery:
        1.  ``_action_registry`` attribute (populated by ``@action`` decorator)
        2.  ``_dynamic_specs`` (runtime-registered specs)
        3.  ``_action_*`` method scanning (fallback for undecorated modules)
        """
        registry: dict[str, Any] = getattr(module, "_action_registry", {})
        if registry:
            actions = []
            for entry in registry.values():
                if hasattr(entry, "spec"):
                    actions.append(entry.spec)
                elif isinstance(entry, dict):
                    actions.append(entry["spec"])
        else:
            actions = []
            dynamic_specs: dict[str, "ActionSpec"] = getattr(
                module, "_dynamic_specs", {}
            )
            for name, spec in dynamic_specs.items():
                actions.append(spec)

            scanned_names = set(dynamic_specs.keys())
            for attr_name in sorted(dir(module)):
                if not attr_name.startswith("_action_"):
                    continue
                action_name = attr_name.removeprefix("_action_")
                if action_name in scanned_names:
                    continue
                handler = getattr(module, attr_name, None)
                if handler is None or not callable(handler):
                    continue
                embedded_spec: "ActionSpec | None" = getattr(
                    handler, "_action_spec", None
                )
                if embedded_spec is not None:
                    actions.append(embedded_spec)
                else:
                    actions.append(ActionSpec(name=action_name, description=""))

        module_constraints = getattr(module, "CONSTRAINTS", [])

        return cls(
            module_id=module.MODULE_ID,
            version=module.VERSION,
            description="",
            module_type=getattr(module, "MODULE_TYPE", "user"),
            platforms=[str(p) for p in getattr(module, "SUPPORTED_PLATFORMS", ["all"])],
            actions=actions,
            supported_constraints=module_constraints,
        )

    def model_copy(self, *, update: "dict[str, Any] | None" = None) -> "ModuleManifest":
        """Return a shallow copy of this manifest with optional field overrides.

        Mirrors the Pydantic v2 ``model_copy(update={...})`` API so the
        doc examples work regardless of whether manifests are dataclasses
        or Pydantic models.
        """
        import dataclasses as _dc
        copy = _dc.replace(self)
        if update:
            for k, v in update.items():
                object.__setattr__(copy, k, v) if _dc.is_dataclass(copy) else setattr(copy, k, v)
        return copy

    def get_action(self, action_name: str) -> ActionSpec | None:
        for action in self.actions:
            if action.name == action_name:
                return action
        return None

    def action_names(self) -> list[str]:
        return [a.name for a in self.actions]

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "module_id": self.module_id,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "platforms": self.platforms,
            "dependencies": self.dependencies,
            "tags": self.tags,
            "declared_permissions": self.declared_permissions,
            "actions": [self._action_to_dict(a) for a in self.actions],
        }
        if self.module_type != "user":
            result["module_type"] = self.module_type
        if self.provides_services:
            result["provides_services"] = [
                {"name": s.name, "methods": s.methods, "description": s.description}
                for s in self.provides_services
            ]
        if self.consumes_services:
            result["consumes_services"] = self.consumes_services
        if self.emits_events:
            result["emits_events"] = self.emits_events
        if self.subscribes_events:
            result["subscribes_events"] = self.subscribes_events
        if self.config_schema is not None:
            result["config_schema"] = self.config_schema
        if self.resource_limits is not None:
            result["resource_limits"] = {
                "max_cpu_percent": self.resource_limits.max_cpu_percent,
                "max_memory_mb": self.resource_limits.max_memory_mb,
                "max_execution_seconds": self.resource_limits.max_execution_seconds,
                "max_concurrent_actions": self.resource_limits.max_concurrent_actions,
            }
        if self.sandbox_level != "none":
            result["sandbox_level"] = self.sandbox_level
        if self.license:
            result["license"] = self.license
        if self.optional_dependencies:
            result["optional_dependencies"] = self.optional_dependencies
        if self.module_dependencies:
            result["module_dependencies"] = self.module_dependencies
        if self.signing is not None:
            result["signing"] = {
                "public_key_fingerprint": self.signing.public_key_fingerprint,
                "signature_hex": self.signing.signature_hex,
                "signed_hash": self.signing.signed_hash,
                "signed_at": self.signing.signed_at,
            }
        if self.declared_capabilities:
            result["declared_capabilities"] = [
                c.to_dict() for c in self.declared_capabilities
            ]
        if self.supported_constraints:
            result["supported_constraints"] = [
                c.to_dict() for c in self.supported_constraints
            ]
        return result

    @staticmethod
    def _action_to_dict(a: ActionSpec) -> dict[str, Any]:
        """Serialise an ActionSpec to a dict, including v3 fields when non-default."""
        d: dict[str, Any] = {
            "name": a.name,
            "description": a.description,
            "params_schema": a.to_json_schema(),
            "returns": a.returns,
            "returns_description": a.returns_description,
            "permission_required": a.permission_required,
            "platforms": a.platforms,
            "examples": a.examples,
            "tags": a.tags,
            "permissions": a.permissions,
            "risk_level": a.risk_level,
            "irreversible": a.irreversible,
            "data_classification": a.data_classification,
        }
        if a.output_schema is not None:
            d["output_schema"] = a.output_schema
        if a.side_effects:
            d["side_effects"] = a.side_effects
        if a.execution_mode != "async":
            d["execution_mode"] = a.execution_mode
        if a.capabilities:
            d["capabilities"] = [c.to_dict() for c in a.capabilities]
        if a.streams_progress:
            d["streams_progress"] = True
        return d
