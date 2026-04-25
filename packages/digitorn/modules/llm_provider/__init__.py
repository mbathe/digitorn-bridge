"""Digitorn — LLM Provider module.

Provides unified access to all major LLM providers (Anthropic, OpenAI,
DeepSeek, Ollama, Groq, Mistral, Together, etc.) through a single interface.

Named provider instances (like named database connections) can be configured
via app YAML and referenced by agents in their ``brain`` section.
"""

from digitorn.modules.llm_provider.module import LLMProviderModule

__all__ = ["LLMProviderModule"]
