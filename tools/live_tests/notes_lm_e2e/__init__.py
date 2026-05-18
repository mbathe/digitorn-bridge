"""Real end-to-end tests for notes-lm against the live daemon.

Drives the agent like a human via DevClient:
  - create session, send messages, wait for assistant tokens
  - inspect workspace files, persistent events, message history
  - assert on shape + content, not just "no exception"

Each phase tests one user-facing capability. When an assertion fails
we identify root cause + fix (system prompt / app yaml / code) before
moving on.
"""
