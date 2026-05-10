"""Stabilization test suite -- real-LLM scenarios via Ollama.

Goal: catch every bug, stall, leak, or crash that could affect a
production daemon. Tests run against a SPAWN-isolated daemon configured
with the user's local Ollama (qwen2.5-7b-gpu) so the LLM actually
responds and we exercise the full stack (HTTP -> queue -> agent_loop
-> LLM -> persistence -> events -> client).
"""
