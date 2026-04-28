# dev_tools - Testing & Building SDK as agent tools

Exposes the `digitorn.testing` client as 3 ultra-powerful agent tools so
a Builder or Test agent can deploy, chat, and run apps exactly like a
human using the Flutter client.

## Tools

- **App** - lifecycle, discovery, packages, MCP, drafts, security, compile
- **Chat** - sessions, queue, approvals, memory, workspace, live events (`watch=true`)
- **Run** - one-shot, pipeline, triggers, background sessions, background tasks, watchers

See `docs/actions.md` for the full parameter reference.

## When to use

Any app that needs to test, build, or orchestrate other Digitorn apps:

- `digitorn-builder` - craft + test apps from user prompts
- Test harness apps - run regression scenarios
- Integration apps - call one app from another

## Configuration

No config required. The module auto-discovers the running daemon at
`http://127.0.0.1:8000` and reuses the current user's auth token from
`~/.digitorn/cli_credentials.json`, or via `DevClient.with_token(...)`
when used programmatically.
