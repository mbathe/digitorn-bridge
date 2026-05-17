"""Live scenarios proving every runtime-injected system directive is
persisted with a canonical seq AND visible to the LLM in-flight.

These run against:
  - a live daemon at http://127.0.0.1:8000
  - a live Ollama at http://127.0.0.1:11434 with ``qwen2.5:7b``
  - an OpenAI-compat tap proxy in front of Ollama that captures every
    ``messages: [...]`` payload sent to the model

The proxy is the proof-of-LLM-saw-it instrumentation the user asked
for. The daemon's persistent events API is the proof of seq +
restoration. Together they cover the contract end to end.
"""
