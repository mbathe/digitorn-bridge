"""Behavior Module - runtime behavioral enforcement + semantic classification.

Two enforcement layers:
  1. **Rule engine** - checks every tool call pre/post, detects violations,
     injects warnings/blocks/reminders into the conversation.
  2. **Semantic classifier** (optional) - a configurable LLM analyzes the
     user's message BEFORE the main agent acts, classifies the task, and
     injects behavioral directives.

The classifier is fully data-driven: complexity levels, approaches, risk
levels, system prompt, directive format, frequency, context inclusion -
all configurable in YAML via ``behavior.classifier``.

Integration points in agent_loop.py:
  - get_prompt_sections() → injects enforced rules into system prompt
  - classify_turn() → runs the classifier LLM before the main agent's turn
  - pre_tool_check() → called before each tool execution
  - post_tool_check() → called after each tool execution
  - on_turn_start() → called at each turn start
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from digitorn.modules.base import BaseModule
from digitorn.modules.behavior.engine import BehaviorEngine
from digitorn.modules.behavior.generic_rules import Violation as BehaviorViolation
from digitorn.modules.manifest import ModuleManifest

logger = logging.getLogger(__name__)


class BehaviorModule(BaseModule):
    """Behavioral enforcement - monitors and corrects agent behavior in real-time."""

    MODULE_ID = "behavior"
    VERSION = "3.0.0"

    def __init__(self) -> None:
        super().__init__()
        self._engine: BehaviorEngine | None = None
        self._classify_enabled: bool = False
        self._classifier_provider: Any = None
        self._classifier_config: dict[str, Any] = {}
        self._profile_name: str = ""
        # Snapshot of the YAML config so ``set_active_profile`` can
        # re-resolve rules without losing the user's overrides.
        self._original_config: dict[str, Any] = {}
        # Active profile override applied by the composer mode system.
        # Empty = use the YAML-declared ``security.behavior.profile``
        # (i.e. ``self._profile_name``).
        self._active_profile_override: str = ""

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Runtime behavioral enforcement + semantic task classification. "
                "Monitors tool calls, detects violations, and injects directives."
            ),
            "author": "Digitorn Team",
        })

    async def on_config_update(self, config: dict[str, Any]) -> None:
        await super().on_config_update(config)
        # Snapshot config so a later ``set_active_profile`` swap can
        # rebuild rule definitions with the same per-app overrides
        # (rule_definitions, custom rules, tracking config).
        self._original_config = dict(config) if isinstance(config, dict) else {}
        self._active_profile_override = ""
        self._engine = BehaviorEngine(config)
        self._classify_enabled = config.get("classify_turns", False)
        self._profile_name = config.get("profile", "")

        # Store classifier config (from Pydantic model or raw dict)
        raw_cls = config.get("classifier", {})
        if hasattr(raw_cls, "model_dump"):
            self._classifier_config = raw_cls.model_dump()
        elif isinstance(raw_cls, dict):
            self._classifier_config = raw_cls
        else:
            self._classifier_config = {}

        logger.info(
            "behavior_module_configured rules=%d classify=%s freq=%s",
            len(self._engine.rules),
            self._classify_enabled,
            self._classifier_config.get("frequency", "every_turn"),
        )

    async def on_start(self) -> None:
        if self._engine is None:
            logger.debug("behavior_module: no config, enforcement disabled")

    async def on_stop(self) -> None:
        pass

    async def cleanup_session(self, session_id: str) -> None:
        if self._engine:
            self._engine.cleanup_session(session_id)

    @property
    def engine(self) -> BehaviorEngine | None:
        return self._engine

    @property
    def active(self) -> bool:
        return self._engine is not None

    @property
    def classify_enabled(self) -> bool:
        return self._classify_enabled and self._classifier_provider is not None

    @property
    def classifier_config(self) -> dict[str, Any]:
        return self._classifier_config

    def set_classifier_provider(self, provider: Any) -> None:
        """Set the LLM provider for semantic classification."""
        self._classifier_provider = provider
        logger.info(
            "behavior_classifier_ready provider=%s model=%s",
            getattr(provider, "provider_hint", "?"),
            getattr(provider, "model", "?"),
        )

    # ── Composer mode integration ────────────────────────────────
    #
    # The mode system (``runtime.modes`` in app.yaml) can override the
    # behavior profile on a per-mode basis via ``ModeDef.behavior_profile``.
    # When the agent loop detects that the active mode declares a profile
    # different from the currently-applied one, it calls this method to
    # swap the engine's active rule set. The swap preserves the per-
    # session state (``_sessions``) so counters / sets / flags survive
    # the profile change.
    def set_active_profile(self, profile_name: str) -> None:
        """Swap the engine's active behavioural profile.

        Pass ``""`` to revert to the YAML-declared
        ``security.behavior.profile``. Idempotent: a call with the same
        profile that's already active is a no-op.

        Mutates ``self._engine._rules`` and ``self._engine._rule_defs``.
        Per-session state (counters, sets, flags, recent_tools, ...)
        stays intact across the swap so a mid-session profile change
        does not zero the agent's track record.
        """
        if self._engine is None:
            return
        target = (profile_name or "").strip()
        if target == self._active_profile_override:
            return  # already on the right profile

        from digitorn.modules.behavior.profiles import resolve_profile

        # When target is empty, fall back to the YAML profile.
        effective_profile = target or self._profile_name
        rule_overrides = self._original_config.get("rules") or {}
        new_rules = resolve_profile(effective_profile, rule_overrides)

        # Re-build rule definitions with a config copy that swaps the
        # profile field. Keeps ``rule_definitions`` + ``custom`` from
        # the YAML intact.
        cfg = dict(self._original_config)
        cfg["profile"] = effective_profile
        self._engine._rules = new_rules
        self._engine._rule_defs = self._engine._build_rule_definitions(cfg)
        self._active_profile_override = target
        logger.info(
            "behavior_profile_override target=%s effective=%s rules=%d",
            target or "<yaml-default>",
            effective_profile or "<none>",
            len(self._engine._rule_defs),
        )

    @property
    def active_profile_override(self) -> str:
        return self._active_profile_override

    # ── Prompt injection ─────────────────────────────────────────

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        """Inject enforced rules + behavior guide into the system prompt."""
        if not self._engine:
            return []

        sections: list[dict[str, Any]] = []

        # Rules (compact, always injected)
        rules_section = self._engine.build_prompt_section()
        if rules_section:
            sections.append({
                "title": "ENFORCED BEHAVIORAL RULES",
                "content": rules_section,
                "priority": 2,
            })

        # Dev profile - inject the advanced behavior guide
        profile = self._engine.rules.get("_profile_name") or ""
        if not profile:
            profile = self._profile_name
        if profile == "dev":
            from digitorn.modules.behavior.profiles import get_dev_prompt_section
            sections.append({
                "title": "DEVELOPER BEHAVIOR GUIDE",
                "content": get_dev_prompt_section(),
                "priority": 3,
            })

        # Custom profile prompt (from ./behavior/*.yaml)
        from digitorn.modules.behavior.profiles import get_custom_prompt_section
        custom_prompt = get_custom_prompt_section(self._engine.rules)
        if custom_prompt:
            display_name = self._engine.rules.get("_profile_display_name", "Custom")
            sections.append({
                "title": f"BEHAVIOR PROFILE: {display_name}",
                "content": custom_prompt,
                "priority": 4,
            })

        return sections

    # ── Semantic classification ──────────────────────────────────

    async def classify_turn(
        self,
        session_id: str,
        user_message: str,
        capabilities: list[str],
        turn: int = 0,
        recent_messages: list[dict[str, Any]] | None = None,
        *,
        tool_inventory: list[dict[str, str]] | None = None,
        workspace_context: dict[str, Any] | None = None,
        provider_override: Any = None,
    ) -> str | None:
        """Run the semantic classifier on a user message.

        Returns a formatted directive message to inject, or None.
        Called by agent_loop BEFORE the main LLM call.

        The classifier respects ``classifier.frequency``, ``classifier.skip_followups``,
        and ``classifier.timeout`` from the YAML config.

        ``provider_override`` lets the runtime hand in a per-session
        provider instance (e.g. a fresh gateway provider for the
        current user's JWT) without mutating the shared singleton.
        When None, the module-level ``_classifier_provider`` is used,
        which preserves the legacy single-tenant behaviour.
        """
        def _trace(msg: str) -> None:
            try:
                from pathlib import Path as _P
                _path = _P.home() / ".digitorn" / "logs" / "coach_trace.log"
                with open(_path, "a", encoding="utf-8") as _f:
                    _f.write(f"{msg}\n")
            except Exception:
                pass

        # Per-call provider wins over the module-level singleton -
        # this is how the BYOK / gateway resolver hands the right
        # provider for the current user without mutating shared state.
        active_provider = provider_override or self._classifier_provider

        _trace(f"--- classify_turn called session={session_id[:12]} turn={turn} msg_len={len(user_message)} enabled={self._classify_enabled} provider={type(active_provider).__name__ if active_provider else None} override={provider_override is not None}")

        if not self._classify_enabled or active_provider is None:
            _trace(f"  SKIP: enabled={self._classify_enabled} provider_none={active_provider is None}")
            return None
        if not self._engine:
            _trace("  SKIP: no engine")
            return None
        if not user_message or not user_message.strip():
            _trace("  SKIP: empty user_message")
            return None

        from digitorn.modules.behavior.classifier import (
            build_classify_messages,
            format_directive_message,
            parse_classification,
            should_run_this_turn,
        )

        cfg = self._classifier_config

        # ── Frequency check - should we even run this turn? ──
        if not should_run_this_turn(turn, cfg, user_message):
            logger.debug("behavior_classify: skipped turn=%d (frequency=%s)", turn, cfg.get("frequency", "every_turn"))
            return None

        # ── Build the full context for the classifier ──

        rules = self._engine.rules
        active_rules = [k for k, v in rules.items() if v is True]

        # Profile context
        profile_context = _build_profile_context(rules)

        # Session state snapshot (generic - state.snapshot() dumps everything)
        state = self._engine.get_session(session_id)
        session_state = state.snapshot()

        messages = build_classify_messages(
            user_message=user_message,
            capabilities=capabilities,
            active_rules=active_rules,
            recent_context=recent_messages,
            profile_context=profile_context or None,
            tool_inventory=tool_inventory,
            session_state=session_state,
            workspace_context=workspace_context,
            classifier_config=cfg,
        )

        # ── Call the classifier LLM ──
        timeout = cfg.get("timeout", 15) or 15

        try:
            from digitorn.core.runtime.messages import to_chat_messages
            chat_messages = to_chat_messages(messages)

            response = await asyncio.wait_for(
                active_provider.chat(chat_messages, tools=None),
                timeout=float(timeout),
            )

            raw_text = _extract_response_text(response)
            _trace(f"  RAW_RESPONSE len={len(raw_text)}: {raw_text[:400]}")
            if not raw_text:
                logger.debug("behavior_classify: empty response")
                return None

            classification = parse_classification(raw_text)
            _trace(f"  PARSED: {classification}")
            if classification is None:
                return None

            directive = format_directive_message(classification, classifier_config=cfg)
            _trace(f"  DIRECTIVE len={len(directive) if directive else 0}")
            if directive:
                logger.info(
                    "behavior_classify session=%s turn=%d complexity=%s approach=%s risk=%s directives=%d",
                    session_id[:12], turn,
                    classification.get("complexity", "?"),
                    classification.get("approach", "?"),
                    classification.get("risk_level", "?"),
                    len(classification.get("directives", [])),
                )
            return directive

        except asyncio.TimeoutError:
            _trace(f"  TIMEOUT >{timeout}s")
            logger.warning("behavior_classify: timeout (>%ds), skipping", timeout)
            return None
        except Exception as exc:
            _trace(f"  EXCEPTION: {type(exc).__name__}: {exc}")
            logger.warning("behavior_classify: error: %s", exc)
            return None

    # ── Runtime hooks (called by agent_loop) ─────────────────────

    def on_turn_start(self, session_id: str) -> None:
        """Called at the start of each agent turn."""
        if self._engine:
            self._engine.on_turn_start(session_id)

    def check_agent_text(self, session_id: str, text: str) -> list[str]:
        """Check rules on the agent's text output."""
        if not self._engine:
            return []
        violations = self._engine.on_agent_text(session_id, text)
        return [v.format() for v in violations]

    def pre_tool_check(
        self,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        agent_text: str = "",
    ) -> tuple[bool, list[str]]:
        """Check rules BEFORE a tool executes."""
        def _ptrace(msg: str) -> None:
            try:
                from pathlib import Path as _P
                _path = _P.home() / ".digitorn" / "logs" / "pretool_trace.log"
                with open(_path, "a", encoding="utf-8") as _f:
                    _f.write(f"{msg}\n")
            except Exception:
                pass
        _rule_count = len(self._engine.rule_definitions) if self._engine else 0
        _cmd_preview = str(params.get("command", ""))[:80]
        _ptrace(f"pre_tool session={session_id[:12]} tool={tool_name} rules_loaded={_rule_count} cmd={_cmd_preview}")
        if not self._engine:
            _ptrace("  NO ENGINE - skipping")
            return True, []
        violations = self._engine.pre_tool(session_id, tool_name, params, agent_text)
        _ptrace(f"  violations={[(v.rule_id, v.level) for v in violations]}")
        if not violations:
            return True, []
        messages = [v.format() for v in violations]
        blocked = any(v.level == "block" for v in violations)
        return not blocked, messages

    def post_tool_check(
        self,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        result: Any,
    ) -> list[str]:
        """Check rules AFTER a tool executes + update state."""
        if not self._engine:
            return []
        reminders = self._engine.post_tool(session_id, tool_name, params, result)
        return [v.format() for v in reminders]


# ── Helpers ──────────────────────────────────────────────────────


def _build_profile_context(rules: dict) -> dict[str, Any]:
    """Extract custom profile metadata from the merged rules dict."""
    ctx: dict[str, Any] = {}
    pname = rules.get("_profile_display_name") or rules.get("_profile_name") or ""
    if pname:
        ctx["profile_name"] = pname
    pdesc = rules.get("_profile_description", "")
    if pdesc:
        ctx["profile_description"] = pdesc
    cprompt = rules.get("_custom_prompt", "")
    if cprompt:
        ctx["custom_prompt"] = cprompt
    custom_rules = rules.get("custom", [])
    if custom_rules and isinstance(custom_rules, list):
        ctx["custom_rules"] = custom_rules
    return ctx



def _extract_response_text(response: Any) -> str:
    """Extract text from various LLM provider response formats."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        text = str(response.get("content", "") or "")
        if not text and "choices" in response:
            choices = response["choices"]
            if choices and isinstance(choices[0], dict):
                text = choices[0].get("message", {}).get("content", "")
        return text
    if hasattr(response, "choices") and response.choices:
        msg = response.choices[0].message
        return getattr(msg, "content", "") or ""
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "".join(parts)
    return ""


# Backward-compat alias
BhvModule = BehaviorModule  # noqa: F841
