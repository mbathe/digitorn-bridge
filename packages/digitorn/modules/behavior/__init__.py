"""Behavior Module - runtime behavioral enforcement engine."""

try:
    from digitorn.modules.behavior.module import BehaviorModule
except ImportError:
    from digitorn.modules.behavior.module import BhvModule as BehaviorModule  # noqa: F401

__all__ = ["BehaviorModule"]
