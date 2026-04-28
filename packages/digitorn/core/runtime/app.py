"""RuntimeApp - the main execution engine.

Like a JVM for YAML apps: takes a CompiledApp, bootstraps it,
and runs it in the configured mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from digitorn.core.app.compiler import CompiledApp, CompiledExecution
from digitorn.core.runtime.types import AgentContext, TurnResult

logger = logging.getLogger(__name__)


@dataclass
class RuntimeApp:
    """A fully bootstrapped application ready to execute.

    Created via ``RuntimeApp.from_compiled()`` - never constructed directly.
    """

    app_id: str
    execution: CompiledExecution
    contexts: dict[str, AgentContext] = field(default_factory=dict)
    modules: dict[str, Any] = field(default_factory=dict)
    context_builder: Any = None
    hook_runner: Any = None
    approval_queue: Any = None

    def __post_init__(self) -> None:
        """Wire modules that need a reference to the RuntimeApp."""
        self._wire_channels_module()

    def _wire_channels_module(self) -> None:
        """Inject RuntimeApp + hook_runner into the channels module.

        The channels module needs these references to:
        - Access all agent contexts for routing (via self._runtime_app.contexts)
        - Pass hook_runner to agent_turn activations
        """
        channels_mod = self.modules.get("channels")
        if channels_mod is not None:
            channels_mod._runtime_app = self
            channels_mod._hook_runner = self.hook_runner
            logger.debug("channels_module_wired app=%s", self.app_id)

    @property
    def entry_context(self) -> AgentContext:
        """The entry agent's context."""
        agent_id = self.execution.entry_agent
        if agent_id not in self.contexts:
            agent_id = next(iter(self.contexts))
        return self.contexts[agent_id]

    def set_on_hook_event(self, callback: Any) -> None:
        """Set the hook event callback on the hook runner.

        This allows UI layers to receive real-time notifications
        when hook actions fire (e.g., context compaction).
        """
        if self.hook_runner is not None:
            self.hook_runner.on_hook_event = callback

    def set_on_approval_request(self, callback: Any) -> None:
        """Set callback for approval requests (CLI prompt or API event).

        Signature: async (request: ApprovalRequest) -> None
        The callback should resolve the request via queue.resolve().
        """
        if self.approval_queue is not None:
            self.approval_queue.set_on_request(callback)

    async def run_one_shot(
        self,
        user_input: str,
        *,
        on_tool_call: Any | None = None,
        on_tool_start: Any | None = None,
        on_thinking: Any | None = None,
        on_token: Any | None = None,
        on_stream_done: Any | None = None,
    ) -> TurnResult:
        """Run in one-shot mode: input → agent → output → done."""
        from digitorn.core.runtime.modes.one_shot import run_one_shot

        return await run_one_shot(
            self.entry_context,
            user_input=user_input,
            input_type=self.execution.input.type,
            output_type=self.execution.output.type,
            max_turns=self.execution.max_turns,
            timeout=self.execution.timeout,
            on_tool_call=on_tool_call,
            on_tool_start=on_tool_start,
            on_thinking=on_thinking,
            on_token=on_token,
            on_stream_done=on_stream_done,
            hook_runner=self.hook_runner,
        )

    async def run_conversation(
        self,
        *,
        on_tool_call: Any | None = None,
        on_tool_start: Any | None = None,
        on_thinking: Any | None = None,
        on_token: Any | None = None,
        on_stream_done: Any | None = None,
        on_out_token: Any | None = None,
        input_fn: Any | None = None,
        output_fn: Any | None = None,
        greeting: str | None = None,
        on_turn_result: Any | None = None,
    ) -> None:
        """Run in conversation mode: interactive loop."""
        from digitorn.core.runtime.modes.conversation import run_conversation

        await run_conversation(
            self.entry_context,
            greeting=greeting if greeting is not None else self.execution.greeting,
            max_turns=self.execution.max_turns,
            timeout=self.execution.timeout,
            on_tool_call=on_tool_call,
            on_tool_start=on_tool_start,
            on_thinking=on_thinking,
            on_token=on_token,
            on_stream_done=on_stream_done,
            on_out_token=on_out_token,
            input_fn=input_fn,
            output_fn=output_fn,
            hook_runner=self.hook_runner,
            on_turn_result=on_turn_result,
        )

    async def run_background(
        self,
        *,
        on_tool_call: Any | None = None,
        on_activation: Any | None = None,
    ) -> None:
        """Run in background mode: trigger-driven daemon.

        If the channels module is loaded, it handles all triggers.
        Otherwise, falls back to the legacy cron/watch loops.
        """
        from digitorn.core.runtime.modes.background import run_background

        await run_background(
            self.entry_context,
            triggers=self.execution.triggers,
            max_turns=self.execution.max_turns,
            timeout=self.execution.timeout,
            on_tool_call=on_tool_call,
            on_activation=on_activation,
            hook_runner=self.hook_runner,
            runtime_app=self,
            app_id=self.app_id,
            max_concurrent_activations=self.execution.max_concurrent_activations,
        )

    async def run(
        self,
        user_input: str = "",
        **kwargs: Any,
    ) -> TurnResult | None:
        """Run the app in its configured mode.

        For one_shot: returns TurnResult.
        For conversation/background: runs until interrupted, returns None.
        """
        mode = self.execution.mode

        if mode == "one_shot":
            return await self.run_one_shot(user_input, **kwargs)
        elif mode == "conversation":
            await self.run_conversation(**kwargs)
            return None
        elif mode == "background":
            await self.run_background(**kwargs)
            return None
        else:
            raise ValueError(f"Unknown execution mode: {mode}")

    async def shutdown(self) -> None:
        """Gracefully shut down all per-app module instances."""
        if self.approval_queue is not None:
            cancelled = self.approval_queue.cancel_all()
            if cancelled:
                logger.info("Cancelled %d pending approval(s) on shutdown", cancelled)

        for module_id, module in self.modules.items():
            try:
                await module.on_stop()
            except Exception as exc:
                logger.warning("Module '%s' on_stop failed: %s", module_id, exc)

        if self.context_builder:
            try:
                await self.context_builder.on_stop()
            except Exception:
                logger.debug("context_builder on_stop failed", exc_info=True)
