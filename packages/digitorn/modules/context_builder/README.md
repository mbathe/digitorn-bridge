# Context Builder Module

Meta-tool provider for agent tool discovery and execution. Provides 5 actions that let agents discover and use any tool across all loaded modules without flooding the LLM context.

## How it works

Instead of injecting thousands of tools into the agent context, this module builds an inverted keyword index and exposes 5 meta-tools: `search_tools`, `get_tool`, `execute_tool`, `list_categories`, `browse_category`.

## Platforms

All platforms (Linux, macOS, Windows).
