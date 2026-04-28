"""Behavior Module - runtime behavioral enforcement engine.

Monitors every tool call, detects rule violations, and signals
the agent immediately. Rules are defined in the app YAML and
enforced in real-time - not just suggested in prompts.
"""

try:
    from digitorn.modules.behavior.module import BehaviorModule
except ImportError:
    from digitorn.modules.behavior.module import BhvModule as BehaviorModule  # noqa: F401

__all__ = ["BehaviorModule"]
