# Copilot Smoke Test

Minimal smoke-test harness wired to the GitHub Copilot endpoint
(`api.githubcopilot.com`, model `gpt-5-mini`). Auto-deployed at every
daemon start (`bootstrap_builtins`) at `scope=system`, visible to
every user in the app listing.

Doubles as the canonical test bed for new modules. Quick prompts in
the YAML probe specific behaviours:

| Prompt | Module exercised | What it tests |
|---|---|---|
| 🔍 Identify model | brain | Does Copilot return identity correctly? |
| 💻 Code sample | brain | Code generation quality |
| 🧠 Reasoning | brain | Multi-step reasoning |
| 🖥️ Shell smoke | `shell` | `Bash` execution + sync output capture |
| 🌐 Static landing page | `workspace` + `web_preview` | Does the agent figure out `PreviewStatic` on its own? |
| ⚡ Vite app live | `shell` + `web_preview` | Does the agent spawn a dev server and call `PreviewProxy` correctly? |

**Test contract for `web_preview`**: the agent's `system_prompt` deliberately makes NO mention of `PreviewProxy` / `PreviewStatic`. The expectation is that the module's own per-action `tool_prompt` and the auto-injected `Live Preview — Environment Awareness` section (via `get_prompt_sections`) are powerful enough that the agent navigates the workflow correctly:

1. Build/spawn the right artefact via `Bash` (foreground for build, `run_in_background=true` for dev server)
2. Wait for the server to bind / build to finish
3. Call `PreviewProxy` or `PreviewStatic` with the right name
4. Tell the user to open the **Preview** tab in the Workspace panel

If the agent fumbles step 1–4, the tool prompts need work, not the system prompt.

## Credential

The `github_copilot` credential must exist for the active user before the first turn (per-user scope).
