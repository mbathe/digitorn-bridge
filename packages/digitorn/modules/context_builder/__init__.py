"""context_builder - Tool Discovery Engine for agents.

Instead of injecting thousands of tools into an agent's context,
this module exposes 5 meta-tools that let the agent discover,
search, and execute any tool in O(1).
"""

from digitorn.modules.context_builder.module import ContextBuilderModule

__all__ = ["ContextBuilderModule"]
